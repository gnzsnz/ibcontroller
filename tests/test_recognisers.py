"""Unit tests for recognisers.py (L5) -- no live Gateway needed, same fake-socket-server
pattern actions.py's own tests use."""

from __future__ import annotations

import asyncio
import logging
import types
from tempfile import gettempdir

import pytest
from typed_settings.types import Secret

from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    Component,
    ElementNotFoundError,
    WindowEvent,
    WindowInfo,
)
from ibcontroller.config import (
    Config,
    ExistingSessionAction,
    Program,
    TradingMode,
)
from ibcontroller.dialogs import (
    ClickStep,
    DialogEntry,
    MatchGroup,
    load_catalog,
    resolve_dialogs,
)
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import load_labels
from ibcontroller.logging_setup import configure_logging, stop_logging
from ibcontroller.login import (
    CredentialsRejectedError,
    InvalidSessionStateError,
    SessionCededError,
    TransientLoginError,
)
from ibcontroller.recognisers import (
    DialogRecognizer,
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
    build_default_registry,
    handle_window_opened,
    is_credential_entry,
    watch_for_unprompted_windows,
)
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio

LABELS = load_labels()
CATALOG = load_catalog()
CHOICES = resolve_dialogs(CATALOG, (), {}, {})


def _catalog_recognizer(name: str) -> DialogRecognizer:
    """The bundled catalog entry `name`, in its default mode."""
    entry = next(e for e in CATALOG.dialog if e.name == name)
    return DialogRecognizer(entry, entry.default)


def _simple(
    *, click: str | list[str] = "OK", title: str | None = None, text: str | None = None
) -> DialogRecognizer:
    """A one-group, one-click dialog entry in its only mode."""
    entry = DialogEntry(
        name="test_entry",
        default="on",
        match=[MatchGroup(title=title, text=text)],
        modes={"on": [ClickStep(click)]},
    )
    return DialogRecognizer(entry, "on")


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


def test_simple_dialog_recognises_by_text_via_accessible_name():
    """The field this data lands in: `ComponentLookup.describe()`'s `text` field
    only ever comes from `JTextComponent`, and `JLabel` isn't one, so a
    label's displayed text never reaches `component.text` at all. It surfaces
    via `accessible_name` instead (Swing's default `AccessibleJLabel` behavior)."""
    recognizer = _catalog_recognizer("non_brokerage_account")
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


def test_simple_dialog_matches_a_phrase_spanning_an_html_tag():
    """IB message panes are HTML: a phrase copied from the screen can span a
    `<br>`/`<b>` that the raw text carries between its words."""
    recognizer = _catalog_recognizer("non_brokerage_account")
    words = "This is not a brokerage account".split(" ", 1)
    markup = f"<html>{words[0]}<br> <b>{words[1]}</b></html>"

    assert recognizer.recognises(_event("IBKR Gateway"), [_component(text=markup)])


def test_simple_dialog_matches_a_phrase_holding_an_html_entity():
    """From a live 2FA dialog dump: the pane holds `&quot;` where the screen
    shows a quote, and a `<br>` mid-sentence."""
    rule = _simple(text='go to "authenticate" from the services screen', click="OK")
    markup = (
        "<html><body>In the app go to &quot;authenticate&quot; from the "
        "services<br>screen.</body></html>"
    )

    assert rule.recognises(_event("2FA"), [_component(text=markup)])


def test_simple_dialog_also_matches_via_text_field():
    """Robustness for a hypothetical future Gateway version rendering this as an
    actual text component instead of a plain label."""
    recognizer = _catalog_recognizer("non_brokerage_account")
    event = _event("IBKR Gateway")
    matching = [_component(text="This is not a brokerage account -- read carefully")]

    assert recognizer.recognises(event, matching) is True


def test_simple_dialog_recognises_by_title():
    rule = _simple(click="OK", title="Login failed")
    recognizer = rule
    assert recognizer.recognises(_event("Login failed"), []) is True
    assert recognizer.recognises(_event("IBKR Gateway"), []) is False


def test_simple_dialog_title_match_ignores_case():
    rule = _simple(click="OK", title="Order Preview")
    recognizer = rule
    assert recognizer.recognises(_event("ORDER PREVIEW (paper)"), []) is True
    assert recognizer.recognises(_event("order preview"), []) is True


def test_simple_dialog_text_match_ignores_case():
    rule = _simple(click="OK", text="Blind Trading")
    recognizer = rule
    assert recognizer.recognises(
        _event("Any"), [_component(text="Warning: BLIND TRADING is on")]
    )
    assert recognizer.recognises(
        _event("Any"), [_component(accessible_name="blind trading")]
    )


def test_simple_dialog_text_match_ignores_case_after_html_stripping():
    rule = _simple(click="OK", text='are you "sure"')
    markup = "<html><body>ARE<br>YOU &quot;SURE&quot;?</body></html>"
    assert rule.recognises(_event("Any"), [_component(text=markup)])


def test_simple_dialog_case_insensitive_match_still_rejects_other_text():
    rule = _simple(click="OK", text="Blind Trading")
    assert not rule.recognises(_event("Any"), [_component(text="something unrelated")])


def test_simple_dialog_requires_both_when_both_given():
    rule = _simple(click="OK", title="Warning", text="specific text")
    recognizer = rule
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


async def test_simple_dialog_handle_clicks_configured_button(
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
            recognizer = _catalog_recognizer("non_brokerage_account")
            await recognizer.handle(_event("IBKR Gateway"), [], dispatcher)
        finally:
            await dispatcher.stop()

    assert seen["target"] == "I understand and accept"


async def test_simple_dialog_handle_raises_when_no_candidate_is_found(
    sock_path, event_sock_path
):
    rule = _simple(click=["a", "b"], text="x")
    async with (
        FakeCommandServer(
            sock_path,
            lambda _req: {"ok": False, "error": "not_found", "detail": "gone"},
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(ElementNotFoundError):
                await rule.handle(_event("Any"), [], dispatcher)
        finally:
            await dispatcher.stop()


async def test_simple_dialog_handle_scopes_click_to_the_recognised_window(
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
            recognizer = _catalog_recognizer("non_brokerage_account")
            await recognizer.handle(
                _event("IBKR Gateway", window_id="w7"), [], dispatcher
            )
        finally:
            await dispatcher.stop()

    assert seen["window_id"] == "w7"


async def test_simple_dialog_handle_logs_with_rule_name(
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
            recognizer = _catalog_recognizer("non_brokerage_account")
            await recognizer.handle(_event("IBKR Gateway"), [], dispatcher)
        finally:
            await dispatcher.stop()

    # Records reach the file via configure_logging's listener thread;
    # draining it is what guarantees they've been written before we read.
    stop_logging()
    log_text = (tmp_path / "test.log").read_text()
    assert "handled dialog non_brokerage_account (mode on)" in log_text


def _dialog(**overrides) -> DialogEntry:
    base = dict(
        name="blind_trading",
        default="on",
        match=[
            MatchGroup(title="Order Preview", text="blind trading"),
            MatchGroup(text="blind trading", text_also="Are you sure"),
        ],
        modes={"off": [], "on": [ClickStep(["Override and Transmit", "Yes"])]},
    )
    base.update(overrides)
    return DialogEntry(**base)


def test_dialog_recogniser_matches_any_group():
    recognizer = DialogRecognizer(_dialog(), "on")
    first = [_component(text="This is blind trading")]
    assert recognizer.recognises(_event("Order Preview"), first) is True
    # Second group: no title condition, two text conditions.
    second = [
        _component(text="blind trading"),
        _component(accessible_name="Are you sure you want to submit this order?"),
    ]
    assert recognizer.recognises(_event("Whatever"), second) is True


def test_dialog_recogniser_requires_every_field_in_a_group():
    recognizer = DialogRecognizer(_dialog(), "on")
    # Title matches, text does not: first group fails; second needs two texts.
    assert (
        recognizer.recognises(
            _event("Order Preview"), [_component(text="something else")]
        )
        is False
    )
    # First text condition only: second group fails on `text_also`.
    assert (
        recognizer.recognises(_event("Whatever"), [_component(text="blind trading")])
        is False
    )


def test_dialog_recogniser_matching_ignores_case_and_html():
    recognizer = DialogRecognizer(_dialog(), "on")
    markup = "<html><body>BLIND<br>TRADING warning</body></html>"
    assert recognizer.recognises(_event("ORDER preview"), [_component(text=markup)])


def test_dialog_recogniser_in_a_mode_without_steps_matches_nothing():
    recognizer = DialogRecognizer(_dialog(), "off")
    components = [_component(text="This is blind trading")]
    assert recognizer.recognises(_event("Order Preview"), components) is False


def test_dialog_recogniser_rejects_an_unknown_mode():
    with pytest.raises(ValueError, match="maybe"):
        DialogRecognizer(_dialog(), "maybe")


async def test_dialog_recogniser_handle_runs_the_mode_steps_in_the_window(
    sock_path, event_sock_path
):
    seen = []

    def responder(request):
        seen.append((request["target"], request.get("window_id")))
        if request["target"] == "Override and Transmit":
            return {"ok": False, "error": "not_found", "detail": "no such button"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            recognizer = DialogRecognizer(_dialog(), "on")
            await recognizer.handle(
                _event("Order Preview", window_id="w9"), [], dispatcher
            )
        finally:
            await dispatcher.stop()

    assert seen == [("Override and Transmit", "w9"), ("Yes", "w9")]


async def test_dialog_recogniser_handle_logs_entry_and_mode(
    sock_path, event_sock_path, tmp_path
):
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await DialogRecognizer(_dialog(), "on").handle(
                _event("Order Preview"), [], dispatcher
            )
        finally:
            await dispatcher.stop()

    stop_logging()
    assert (
        "handled dialog blind_trading (mode on)" in (tmp_path / "test.log").read_text()
    )


async def test_dialog_recogniser_failed_step_logs_the_entry_and_reraises(
    sock_path, event_sock_path, tmp_path
):
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(
            sock_path,
            lambda _req: {"ok": False, "error": "not_found", "detail": "no button"},
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(ElementNotFoundError):
                await DialogRecognizer(_dialog(), "on").handle(
                    _event("Order Preview"), [], dispatcher
                )
        finally:
            await dispatcher.stop()

    stop_logging()
    log_text = (tmp_path / "test.log").read_text()
    assert "dialog blind_trading (mode on) failed" in log_text
    assert "handled dialog" not in log_text


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
        CHOICES,
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


def _accept_recognizer(mode: str) -> DialogRecognizer:
    entry = next(e for e in CATALOG.dialog if e.name == "accept_incoming_connections")
    return DialogRecognizer(entry, mode)


async def test_accept_incoming_connection_recognises_by_message_text():
    """The window title is not the phrase; the dialog's label is, with the
    client address in it."""
    recognizer = _accept_recognizer("accept")
    event = _event("IBKR Gateway")
    prompt = _component(text="Accept incoming connection attempt from 10.0.0.5?")
    assert recognizer.recognises(event, [prompt]) is True
    assert recognizer.recognises(event, [_component(text="ACCEPT INCOMING CONNECTION")])
    assert recognizer.recognises(event, [_component(text="something else")]) is False
    assert recognizer.recognises(_event("Accept incoming connection"), []) is False


async def test_accept_incoming_connection_manual_leaves_the_dialog_unrecognised():
    recognizer = _accept_recognizer("manual")
    prompt = _component(text="Accept incoming connection attempt?")
    assert recognizer.recognises(_event("IBKR Gateway"), [prompt]) is False


async def _run_accept(sock_path, event_sock_path, mode: str, respond) -> list:
    seen = []
    async with (
        FakeCommandServer(
            sock_path, lambda req: (seen.append(req["target"]), respond(req))[1]
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await _accept_recognizer(mode).handle(
                _event("Accept incoming connection"), [], dispatcher
            )
        finally:
            await dispatcher.stop()
    return seen


async def test_accept_incoming_connection_manual_does_nothing(
    sock_path, event_sock_path
):
    seen = await _run_accept(
        sock_path, event_sock_path, "manual", lambda r: {"ok": True}
    )
    assert seen == []


async def test_accept_incoming_connection_accept_clicks_ok(sock_path, event_sock_path):
    seen = await _run_accept(
        sock_path, event_sock_path, "accept", lambda r: {"ok": True}
    )
    assert seen == ["OK"]


async def test_accept_incoming_connection_accept_falls_back_to_yes(
    sock_path, event_sock_path
):
    def respond(req):
        if req["target"] == "OK":
            return {"ok": False, "error": "not_found", "detail": "no such button"}
        return {"ok": True}

    seen = await _run_accept(sock_path, event_sock_path, "accept", respond)
    assert seen == ["OK", "Yes"]


async def test_accept_incoming_connection_reject_clicks_no(sock_path, event_sock_path):
    seen = await _run_accept(
        sock_path, event_sock_path, "reject", lambda r: {"ok": True}
    )
    assert seen == ["No"]


def _crypto_recognizer(mode: str) -> DialogRecognizer:
    entry = next(e for e in CATALOG.dialog if e.name == "crypto_order_confirmation")
    return DialogRecognizer(entry, mode)


async def test_crypto_order_confirmation_recognises_by_title():
    event = _event("Cryptocurrency Order Confirmation")
    assert _crypto_recognizer("transmit").recognises(event, []) is True
    assert _crypto_recognizer("cancel").recognises(event, []) is True
    assert (
        _crypto_recognizer("transmit").recognises(_event("IBKR Gateway"), []) is False
    )


async def test_crypto_order_confirmation_manual_leaves_the_dialog_unrecognised():
    event = _event("Cryptocurrency order confirmation")
    assert _crypto_recognizer("manual").recognises(event, []) is False


@pytest.mark.parametrize(
    ("mode", "button"), [("transmit", "Transmit"), ("cancel", "Cancel")]
)
async def test_crypto_order_confirmation_clicks_the_mode_button(
    sock_path, event_sock_path, mode, button
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
            await _crypto_recognizer(mode).handle(
                _event("Cryptocurrency order confirmation"), [], dispatcher
            )
        finally:
            await dispatcher.stop()
    assert seen == [button]


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
                [_catalog_recognizer("non_brokerage_account")]
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
                [_catalog_recognizer("non_brokerage_account")]
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
                [_catalog_recognizer("non_brokerage_account")]
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
                [_catalog_recognizer("non_brokerage_account")]
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
                        "accessible_name": "This is not a brokerage account",
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
                [_catalog_recognizer("non_brokerage_account")]
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
                        "accessible_name": "This is not a brokerage account",
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
                [_catalog_recognizer("non_brokerage_account")]
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
    recognizer = _catalog_recognizer("auto_restart_confirmation")
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
    recognizer = _catalog_recognizer("auto_restart_confirmation")
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

    recognizer = _catalog_recognizer("auto_restart_confirmation")
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
        CHOICES,
        session,  # type: ignore[arg-type]
        lambda _wait: None,
    )
    declarative = [r for r in registry._registry if isinstance(r, DialogRecognizer)]
    hand_written_types = {
        type(r) for r in registry._registry if not isinstance(r, DialogRecognizer)
    }
    # Catalog entries sit after the signal recognisers, before the rest.
    outcomes = len(_SIGNAL_TYPES)
    assert all(isinstance(r, _SIGNAL_TYPES) for r in registry._registry[:outcomes])
    assert registry._registry[outcomes : outcomes + len(declarative)] == declarative
    assert [(r.entry.name, r.mode) for r in declarative] == [
        (entry.name, entry.default) for entry in CATALOG.dialog
    ]
    assert hand_written_types == {
        ExistingSessionRecognizer,
        ReloginRecognizer,
        TradingLoginHandoffRecognizer,
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
    shadow = DialogEntry(
        name="shadow",
        default="on",
        match=[MatchGroup(title="Unrecognized Username")],
        modes={"on": [ClickStep("OK")]},
    )
    choices = resolve_dialogs(CATALOG, [shadow], {}, {})
    registry = build_default_registry(
        config,
        LABELS,
        choices,
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
    shadow = DialogEntry(
        name="shadow",
        default="on",
        match=[MatchGroup(title="Restart in progress")],
        modes={"on": [ClickStep("OK")]},
    )
    choices = resolve_dialogs(CATALOG, [shadow], {}, {})
    registry = build_default_registry(
        config,
        LABELS,
        choices,
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
        CHOICES,
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


@pytest.mark.parametrize(
    ("recognizer", "title"),
    [
        (LoginFailedRecognizer(LABELS.login_failed), LABELS.login_failed.title),
        (LoginErrorRecognizer(LABELS.login_error), LABELS.login_error.title),
        (
            UnrecognizedCredentialsRecognizer(LABELS.unrecognized_credentials),
            LABELS.unrecognized_credentials.title,
        ),
        (
            ExistingSessionRecognizer(
                LABELS.existing_session, ExistingSessionAction.MANUAL, lambda: False
            ),
            LABELS.existing_session.title,
        ),
        (
            ReloginRecognizer(LABELS.relogin, ExistingSessionAction.PRIMARY),
            LABELS.relogin.title,
        ),
        (
            TradingLoginHandoffRecognizer(
                LABELS.trading_login_handoff, ExistingSessionAction.PRIMARY
            ),
            LABELS.trading_login_handoff.title,
        ),
    ],
)
def test_title_recognisers_ignore_title_case(recognizer, title):
    assert recognizer.recognises(_event(title.upper()), []) is True
    assert recognizer.recognises(_event(title.lower()), []) is True


def test_gateway_connection_failed_ignores_title_and_prefix_case():
    recognizer = GatewayConnectionFailedRecognizer(LABELS.gateway_connection_failed)
    prefix = LABELS.gateway_connection_failed.message_prefix
    title = LABELS.gateway_connection_failed.title
    anchored = [_component(text=f"<html>{prefix.upper()} some reason</html>")]
    mid_text = [_component(text=f"<html>Hello {prefix.upper()} x</html>")]

    assert recognizer.recognises(_event(title.upper()), anchored) is True
    assert recognizer.recognises(_event(title), mid_text) is False


async def test_gateway_connection_failed_known_reason_ignores_case(
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
            reason = LABELS.gateway_connection_failed.known_reasons[0]
            prefix = LABELS.gateway_connection_failed.message_prefix
            components = [_component(text=f"<html>{prefix} {reason.upper()}</html>")]
            with pytest.raises(CredentialsRejectedError):
                await recognizer.handle(_event("Gateway"), components, dispatcher)
        finally:
            await dispatcher.stop()


def test_too_many_failed_login_attempts_ignores_prefix_case():
    recognizer = TooManyFailedLoginAttemptsRecognizer(
        LABELS.too_many_failed_login_attempts,
        relogin_enabled=True,
        schedule_retry=lambda _wait: None,
    )
    prefix = LABELS.too_many_failed_login_attempts.message_prefix.upper()
    event = _event("Warning")
    assert recognizer.recognises(event, [_component(text=f"{prefix} x")]) is True
    assert (
        recognizer.recognises(event, [_component(accessible_name=f"{prefix} x")])
        is True
    )


def test_exit_progress_recognizer_ignores_title_case():
    recognizer = ExitProgressRecognizer(LABELS.shutdown)
    assert recognizer.kind_of(_event("SHUTDOWN PROGRESS")) is ExitKind.SHUTDOWN
    assert recognizer.kind_of(_event("restart in progress")) is ExitKind.RESTART
