"""Unit tests for recognisers.py (L5) -- no live Gateway needed, same fake-socket-server
pattern actions.py's own tests use."""

from __future__ import annotations

import asyncio
import logging
import types
from tempfile import gettempdir

import attrs
import pytest
from typed_settings.types import Secret

from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    Component,
    WindowEvent,
    WindowInfo,
)
from ibcontroller.config import (
    AcceptIncomingConnections,
    Config,
    ExistingSessionAction,
    Program,
    TradingMode,
)
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import DismissRule, LabelsError, load_labels
from ibcontroller.logging_setup import configure_logging, stop_logging
from ibcontroller.login import (
    CredentialsRejectedError,
    InvalidSessionStateError,
    SessionCededError,
    TransientLoginError,
)
from ibcontroller.recognisers import (
    AcceptIncomingConnectionsRecognizer,
    DeclarativeDismissRecognizer,
    ExistingSessionRecognizer,
    ExitKind,
    ExitProgressRecognizer,
    GatewayConnectionFailedRecognizer,
    LoginErrorRecognizer,
    LoginFailedRecognizer,
    NotCurrentlyAvailableRecognizer,
    RecognizerRegistry,
    ReloginRecognizer,
    TooManyFailedLoginAttemptsRecognizer,
    TradingLoginHandoffRecognizer,
    UnrecognizedCredentialsRecognizer,
    _parse_wait_seconds,
    _strip_html,
    build_default_registry,
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


def _event(title: str, window_id: str | None = None, class_: str = "x") -> WindowEvent:
    return WindowEvent(
        seq=1,
        kind="window_opened",
        window=WindowInfo(class_=class_, title=title, window_id=window_id),
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
    """The case this gating exists for: Gateway keeps the original login frame
    alive (not disposed) after a real login completes -- its password field
    still reports `credential_field`, but `showing=False`. Without gating on
    `showing`, this would stay permanently True for the rest of the process's life."""
    components = [_component(credential_field=True, showing=False)]
    assert is_credential_entry(components) is False


def test_dismiss_rule_requires_at_least_one_match_field():
    with pytest.raises(LabelsError, match=r"match_title.*match_text"):
        DismissRule(name="bad", click="OK")


def test_declarative_dismiss_recognises_by_text_via_accessible_name():
    """The field this data lands in: `ComponentLookup.describe()`'s `text` field
    only ever comes from `JTextComponent`, and `JLabel` isn't one, so a
    label's displayed text never reaches `component.text` at all. It surfaces
    via `accessible_name` instead (Swing's default `AccessibleJLabel` behavior)."""
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


def test_declarative_dismiss_matches_a_phrase_spanning_an_html_tag():
    """IB message panes are HTML: a phrase copied from the screen can span a
    `<br>`/`<b>` that the raw text carries between its words."""
    recognizer = DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE)
    assert _NON_BROKERAGE_RULE.match_text is not None
    words = _NON_BROKERAGE_RULE.match_text.split(" ", 1)
    markup = f"<html>{words[0]}<br> <b>{words[1]}</b></html>"

    assert recognizer.recognises(_event("IBKR Gateway"), [_component(text=markup)])


def test_declarative_dismiss_matches_a_phrase_holding_an_html_entity():
    """From a live 2FA dialog dump: the pane holds `&quot;` where the screen
    shows a quote, and a `<br>` mid-sentence."""
    rule = DismissRule(
        name="x", match_text='go to "authenticate" from the services screen', click="OK"
    )
    markup = (
        "<html><body>In the app go to &quot;authenticate&quot; from the "
        "services<br>screen.</body></html>"
    )

    assert DeclarativeDismissRecognizer(rule).recognises(
        _event("2FA"), [_component(text=markup)]
    )


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
    """Regression test: an unscoped click lands on
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
    """Integration check that the logging actually reaches a real
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

    # Records reach the file via configure_logging's listener thread;
    # draining it is what guarantees they've been written before we read.
    stop_logging()
    log_text = (tmp_path / "test.log").read_text()
    assert "dismissed non_brokerage_account (declarative rule)" in log_text


async def test_login_failed_recognises_by_title():
    recognizer = LoginFailedRecognizer(LABELS.login_failed)
    assert recognizer.recognises(_event("Login failed"), []) is True
    assert recognizer.recognises(_event("IBKR Gateway"), []) is False


async def test_login_failed_handle_dismisses_then_raises(sock_path, event_sock_path):
    """ "Login failed" is IB's own wording for a server disconnect,
    not rejected credentials -- so it's `TransientLoginError` (unbounded
    in-process relaunch), not `CredentialsRejectedError`."""
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = LoginFailedRecognizer(LABELS.login_failed)
            with pytest.raises(TransientLoginError):
                await recognizer.handle(_event("Login failed"), [], dispatcher)
        finally:
            await dispatcher.stop()


def test_strip_html_collapses_tags_and_whitespace():
    raw = (
        "<html>Connection to server failed:  <br><br>  The specified user has "
        "multiple Paper Trading users associated with it.<br><br>Please log on "
        "using one of the Paper Trading users and corresponding password.</html>"
    )
    stripped = _strip_html(raw)
    assert stripped.startswith("Connection to server failed:")
    assert "multiple Paper Trading users associated with it" in stripped
    assert "<" not in stripped


async def test_login_error_recognises_by_title():
    recognizer = LoginErrorRecognizer(LABELS.login_error)
    assert recognizer.recognises(_event("Login Error"), []) is True
    assert recognizer.recognises(_event("IBKR Gateway"), []) is False


async def test_login_error_handle_logs_dismisses_then_raises(
    sock_path, event_sock_path, caplog
):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = LoginErrorRecognizer(LABELS.login_error)
            components = [
                _component(text="<html>Server disconnected, please try again</html>")
            ]
            with (
                caplog.at_level(logging.WARNING, logger="ibcontroller.recognisers"),
                pytest.raises(TransientLoginError),
            ):
                await recognizer.handle(_event("Login Error"), components, dispatcher)
        finally:
            await dispatcher.stop()
    assert any("Server disconnected" in message for message in caplog.messages)


def test_not_currently_available_recognises_by_text_ignoring_case_and_html():
    recognizer = NotCurrentlyAvailableRecognizer(LABELS.not_currently_available)
    event = _event("Login")
    for text in (
        "The system is not currently available.",
        "<html>Service Not Currently Available</html>",
    ):
        assert recognizer.recognises(event, [_component(text=text)]) is True
    assert (
        recognizer.recognises(
            event, [_component(text=None, accessible_name="not currently available")]
        )
        is True
    )
    assert recognizer.recognises(event, [_component(text="Login failed")]) is False
    assert recognizer.recognises(event, []) is False


async def test_not_currently_available_dismisses_then_raises(
    sock_path, event_sock_path
):
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = NotCurrentlyAvailableRecognizer(LABELS.not_currently_available)
            with pytest.raises(TransientLoginError):
                await recognizer.handle(_event("Login", "w1"), [], dispatcher)
        finally:
            await dispatcher.stop()
    assert [c.get("target") for c in calls if c.get("cmd") == "click"] == ["OK"]


def test_not_currently_available_registered_before_login_failed():
    registry = build_default_registry(
        Config(
            program=Program.GATEWAY,
            tws_version="10.50",
            trading_mode=TradingMode.PAPER,
            userid=Secret("u"),
            password=Secret("p"),
            log_dir=f"{gettempdir()}/log",
        ),
        LABELS,
        types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: False),  # type: ignore[arg-type]
        lambda _s: None,
    )
    order = [type(r) for r in registry._registry]
    assert order.index(NotCurrentlyAvailableRecognizer) < order.index(
        LoginFailedRecognizer
    )


async def test_unrecognized_credentials_recognises_by_title():
    recognizer = UnrecognizedCredentialsRecognizer(LABELS.unrecognized_credentials)
    assert (
        recognizer.recognises(_event("Unrecognized Username or Password"), []) is True
    )
    assert recognizer.recognises(_event("IBKR Gateway"), []) is False


async def test_unrecognized_credentials_handle_dismisses_then_raises(
    sock_path, event_sock_path
):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = UnrecognizedCredentialsRecognizer(
                LABELS.unrecognized_credentials
            )
            with pytest.raises(CredentialsRejectedError):
                await recognizer.handle(
                    _event("Unrecognized Username or Password"), [], dispatcher
                )
        finally:
            await dispatcher.stop()


def test_gateway_connection_failed_recognises_only_the_prefixed_text():
    recognizer = GatewayConnectionFailedRecognizer(LABELS.gateway_connection_failed)
    matching = [
        _component(
            class_="javax.swing.JTextPane",
            text="<html>Connection to server failed: some reason</html>",
        )
    ]
    unrelated_text = [
        _component(class_="javax.swing.JTextPane", text="<html>Hi</html>")
    ]

    assert recognizer.recognises(_event("Gateway"), matching) is True
    assert recognizer.recognises(_event("Gateway"), unrelated_text) is False
    assert recognizer.recognises(_event("Existing session detected"), matching) is False


async def test_gateway_connection_failed_known_reason_raises_login_failed(
    sock_path, event_sock_path
):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = GatewayConnectionFailedRecognizer(
                LABELS.gateway_connection_failed
            )
            components = [
                _component(
                    class_="javax.swing.JTextPane",
                    text=(
                        "<html>Connection to server failed: The specified user "
                        "has multiple Paper Trading users associated with it."
                        "</html>"
                    ),
                )
            ]
            with pytest.raises(CredentialsRejectedError):
                await recognizer.handle(_event("Gateway"), components, dispatcher)
        finally:
            await dispatcher.stop()


async def test_gateway_connection_failed_unknown_reason_raises_transient(
    sock_path, event_sock_path
):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = GatewayConnectionFailedRecognizer(
                LABELS.gateway_connection_failed
            )
            components = [
                _component(
                    class_="javax.swing.JTextPane",
                    text="<html>Connection to server failed: stale restart token"
                    "</html>",
                )
            ]
            with pytest.raises(TransientLoginError):
                await recognizer.handle(_event("Gateway"), components, dispatcher)
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


async def test_existing_session_secondary_cancels_and_ends(sock_path, event_sock_path):
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
            with pytest.raises(SessionCededError):
                await recognizer.handle(
                    _event("Existing session detected"), [], dispatcher
                )
        finally:
            await dispatcher.stop()

    assert seen == ["Cancel"]


async def test_existing_session_first_attempt_continues_second_attempt_ends(
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
            with pytest.raises(SessionCededError):
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


async def test_existing_session_logged_in_primary_override_ends(
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
            with pytest.raises(SessionCededError):
                await recognizer.handle(
                    _event("Existing session detected"), [], dispatcher
                )
        finally:
            await dispatcher.stop()

    assert seen == ["Cancel"]


async def _run_recognizer(recognizer, title, sock_path, event_sock_path):
    """Runs `recognizer.handle` against a fake agent; returns (clicked targets,
    exception or None)."""
    seen = []
    error = None
    async with (
        FakeCommandServer(
            sock_path, lambda req: (seen.append(req["target"]), {"ok": True})[1]
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await recognizer.handle(_event(title), [], dispatcher)
        except (SessionCededError, InvalidSessionStateError) as exc:
            error = exc
        finally:
            await dispatcher.stop()
    return seen, error


async def test_relogin_recognises_by_title():
    recognizer = ReloginRecognizer(LABELS.relogin, ExistingSessionAction.PRIMARY)
    assert recognizer.recognises(_event("Re-login is required"), []) is True
    assert recognizer.recognises(_event("Existing session detected"), []) is False


@pytest.mark.parametrize(
    ("action", "clicked", "error"),
    [
        (ExistingSessionAction.MANUAL, [], None),
        (ExistingSessionAction.PRIMARY, ["Re-login"], None),
        (ExistingSessionAction.PRIMARY_OVERRIDE, ["Re-login"], None),
        (ExistingSessionAction.SECONDARY, ["Cancel"], SessionCededError),
    ],
)
async def test_relogin_by_action(action, clicked, error, sock_path, event_sock_path):
    seen, raised = await _run_recognizer(
        ReloginRecognizer(LABELS.relogin, action),
        "Re-login is required",
        sock_path,
        event_sock_path,
    )
    assert seen == clicked
    assert type(raised) is error if error else raised is None


async def test_relogin_keeps_session_existing(sock_path, event_sock_path):
    """After Re-login the next existing-session dialog must still take the
    EXISTING path (login state untouched), so `primary` continues."""
    existing = ExistingSessionRecognizer(
        LABELS.existing_session, ExistingSessionAction.PRIMARY, lambda: True
    )
    relogin = ReloginRecognizer(LABELS.relogin, ExistingSessionAction.PRIMARY)
    await _run_recognizer(relogin, "Re-login is required", sock_path, event_sock_path)
    seen, raised = await _run_recognizer(
        existing, "Existing session detected", sock_path, event_sock_path
    )
    assert seen == ["OK"]
    assert raised is None


@pytest.mark.parametrize(
    ("action", "clicked", "error"),
    [
        (ExistingSessionAction.MANUAL, [], None),
        (ExistingSessionAction.PRIMARY, ["Disconnect Other Session"], None),
        (ExistingSessionAction.PRIMARY_OVERRIDE, [], InvalidSessionStateError),
        (ExistingSessionAction.SECONDARY, [], InvalidSessionStateError),
    ],
)
async def test_trading_login_handoff_by_action(
    action, clicked, error, sock_path, event_sock_path
):
    recognizer = TradingLoginHandoffRecognizer(LABELS.trading_login_handoff, action)
    assert recognizer.recognises(_event("Trading Login Handoff"), []) is True
    seen, raised = await _run_recognizer(
        recognizer, "Trading Login Handoff", sock_path, event_sock_path
    )
    assert seen == clicked
    assert type(raised) is error if error else raised is None


def test_session_exit_codes():
    assert SessionCededError.exit_code == 0
    assert InvalidSessionStateError.exit_code == 76


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
            with pytest.raises(TransientLoginError):
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
    """Regression test: dumping by
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
    """The consistent-with-writes choice:
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
    """The real scenario this function exists for: a pop-up that
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
            dispatcher._window_events.emit(_event("Warning"))
            await asyncio.sleep(0.2)  # let it process the dispatch
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
        finally:
            await dispatcher.stop()

    assert "dump" in calls
    assert "click" in calls


@pytest.mark.parametrize("error", ["window_gone", "not_found"])
async def test_watch_for_unprompted_windows_survives_a_failed_action(
    sock_path, event_sock_path, caplog, error
):
    """A click that fails on one window (closed first, stale
    label) is logged; the next window is still handled."""
    clicks = []

    def responder(request):
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
        if request.get("cmd") == "click":
            clicks.append(request.get("window_id"))
            if request.get("window_id") == "w1":
                return {"ok": False, "error": error, "detail": "x"}
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
            await asyncio.sleep(0.05)
            with caplog.at_level(logging.WARNING, logger="ibcontroller.recognisers"):
                dispatcher._window_events.emit(_event("Warning", "w1"))
                dispatcher._window_events.emit(_event("Warning", "w2"))
                await asyncio.sleep(0.3)
            assert not watcher.done()
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
        finally:
            await dispatcher.stop()

    assert clicks == ["w1", "w2"]
    assert any("could not handle window" in message for message in caplog.messages)


async def test_watch_for_unprompted_windows_propagates_a_login_outcome(
    sock_path, event_sock_path
):
    """The boundary catches agent errors only: a login-outcome signal still
    ends the watcher (L6 relies on it)."""

    def responder(request):
        if request.get("cmd") == "dump":
            return {
                "ok": True,
                "components": [
                    {
                        "class": "javax.swing.JTextPane",
                        "text": "<html>Connection to server failed: x</html>",
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
                [GatewayConnectionFailedRecognizer(LABELS.gateway_connection_failed)]
            )
            watcher = asyncio.ensure_future(
                watch_for_unprompted_windows(registry, dispatcher)
            )
            await asyncio.sleep(0.05)
            dispatcher._window_events.emit(_event("Gateway", "w1"))
            with pytest.raises(TransientLoginError):
                await asyncio.wait_for(watcher, timeout=2.0)
        finally:
            await dispatcher.stop()


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


async def test_too_many_failed_login_attempts_disabled_raises_login_failed(
    sock_path, event_sock_path
):
    """This state won't resolve itself, so it must stop the
    container (77), not wait out `mfa_timeout` and exit 69 (retry)."""
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
            with pytest.raises(CredentialsRejectedError):
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


_GATEWAY_FAILED_TEXT = [
    _component(
        class_="javax.swing.JTextPane",
        text="<html>Connection to server failed: some reason</html>",
    )
]
_TOO_MANY_TEXT = [
    _component(text="Too many failed login attempts. Please wait 1 minute & 2 seconds")
]


@pytest.mark.parametrize(
    "case",
    [
        (
            lambda: LoginFailedRecognizer(LABELS.login_failed),
            "Login failed",
            [],
            TransientLoginError,
        ),
        (
            lambda: LoginErrorRecognizer(LABELS.login_error),
            "Login Error",
            [],
            TransientLoginError,
        ),
        (
            lambda: NotCurrentlyAvailableRecognizer(LABELS.not_currently_available),
            "Login",
            [_component(text="The system is not currently available.")],
            TransientLoginError,
        ),
        (
            lambda: UnrecognizedCredentialsRecognizer(LABELS.unrecognized_credentials),
            "Unrecognized Username or Password",
            [],
            CredentialsRejectedError,
        ),
        (
            lambda: GatewayConnectionFailedRecognizer(LABELS.gateway_connection_failed),
            "Gateway",
            _GATEWAY_FAILED_TEXT,
            TransientLoginError,
        ),
    ],
)
@pytest.mark.parametrize("error", ["window_gone", "not_found"])
async def test_failed_dismiss_still_raises_the_classification(
    sock_path, event_sock_path, caplog, case, error
):
    build, title, components, expected = case
    async with (
        FakeCommandServer(
            sock_path, lambda _req: {"ok": False, "error": error, "detail": "x"}
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with (
                caplog.at_level(logging.WARNING, logger="ibcontroller.recognisers"),
                pytest.raises(expected),
            ):
                await build().handle(_event(title, "w1"), components, dispatcher)
        finally:
            await dispatcher.stop()
    assert any("dismiss" in message for message in caplog.messages)


async def test_too_many_failed_login_attempts_failed_dismiss_still_schedules(
    sock_path, event_sock_path
):
    scheduled: list[float] = []
    recognizer = TooManyFailedLoginAttemptsRecognizer(
        LABELS.too_many_failed_login_attempts,
        relogin_enabled=True,
        schedule_retry=scheduled.append,
    )
    async with (
        FakeCommandServer(
            sock_path,
            lambda _req: {"ok": False, "error": "window_gone", "detail": "x"},
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await recognizer.handle(_event("Warning", "w1"), _TOO_MANY_TEXT, dispatcher)
        finally:
            await dispatcher.stop()
    assert scheduled == [65.0]


def test_build_registry_contains_declarative_rules_and_hand_written_built_ins():
    config = Config(
        program=Program.GATEWAY,
        tws_version="10.50",
        trading_mode=TradingMode.PAPER,
        userid=Secret("u"),
        password=Secret("p"),
        log_dir=f"{gettempdir()}/log",
    )
    # A plain stub is enough: the registry only reads `SessionState`
    session = types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: False)

    registry = build_default_registry(
        config,
        LABELS,
        session,  # type: ignore[arg-type]
        lambda _wait: None,
    )
    declarative = [
        r for r in registry._registry if isinstance(r, DeclarativeDismissRecognizer)
    ]
    hand_written_types = {
        type(r)
        for r in registry._registry
        if not isinstance(r, DeclarativeDismissRecognizer)
    }
    # Declarative rules sit after the signal recognisers, before the rest.
    outcomes = len(_SIGNAL_TYPES)
    assert all(isinstance(r, _SIGNAL_TYPES) for r in registry._registry[:outcomes])
    assert registry._registry[outcomes : outcomes + len(declarative)] == declarative
    assert {r.rule.name for r in declarative} == {
        rule.name for rule in LABELS.dismiss_rules
    }
    assert hand_written_types == {
        ExistingSessionRecognizer,
        ReloginRecognizer,
        TradingLoginHandoffRecognizer,
        AcceptIncomingConnectionsRecognizer,
        NotCurrentlyAvailableRecognizer,
        LoginFailedRecognizer,
        LoginErrorRecognizer,
        UnrecognizedCredentialsRecognizer,
        GatewayConnectionFailedRecognizer,
        ExitProgressRecognizer,
        TooManyFailedLoginAttemptsRecognizer,
    }


_SIGNAL_TYPES = (
    NotCurrentlyAvailableRecognizer,
    LoginFailedRecognizer,
    LoginErrorRecognizer,
    UnrecognizedCredentialsRecognizer,
    GatewayConnectionFailedRecognizer,
    TooManyFailedLoginAttemptsRecognizer,
    ExitProgressRecognizer,
)


def test_declarative_rule_does_not_shadow_a_login_outcome_recogniser():
    """A rule matching a login-outcome title would dismiss the dialog before
    the outcome recogniser raises, turning a credential rejection into a
    timeout."""
    config = Config(
        program=Program.GATEWAY,
        tws_version="10.50",
        trading_mode=TradingMode.PAPER,
        userid=Secret("u"),
        password=Secret("p"),
        log_dir=f"{gettempdir()}/log",
    )
    rule = DismissRule(
        name="user-rule", match_title="Unrecognized Username", click="OK"
    )
    labels = attrs.evolve(LABELS, dismiss_rules=[*LABELS.dismiss_rules, rule])
    registry = build_default_registry(
        config,
        labels,
        types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: False),  # type: ignore[arg-type]
        lambda _s: None,
    )
    event = _event("Unrecognized Username or Password")
    matched = next(r for r in registry._registry if r.recognises(event, []))
    assert isinstance(matched, UnrecognizedCredentialsRecognizer)


def test_declarative_rule_does_not_shadow_the_exit_progress_recogniser():
    """A rule matching an exit progress title would stop dispatch before the
    exit is classified, so the exit that follows would go unrecognised."""
    config = Config(
        program=Program.GATEWAY,
        tws_version="10.50",
        trading_mode=TradingMode.PAPER,
        userid=Secret("u"),
        password=Secret("p"),
        log_dir=f"{gettempdir()}/log",
    )
    rule = DismissRule(name="user-rule", match_title="Restart in progress", click="OK")
    labels = attrs.evolve(LABELS, dismiss_rules=[*LABELS.dismiss_rules, rule])
    registry = build_default_registry(
        config,
        labels,
        types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: False),  # type: ignore[arg-type]
        lambda _s: None,
    )
    event = _event("Restart in progress")
    matched = next(r for r in registry._registry if r.recognises(event, []))
    assert isinstance(matched, ExitProgressRecognizer)


def test_build_registry_binds_session_and_schedule_retry():
    """`is_logged_in` is read from the session at match time; the retry hook
    is the callable passed in."""
    retries: list[float] = []
    session = types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: True)
    config = Config(
        program=Program.GATEWAY,
        tws_version="10.50",
        trading_mode=TradingMode.PAPER,
        userid=Secret("u"),
        password=Secret("p"),
        log_dir=f"{gettempdir()}/log",
    )
    registry = build_default_registry(
        config,
        LABELS,
        session,  # type: ignore[arg-type]
        retries.append,
    )
    existing = next(
        r for r in registry._registry if isinstance(r, ExistingSessionRecognizer)
    )
    retry = next(
        r
        for r in registry._registry
        if isinstance(r, TooManyFailedLoginAttemptsRecognizer)
    )
    assert existing._is_logged_in is session.is_logged_in
    assert retry._schedule_retry == retries.append


def test_exit_progress_recognizer_classifies_by_title():
    recognizer = ExitProgressRecognizer(load_labels().shutdown)

    assert recognizer.kind_of(_event("Shutdown progress")) is ExitKind.SHUTDOWN
    assert recognizer.kind_of(_event("Restart in progress")) is ExitKind.RESTART
    assert recognizer.kind_of(_event("Something else")) is None
    assert recognizer.recognises(_event("Restart in progress"), [])
    assert not recognizer.recognises(_event("Something else"), [])
