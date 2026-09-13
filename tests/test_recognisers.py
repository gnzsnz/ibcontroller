"""Unit tests for recognisers.py (L5) -- no live Gateway needed, same fake-socket-server
pattern actions.py's own tests use."""

from __future__ import annotations

import asyncio

import pytest

from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    Component,
    WindowEvent,
    WindowInfo,
)
from ibcontroller.config import AcceptIncomingConnections, ExistingSessionAction
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import DismissRule, LabelsError, load_labels
from ibcontroller.logging_setup import configure_logging, stop_logging
from ibcontroller.recognisers import (
    AcceptIncomingConnectionsRecognizer,
    DeclarativeDismissRecognizer,
    ExistingSessionRecognizer,
    LoginFailedError,
    LoginFailedRecognizer,
    RecognizerRegistry,
    TooManyFailedLoginAttemptsRecognizer,
    _parse_wait_seconds,
    handle_window_opened,
    is_credential_entry,
    watch_for_unprompted_windows,
)
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio

LABELS = load_labels()
_NON_BROKERAGE_RULE = next(
    r for r in LABELS.dismiss_rules if r.name == "non_brokerage_account"
)
_AUTO_RESTART_RULE = next(
    r for r in LABELS.dismiss_rules if r.name == "auto_restart_confirmation"
)


async def _start(cmd_sock, event_sock) -> Dispatcher:
    dispatcher = Dispatcher(
        AgentCommandConnection(cmd_sock), AgentEventConnection(event_sock)
    )
    await dispatcher.start()
    return dispatcher


def _component(**overrides) -> Component:
    base = dict(class_="javax.swing.JLabel", text=None, credential_field=False)
    base.update(overrides)
    return Component(**base)


def _event(title: str, window_id: str | None = None) -> WindowEvent:
    return WindowEvent(
        seq=1,
        kind="window_opened",
        window=WindowInfo(class_="x", title=title, window_id=window_id),
    )


def test_is_credential_entry_true_when_a_showing_credential_field_exists():
    components = [
        _component(),
        _component(credential_field=True, showing=True),
    ]
    assert is_credential_entry(components) is True


def test_is_credential_entry_false_when_none_flagged():
    components = [_component(), _component()]
    assert is_credential_entry(components) is False


def test_is_credential_entry_false_for_a_stale_not_showing_credential_field():
    """The real case this gating exists for: confirmed live (2026-09-05) that
    Gateway keeps the original login frame alive (not disposed) after a real
    login completes -- its password field still reports `credential_field`,
    but `showing=False`. Without gating on `showing`, this would stay
    permanently True for the rest of the process's life."""
    components = [_component(credential_field=True, showing=False)]
    assert is_credential_entry(components) is False


def test_dismiss_rule_requires_at_least_one_match_field():
    with pytest.raises(LabelsError, match=r"match_title.*match_text"):
        DismissRule(name="bad", click="OK")


def test_declarative_dismiss_recognises_by_text_via_accessible_name():
    """The real field this data lands in -- confirmed live against the actual dialog,
    not assumed: `ComponentLookup.describe()`'s `text` field only ever comes from
    `JTextComponent`, and `JLabel` isn't one, so a label's displayed text never
    reaches `component.text` at all. It surfaces via `accessible_name` instead
    (Swing's default `AccessibleJLabel` behavior)."""
    recognizer = DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)
    event = _event("IBKR Gateway")
    matching = [
        _component(
            accessible_name=(
                'This is not a brokerage account. This is a "paper" trading '
                "account in which you can engage in simulated trading."
            )
        )
    ]
    not_matching = [_component(accessible_name="something else entirely")]

    assert recognizer.recognises(event, matching) is True
    assert recognizer.recognises(event, not_matching) is False


def test_declarative_dismiss_also_matches_via_text_field():
    """Robustness for a hypothetical future Gateway version rendering this as an
    actual text component instead of a plain label."""
    recognizer = DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)
    event = _event("IBKR Gateway")
    matching = [_component(text="This is not a brokerage account -- read carefully")]

    assert recognizer.recognises(event, matching) is True


def test_declarative_dismiss_recognises_by_title():
    rule = DismissRule(name="test_title", click="OK", match_title="Login failed")
    recognizer = DeclarativeDismissRecognizer(rule)
    assert recognizer.recognises(_event("Login failed"), []) is True
    assert recognizer.recognises(_event("IBKR Gateway"), []) is False


def test_declarative_dismiss_requires_both_when_both_given():
    rule = DismissRule(
        name="test_and", click="OK", match_title="Warning", match_text="specific text"
    )
    recognizer = DeclarativeDismissRecognizer(rule)
    # Title matches, text doesn't -- must not recognise (AND, not OR).
    assert (
        recognizer.recognises(_event("Warning"), [_component(text="unrelated")])
        is False
    )
    # Text matches, title doesn't.
    assert (
        recognizer.recognises(
            _event("IBKR Gateway"), [_component(text="specific text")]
        )
        is False
    )
    # Both match.
    assert (
        recognizer.recognises(_event("Warning"), [_component(text="specific text")])
        is True
    )


async def test_declarative_dismiss_handle_clicks_configured_button(
    sock_path, event_sock_path
):
    seen = {}

    def responder(request):
        seen["target"] = request["target"]
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)
            await recognizer.handle(_event("IBKR Gateway"), [], dispatcher)
        finally:
            await dispatcher.stop()

    assert seen["target"] == "I understand and accept"


async def test_declarative_dismiss_handle_scopes_click_to_the_recognised_window(
    sock_path, event_sock_path
):
    """Regression test for a real, live-caught bug (2026-09-06, reproduced twice
    on a genuinely fresh settings directory): the old unscoped click landed on
    the wrong of two simultaneously-open, identically-labeled "OK" buttons (this
    dialog's own, versus the Global Configuration dialog's, which had just been
    asked to close but hadn't finished disposing yet). `handle` must pass the
    recognised event's own `window_id` through to `click`."""
    seen = {}

    def responder(request):
        seen["window_id"] = request.get("window_id")
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)
            await recognizer.handle(
                _event("IBKR Gateway", window_id="w7"), [], dispatcher
            )
        finally:
            await dispatcher.stop()

    assert seen["window_id"] == "w7"


async def test_declarative_dismiss_handle_logs_with_rule_name(
    sock_path, event_sock_path, tmp_path
):
    """Integration check that the logging (2026-09-05) actually reaches a real
    file, not just that the call site exists -- same file-based pattern as
    test_logging_setup.py (caplog can't see it: configure_logging disables
    propagation to the root logger on purpose)."""
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)
            await recognizer.handle(_event("IBKR Gateway"), [], dispatcher)
        finally:
            await dispatcher.stop()

    # Records reach the file via configure_logging's listener thread (2026-09-08);
    # draining it is what guarantees they've been written before we read.
    stop_logging()
    log_text = (tmp_path / "test.log").read_text()
    assert "dismissed non_brokerage_account (declarative rule)" in log_text


async def test_login_failed_recognises_by_title():
    recognizer = LoginFailedRecognizer(LABELS.login_failed)
    assert recognizer.recognises(_event("Login failed"), []) is True
    assert recognizer.recognises(_event("IBKR Gateway"), []) is False


async def test_login_failed_handle_dismisses_then_raises(sock_path, event_sock_path):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = LoginFailedRecognizer(LABELS.login_failed)
            with pytest.raises(LoginFailedError):
                await recognizer.handle(_event("Login failed"), [], dispatcher)
        finally:
            await dispatcher.stop()


async def test_existing_session_recognises_by_title():
    recognizer = ExistingSessionRecognizer(
        LABELS.existing_session, ExistingSessionAction.MANUAL, lambda: False
    )
    assert recognizer.recognises(_event("Existing session detected"), []) is True
    assert recognizer.recognises(_event("IBKR Gateway"), []) is False


async def test_existing_session_manual_does_nothing(sock_path, event_sock_path):
    calls = []

    async with (
        FakeCommandServer(sock_path, lambda req: (calls.append(req), {"ok": True})[1]),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = ExistingSessionRecognizer(
                LABELS.existing_session, ExistingSessionAction.MANUAL, lambda: False
            )
            await recognizer.handle(_event("Existing session detected"), [], dispatcher)
        finally:
            await dispatcher.stop()

    assert calls == []


async def test_existing_session_secondary_clicks_cancel(sock_path, event_sock_path):
    seen = []

    async with (
        FakeCommandServer(
            sock_path, lambda req: (seen.append(req["target"]), {"ok": True})[1]
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = ExistingSessionRecognizer(
                LABELS.existing_session, ExistingSessionAction.SECONDARY, lambda: False
            )
            await recognizer.handle(_event("Existing session detected"), [], dispatcher)
        finally:
            await dispatcher.stop()

    assert seen == ["Cancel"]


async def test_existing_session_first_attempt_continues_second_attempt_cancels(
    sock_path, event_sock_path
):
    """Matches IBC's own scenario 3 -> 4: not logged in, unknown other session type --
    continue on the first attempt, give up on the second."""
    seen = []

    async with (
        FakeCommandServer(
            sock_path, lambda req: (seen.append(req["target"]), {"ok": True})[1]
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = ExistingSessionRecognizer(
                LABELS.existing_session, ExistingSessionAction.PRIMARY, lambda: False
            )
            event = _event("Existing session detected")
            await recognizer.handle(event, [], dispatcher)
            await recognizer.handle(event, [], dispatcher)
        finally:
            await dispatcher.stop()

    assert seen == ["OK", "Cancel"]


async def test_existing_session_logged_in_primary_continues(sock_path, event_sock_path):
    seen = []

    async with (
        FakeCommandServer(
            sock_path, lambda req: (seen.append(req["target"]), {"ok": True})[1]
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = ExistingSessionRecognizer(
                LABELS.existing_session, ExistingSessionAction.PRIMARY, lambda: True
            )
            await recognizer.handle(_event("Existing session detected"), [], dispatcher)
        finally:
            await dispatcher.stop()

    assert seen == ["OK"]


async def test_existing_session_logged_in_primary_override_cancels(
    sock_path, event_sock_path
):
    seen = []

    async with (
        FakeCommandServer(
            sock_path, lambda req: (seen.append(req["target"]), {"ok": True})[1]
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = ExistingSessionRecognizer(
                LABELS.existing_session,
                ExistingSessionAction.PRIMARY_OVERRIDE,
                lambda: True,
            )
            await recognizer.handle(_event("Existing session detected"), [], dispatcher)
        finally:
            await dispatcher.stop()

    assert seen == ["Cancel"]


async def test_accept_incoming_connection_recognises_by_title():
    recognizer = AcceptIncomingConnectionsRecognizer(
        LABELS.accept_incoming_connection, AcceptIncomingConnections.MANUAL
    )
    assert recognizer.recognises(_event("Accept incoming connection"), []) is True
    assert recognizer.recognises(_event("IBKR Gateway"), []) is False


async def test_accept_incoming_connection_manual_does_nothing(
    sock_path, event_sock_path
):
    calls = []

    async with (
        FakeCommandServer(sock_path, lambda req: (calls.append(req), {"ok": True})[1]),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = AcceptIncomingConnectionsRecognizer(
                LABELS.accept_incoming_connection, AcceptIncomingConnections.MANUAL
            )
            await recognizer.handle(
                _event("Accept incoming connection"), [], dispatcher
            )
        finally:
            await dispatcher.stop()

    assert calls == []


async def test_accept_incoming_connection_accept_clicks_ok(sock_path, event_sock_path):
    seen = []

    async with (
        FakeCommandServer(
            sock_path, lambda req: (seen.append(req["target"]), {"ok": True})[1]
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = AcceptIncomingConnectionsRecognizer(
                LABELS.accept_incoming_connection, AcceptIncomingConnections.ACCEPT
            )
            await recognizer.handle(
                _event("Accept incoming connection"), [], dispatcher
            )
        finally:
            await dispatcher.stop()

    assert seen == ["OK"]


async def test_accept_incoming_connection_reject_clicks_no(sock_path, event_sock_path):
    seen = []

    async with (
        FakeCommandServer(
            sock_path, lambda req: (seen.append(req["target"]), {"ok": True})[1]
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = AcceptIncomingConnectionsRecognizer(
                LABELS.accept_incoming_connection, AcceptIncomingConnections.REJECT
            )
            await recognizer.handle(
                _event("Accept incoming connection"), [], dispatcher
            )
        finally:
            await dispatcher.stop()

    assert seen == ["No"]


async def test_registry_dispatch_first_match_wins(sock_path, event_sock_path):
    """Two recognisers that would both match -- only the first in registry order
    actually runs."""
    calls = []

    class _AlwaysMatchesFirst:
        def recognises(self, event, components):
            return True

        async def handle(self, event, components, dispatcher):
            calls.append("first")

    class _AlwaysMatchesSecond:
        def recognises(self, event, components):
            return True

        async def handle(self, event, components, dispatcher):
            calls.append("second")

    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry(
                [_AlwaysMatchesFirst(), _AlwaysMatchesSecond()]
            )
            handled = await registry.dispatch(_event("anything"), [], dispatcher)
        finally:
            await dispatcher.stop()

    assert handled is True
    assert calls == ["first"]


async def test_registry_dispatch_returns_false_when_nothing_matches(
    sock_path, event_sock_path
):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry(
                [DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)]
            )
            handled = await registry.dispatch(
                _event("Some Unrelated Window"), [], dispatcher
            )
        finally:
            await dispatcher.stop()

    assert handled is False


async def test_registry_dispatch_propagates_builtin_exception(
    sock_path, event_sock_path
):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry([LoginFailedRecognizer(LABELS.login_failed)])
            with pytest.raises(LoginFailedError):
                await registry.dispatch(_event("Login failed"), [], dispatcher)
        finally:
            await dispatcher.stop()


async def test_handle_window_opened_skips_dispatch_when_credential_field_present(
    sock_path, event_sock_path
):
    """The one unconditional check: dump() shows a live credential field -> refuse the
    whole registry, regardless of what else matches."""

    def responder(request):
        assert request["cmd"] == "dump"
        return {
            "ok": True,
            "components": [
                {
                    "class": "javax.swing.JPasswordField",
                    "credential_field": True,
                    "showing": True,
                }
            ],
        }

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry(
                [DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)]
            )
            handled = await handle_window_opened(
                registry, _event("IBKR Gateway"), dispatcher
            )
        finally:
            await dispatcher.stop()

    assert handled is False


async def test_handle_window_opened_dispatches_when_no_credential_field(
    sock_path, event_sock_path
):
    calls = []

    def responder(request):
        calls.append(request["cmd"])
        if request["cmd"] == "dump":
            return {
                "ok": True,
                "components": [
                    {
                        "class": "javax.swing.JLabel",
                        "text": "This is not a brokerage account",
                    }
                ],
            }
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry(
                [DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)]
            )
            handled = await handle_window_opened(
                registry, _event("IBKR Gateway"), dispatcher
            )
        finally:
            await dispatcher.stop()

    assert handled is True
    assert calls == ["dump", "click"]


async def test_handle_window_opened_scopes_dump_to_the_window_id(
    sock_path, event_sock_path
):
    """Regression test for a real, live-caught bug (2026-09-06): dumping by
    title alone can aggregate several simultaneously-open windows sharing
    that title (the persisting login frame, the real main window, a fresh
    popup) into one response -- large enough on a live account to corrupt
    the shared command connection. `window_id` scopes the dump to the one
    window that actually opened."""
    seen = {}

    def responder(request):
        if request["cmd"] == "dump":
            seen["window_id"] = request.get("window_id")
            return {"ok": True, "components": []}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry([])
            await handle_window_opened(
                registry, _event("IBKR Gateway", window_id="w7"), dispatcher
            )
        finally:
            await dispatcher.stop()

    assert seen["window_id"] == "w7"


async def test_handle_window_opened_treats_a_vanished_window_as_unhandled(
    sock_path, event_sock_path
):
    """The consistent-with-writes choice (2026-09-06, per the user's own call):
    `dump` raises `WindowGoneError` the same way the write commands do when
    their `window_id` no longer resolves (the window closed between
    recognition and this command arriving -- a real, expected race). Caught
    here rather than left to crash `watch_for_unprompted_windows`'s loop --
    a vanished window has nothing left to recognise or handle."""

    def responder(request):
        return {
            "ok": False,
            "error": "window_gone",
            "detail": "window w7 is no longer open",
        }

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry(
                [DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)]
            )
            handled = await handle_window_opened(
                registry, _event("IBKR Gateway", window_id="w7"), dispatcher
            )
        finally:
            await dispatcher.stop()

    assert handled is False


async def test_watch_for_unprompted_windows_dispatches_a_window_that_arrives_late(
    sock_path, event_sock_path
):
    """The real scenario this function exists for (2026-09-05): a pop-up that
    opens *after* Login's own active sequence has already finished watching.
    Confirms the background watcher still catches and dismisses it."""
    calls = []

    def responder(request):
        calls.append(request.get("cmd"))
        if request.get("cmd") == "dump":
            return {
                "ok": True,
                "components": [
                    {
                        "class": "javax.swing.JLabel",
                        "accessible_name": _NON_BROKERAGE_RULE.match_text,
                    }
                ],
            }
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry(
                [DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)]
            )
            watcher = asyncio.ensure_future(
                watch_for_unprompted_windows(registry, dispatcher)
            )
            await asyncio.sleep(0.05)  # let the watcher connect its listener
            dispatcher.window_events.emit(_event("Warning"))
            await asyncio.sleep(0.2)  # let it process the dispatch
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
        finally:
            await dispatcher.stop()

    assert "dump" in calls
    assert "click" in calls


# --- TooManyFailedLoginAttemptsRecognizer / _parse_wait_seconds --------------


def test_parse_wait_seconds_minutes_and_seconds():
    message = (
        "Too many failed login attempts. Please wait 4 minutes & 47 seconds "
        "before attempting to re-login again."
    )
    assert _parse_wait_seconds(message) == 4 * 60 + 47 + 3


def test_parse_wait_seconds_seconds_only():
    message = (
        "Too many failed login attempts. Please wait 53 seconds before "
        "attempting to re-login again."
    )
    assert _parse_wait_seconds(message) == 53 + 3


def test_parse_wait_seconds_falls_back_to_buffer_when_unmatched():
    assert _parse_wait_seconds("nothing recognisable here") == 3.0


def test_auto_restart_confirmation_recognises_by_text():
    recognizer = DeclarativeDismissRecognizer(_AUTO_RESTART_RULE)
    event = _event("Warning")
    matching = [
        _component(
            text="IB Gateway will close the current session and the trading "
            "platform restart automatically in a few minutes"
        )
    ]
    non_matching = [_component()]
    assert recognizer.recognises(event, matching) is True
    assert recognizer.recognises(event, non_matching) is False


def test_auto_restart_confirmation_recognises_via_accessible_name_too():
    recognizer = DeclarativeDismissRecognizer(_AUTO_RESTART_RULE)
    event = _event("Warning")
    matching = [
        _component(
            text=None,
            accessible_name="...trading platform restart automatically...",
        )
    ]
    assert recognizer.recognises(event, matching) is True


async def test_auto_restart_confirmation_dismisses_via_ok(sock_path, event_sock_path):
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True}

    recognizer = DeclarativeDismissRecognizer(_AUTO_RESTART_RULE)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            components = [
                _component(text="...trading platform restart automatically...")
            ]
            await recognizer.handle(_event("Warning"), components, dispatcher)
        finally:
            await dispatcher.stop()

    assert any(c.get("target") == "OK" for c in calls)


def test_too_many_failed_login_attempts_recognises_by_text():
    recognizer = TooManyFailedLoginAttemptsRecognizer(
        LABELS.too_many_failed_login_attempts,
        relogin_enabled=True,
        schedule_retry=lambda _wait: None,
    )
    event = _event("Warning")
    matching = [
        _component(text="Too many failed login attempts. Please wait 5 seconds.")
    ]
    non_matching = [_component()]
    assert recognizer.recognises(event, matching) is True
    assert recognizer.recognises(event, non_matching) is False


async def test_too_many_failed_login_attempts_disabled_does_nothing(
    sock_path, event_sock_path
):
    calls: list[str] = []

    def responder(request):
        calls.append(request.get("cmd", ""))
        return {"ok": True}

    scheduled: list[float] = []
    recognizer = TooManyFailedLoginAttemptsRecognizer(
        LABELS.too_many_failed_login_attempts,
        relogin_enabled=False,
        schedule_retry=scheduled.append,
    )
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            components = [
                _component(
                    text="Too many failed login attempts. Please wait 5 seconds."
                )
            ]
            await recognizer.handle(_event("Warning"), components, dispatcher)
        finally:
            await dispatcher.stop()

    assert calls == []  # no click at all -- matches IBC exactly
    assert scheduled == []


async def test_too_many_failed_login_attempts_enabled_dismisses_and_schedules(
    sock_path, event_sock_path
):
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True}

    scheduled: list[float] = []
    recognizer = TooManyFailedLoginAttemptsRecognizer(
        LABELS.too_many_failed_login_attempts,
        relogin_enabled=True,
        schedule_retry=scheduled.append,
    )
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            components = [
                _component(
                    text=(
                        "Too many failed login attempts. Please wait 4 minutes "
                        "& 47 seconds before attempting to re-login again."
                    )
                )
            ]
            await recognizer.handle(_event("Warning"), components, dispatcher)
        finally:
            await dispatcher.stop()

    dismiss_calls = [c for c in calls if c.get("target") == "OK"]
    assert dismiss_calls
    assert scheduled == [4 * 60 + 47 + 3]
