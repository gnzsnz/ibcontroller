"""Unit tests for login.py (L6) -- no live Gateway needed, same fake-socket-server
pattern the rest of this project's tests use. Multi-step scenarios (2FA,
existing-session) drive `Dispatcher._window_events` directly with controlled
timing (`asyncio.sleep(0)` between emits), matching actions.py's own established
pattern for this -- avoids any race with a fake event *socket* server that would
otherwise dump every scripted message at once, well before a later step in the
sequence has its own listener connected.

**`LoginManager` takes no registry** -- reacting to existing-session/login-failed/
non-brokerage dialogs is entirely `recognisers.watch_for_unprompted_windows`'s job,
run as a separate concurrent task. The tests for those scenarios run both
`LoginManager.run()` and the watcher together, matching how they're actually meant
to be wired -- see `test_login_failed_is_raised_by_the_watcher_not_by_run` for why
`CredentialsRejectedError` no longer propagates from `run()` itself."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from tempfile import gettempdir

import attrs
import pytest
from typed_settings.types import Secret

from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    WindowEvent,
    WindowGoneError,
    WindowInfo,
)
from ibcontroller.config import Config, ExistingSessionAction, Program, TradingMode
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import load_labels
from ibcontroller.login import (
    LoginFrameTimeoutError,
    LoginManager,
    LoginState,
    MfaTimeoutError,
)
from ibcontroller.recognisers import (
    DeclarativeDismissRecognizer,
    ExistingSessionRecognizer,
    LoginFailedRecognizer,
    RecognizerRegistry,
    TooManyFailedLoginAttemptsRecognizer,
    TransientLoginError,
    watch_for_unprompted_windows,
)
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio

LABELS = load_labels()
_NON_BROKERAGE_RULE = next(
    r for r in LABELS.dismiss_rules if r.name == "non_brokerage_account"
)


def _config(**overrides) -> Config:
    base = Config(
        program=Program.GATEWAY,
        tws_version="10.50",
        trading_mode=TradingMode.PAPER,
        userid=Secret("u"),
        password=Secret("p"),
        log_dir=f"{gettempdir()}/log",
    )
    return attrs.evolve(base, **overrides)


def _registry(is_logged_in) -> RecognizerRegistry:
    return RecognizerRegistry(
        [
            ExistingSessionRecognizer(
                LABELS.existing_session, ExistingSessionAction.PRIMARY, is_logged_in
            ),
            LoginFailedRecognizer(LABELS.login_failed),
            DeclarativeDismissRecognizer(_NON_BROKERAGE_RULE),
        ]
    )


async def _start(cmd_sock, event_sock) -> Dispatcher:
    dispatcher = Dispatcher(
        AgentCommandConnection(cmd_sock), AgentEventConnection(event_sock)
    )
    await dispatcher.start()
    return dispatcher


def _window_event(
    seq: int, kind: str, class_: str, title: str, window_id: str | None = None
) -> WindowEvent:
    window = WindowInfo(class_=class_, title=title, window_id=window_id)
    return WindowEvent(seq=seq, kind=kind, window=window)


# Gateway's real login-completion signal (`login.py`'s `_wait_for_outcome_gateway`,
# ported from IBC's `SplashFrameHandler`) -- a `window_closed` event whose title has
# already relabelled to this by the time it fires.
# The class is arbitrary (matching is by title only, same as IBC's own
# `titleContains` check) -- this mirrors the real one.
_SPLASH_CLASS = "twslaunch.feature.welcome.C"


def _splash_closed_event(seq: int) -> WindowEvent:
    return _window_event(seq, "window_closed", _SPLASH_CLASS, "Starting application...")


def _tracking_responder(
    calls: list[dict],
    *,
    menu_item_exists: bool = True,
    not_found_cmds: frozenset[str] = frozenset(),
):
    """`dump` needs a `components` key (empty is fine -- these tests recognise
    existing-session/login-failed by event title, not component content);
    `menu_item_exists` needs `exists`/`enabled` keys (`login.py`'s TWS
    main-window detection -- default `True` matches every
    existing TWS test's own fed "main window" event); everything else just
    needs `{"ok": True}`. Commands in `not_found_cmds` answer `not_found`."""

    def responder(request):
        calls.append(request)
        if request.get("cmd") in not_found_cmds:
            return {"ok": False, "error": "not_found", "detail": "no such component"}
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
        if request.get("cmd") == "get_text":
            return {"ok": True, "value": ""}
        if request.get("cmd") == "menu_item_exists":
            return {"ok": True, "exists": menu_item_exists, "enabled": menu_item_exists}
        return {"ok": True}

    return responder


async def _feed(dispatcher: Dispatcher, events: list[WindowEvent]) -> None:
    """Lets the currently-running task(s) reach their own next wait/
    watcher filter-connection point before each emit."""
    for event in events:
        await asyncio.sleep(0.01)
        dispatcher._window_events.emit(event)


# --- LoginManager, on its own (no unprompted dialogs) ------------------------


async def test_happy_path_fills_credentials_and_reaches_logged_in(
    sock_path, event_sock_path, tmp_path
):
    calls: list[dict] = []
    responder = _tracking_responder(calls)

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(2, "window_opened", "ibgateway.aw", "IBKR Gateway"),
                    _splash_closed_event(3),
                ],
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    sent_targets = [c.get("target") for c in calls if "target" in c]
    assert "IB API" in sent_targets
    assert "Paper Trading" in sent_targets
    assert "Username" in sent_targets
    assert "Password" in sent_targets
    login_candidates = {"Log In", "Paper Log In", "Login"}
    assert login_candidates & set(sent_targets)


async def test_fill_credentials_scopes_every_action_to_the_login_frame(
    sock_path, event_sock_path, tmp_path
):
    """Regression test: the login frame's FIX Login and IB API sections both carry
    Username/Password pairs with identical accessible names at once. Every
    click/set_text `_fill_credentials` sends should now be scoped to the
    login frame's own `window_id`, captured from the event that recognised
    it -- not left to an unscoped, whole-JVM search."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(
                        1,
                        "window_opened",
                        "ibgateway.ax",
                        "IBKR Gateway",
                        window_id="w1",
                    ),
                    _window_event(2, "window_opened", "ibgateway.aw", "IBKR Gateway"),
                    _splash_closed_event(3),
                ],
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    window_ids = {c.get("window_id") for c in calls if "target" in c}
    assert window_ids == {"w1"}


async def test_restart_expected_true_skips_fill_even_with_no_marker_file(
    sock_path, event_sock_path, tmp_path
):
    """Gateway can delete the marker quickly as part of a
    successful silent relogin, so a check done *after* relaunch can find it
    already gone. `control_loop.py` passes the answer it had before
    relaunching instead; LoginManager never checks the disk."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(),
            LABELS,
            dispatcher,
            restart_expected=True,
        )
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(2, "window_opened", "ibgateway.aw", "IBKR Gateway"),
                    _splash_closed_event(3),
                ],
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    assert calls == []


async def test_restart_expected_false_fills_credentials(
    sock_path, event_sock_path, tmp_path
):
    """The other direction: a launch the caller knows is not a restart fills
    credentials. LoginManager never looks at the marker on disk, so a stale
    one cannot make a fresh launch skip them."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(),
            LABELS,
            dispatcher,
            restart_expected=False,
        )
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(2, "window_opened", "ibgateway.aw", "IBKR Gateway"),
                    _splash_closed_event(3),
                ],
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    sent_targets = [c.get("target") for c in calls if "target" in c]
    assert "Username" in sent_targets
    assert "Password" in sent_targets


async def test_mfa_is_handled_inline_then_reaches_logged_in(
    sock_path, event_sock_path, tmp_path
):
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(
                        2,
                        "window_opened",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                    _window_event(
                        3,
                        "window_closed",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                    _window_event(4, "window_opened", "ibgateway.aw", "IBKR Gateway"),
                    _splash_closed_event(5),
                ],
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN


async def test_login_frame_never_appears_raises_login_frame_timeout_error(
    sock_path, event_sock_path, tmp_path
):
    """`LoginFrameTimeoutError`, not the generic `LoginError` -- matches IBC's
    own error code 1112 and is what `control_loop.run_control_loop` matches
    on to always relaunch, regardless of any config."""
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        try:
            with pytest.raises(LoginFrameTimeoutError):
                await manager.run(login_timeout=0.2)
        finally:
            await dispatcher.stop()


# --- LoginManager, unprompted dialogs handled by the separate watcher --------


async def test_existing_session_handled_by_watcher_while_login_still_waits(
    sock_path, event_sock_path, tmp_path
):
    """`LoginManager.run()`'s own filter
    never captures the "Existing session detected" event at all (it just keeps
    waiting past it) -- the separate, concurrently-running watcher handles it,
    and `run()` only resolves once the main window it's actually watching for
    appears."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        # not logged in -> ExistingSession scenario 3
        registry = _registry(lambda: False)
        watcher = asyncio.ensure_future(
            watch_for_unprompted_windows(registry, dispatcher)
        )
        try:
            run_task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(
                        2,
                        "window_opened",
                        "javax.swing.JDialog",
                        "Existing session detected",
                    ),
                    _window_event(3, "window_opened", "ibgateway.aw", "IBKR Gateway"),
                    _splash_closed_event(4),
                ],
            )
            await asyncio.wait_for(run_task, timeout=5.0)
        finally:
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    # scenario 3 (not logged in, first attempt): continue via OK/Continue Login/...
    sent_targets = [c.get("target") for c in calls if "target" in c]
    assert any(t in LABELS.existing_session.continue_buttons for t in sent_targets)


async def test_login_failed_is_raised_by_the_watcher_not_by_run(
    sock_path, event_sock_path, tmp_path
):
    """`TransientLoginError` ("Login failed" is IB's wording for a server
    disconnect, not
    rejected credentials) never propagates from `LoginManager.run()` (its own
    filter never captures "Login failed" at all) -- it propagates from the
    separate watcher task instead, since that's the only thing left
    dispatching to the registry. `run()` itself just keeps waiting until its
    own timeout."""
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        registry = _registry(lambda: False)
        watcher = asyncio.ensure_future(
            watch_for_unprompted_windows(registry, dispatcher)
        )
        run_task = asyncio.ensure_future(manager.run(login_timeout=5.0))
        try:
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(
                        2, "window_opened", "javax.swing.JDialog", "Login failed"
                    ),
                ],
            )
            with pytest.raises(TransientLoginError):
                await asyncio.wait_for(watcher, timeout=5.0)
            assert manager.state is LoginState.LOGGING_IN  # run() never saw it
        finally:
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task
            await dispatcher.stop()


# --- login/2FA timeout + retry ----------------------------------


async def test_run_uses_config_sourced_timeouts_when_not_overridden(
    sock_path, event_sock_path, tmp_path
):
    """`login_timeout` defaults from `Config.login_dialog_display_timeout`
    (IBC's own setting) when `run()` isn't given an explicit value
    (the test passes no argument)."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            login_dialog_display_timeout=5.0,
            mfa_timeout=5.0,
        )
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        try:
            task = asyncio.ensure_future(manager.run())  # no explicit timeout
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(2, "window_opened", "ibgateway.aw", "IBKR Gateway"),
                    _splash_closed_event(3),
                ],
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN


async def test_mfa_watchdog_succeeds_within_exit_interval(
    sock_path, event_sock_path, tmp_path
):
    """Fast 2FA close (well within `mfa_timeout`)
    with `relogin_after_mfa_timeout` enabled -- the watchdog just bounds the
    rest of the wait by `mfa_exit_interval`; the main
    window still arrives in time, so this behaves exactly like the plain 2FA
    case, just with a shorter timeout applied."""
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            relogin_after_mfa_timeout=True,
            mfa_timeout=5.0,
            mfa_exit_interval=5.0,
        )
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(
                        2,
                        "window_opened",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                    _window_event(
                        3,
                        "window_closed",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                    _window_event(4, "window_opened", "ibgateway.aw", "IBKR Gateway"),
                    _splash_closed_event(5),
                ],
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN


async def test_gateway_mfa_watchdog_raises_when_main_window_never_appears(
    sock_path, event_sock_path, tmp_path
):
    """Ported from IBC's `restartAfterTime` -- IBC exits/restarts the JVM if
    login hasn't completed within `mfa_exit_interval` of 2FA closing. Raises
    `MfaTimeoutError`, distinct from the generic `LoginError` --
    `Config.mfa_timeout_action` decides whether `control_loop.run_control_loop`
    lets this propagate (`exit`) or relaunches on it (`restart`)."""
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            relogin_after_mfa_timeout=True,
            mfa_timeout=5.0,
            mfa_exit_interval=0.05,
        )
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(
                        2,
                        "window_opened",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                    _window_event(
                        3,
                        "window_closed",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                    # no splash-closed event -- the watchdog must time out
                ],
            )
            with pytest.raises(MfaTimeoutError):
                await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()


async def test_tws_mfa_watchdog_raises_when_main_window_never_appears(
    sock_path, event_sock_path, tmp_path
):
    """TWS equivalent of the Gateway watchdog test above -- 2FA closes within
    `mfa_timeout`, but no main window ever appears, so `_wait_for_outcome_tws`'s
    own `mfa_exit_interval` watchdog times out and raises `MfaTimeoutError`."""
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            program=Program.TWS,
            relogin_after_mfa_timeout=True,
            mfa_timeout=5.0,
            mfa_exit_interval=0.05,
        )
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE),
                    _window_event(
                        2,
                        "window_opened",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                    _window_event(
                        3,
                        "window_closed",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                    # no main-window event -- the watchdog must time out
                ],
            )
            with pytest.raises(MfaTimeoutError):
                await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()


async def test_mfa_late_close_relogin_disabled_does_nothing(
    sock_path, event_sock_path, tmp_path
):
    """2FA closes *after* `mfa_timeout` (the user
    answered too slowly) with `relogin_after_mfa_timeout` disabled (the
    default) -- matches IBC's own intent (just a log line, no retry) while
    still keeping `run()`'s own contract: it doesn't return early leaving
    `state` stuck at `MFA_IN_PROGRESS` (a real bug this test caught, fixed
    in `_after_mfa_closed`) -- it keeps
    waiting for the outcome, same as always, just without ever retrying."""
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            relogin_after_mfa_timeout=False,
            mfa_timeout=0.01,
        )
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        task = asyncio.ensure_future(manager.run(login_timeout=5.0))
        try:
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(
                        2,
                        "window_opened",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                ],
            )
            await asyncio.sleep(0.05)  # elapsed now exceeds the tiny threshold
            dispatcher._window_events.emit(
                _window_event(
                    3,
                    "window_closed",
                    "twslaunch.jauthentication.bh",
                    "Second Factor Authentication",
                )
            )
            await asyncio.sleep(0.05)
            assert manager.state is LoginState.MFA_IN_PROGRESS
        finally:
            # `run()` completes normally here (nothing more to do, matching
            # IBC exactly) -- `task` may already be done by this point, so
            # `cancel()` can be a no-op. `contextlib.suppress`, not
            # `pytest.raises(asyncio.CancelledError)`: the latter hangs when
            # the awaited task did *not* raise CancelledError (a
            # pytest/asyncio interaction, not a production bug).
            # `pytest.raises` around `await task` is only safe when
            # the task is genuinely still running and cancellation is certain
            # to raise, as in this file's other cancel-then-await tests.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await dispatcher.stop()


async def test_mfa_late_close_relogin_enabled_retries_then_succeeds(
    sock_path, event_sock_path, tmp_path, monkeypatch
):
    """2FA closes late, `relogin_after_mfa_timeout` enabled -- re-initiates the
    whole login sequence after IBC's own hardcoded 5-second delay (monkeypatched
    down here so the test doesn't need to wait 5 real seconds; the constant
    itself stays literal in production code, matching IBC)."""
    monkeypatch.setattr("ibcontroller.login._IBC_RELOGIN_DELAY_SECONDS", 0.01)
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            relogin_after_mfa_timeout=True,
            mfa_timeout=0.01,
            mfa_exit_interval=5.0,
        )
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        task = asyncio.ensure_future(manager.run(login_timeout=5.0))
        try:
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(
                        2,
                        "window_opened",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                ],
            )
            await asyncio.sleep(0.05)
            dispatcher._window_events.emit(
                _window_event(
                    3,
                    "window_closed",
                    "twslaunch.jauthentication.bh",
                    "Second Factor Authentication",
                )
            )
            # let retry_login's sleep(0.01) elapse, then it re-fills + re-clicks
            await asyncio.sleep(0.1)
            dispatcher._window_events.emit(_splash_closed_event(4))
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    # credentials were genuinely re-submitted, not just the original click
    sent_targets = [c.get("target") for c in calls if c.get("cmd") == "set_text"]
    assert sent_targets.count("Username") == 2


async def test_retry_login_refills_credentials_and_resubmits(
    sock_path, event_sock_path, tmp_path
):
    """`retry_login` in isolation -- mirrors IBC's `initiateLogin(getLoginFrame())`:
    re-fills and re-clicks without waiting for a fresh login-frame
    `window_opened` (there won't be one for an already-open frame), and
    returns without waiting for the outcome."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        # normally set by run()'s own initial wait for the login frame --
        # this test exercises retry_login on its own, bypassing run() entirely.
        manager._login_frame_class = "ibgateway.ax"
        try:
            await asyncio.wait_for(manager.retry_login(0.01), timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGING_IN
    sent_targets = [c.get("target") for c in calls if "target" in c]
    assert "Username" in sent_targets


async def test_schedule_retry_fires_a_background_task(
    sock_path, event_sock_path, tmp_path
):
    calls: list[dict] = []
    async with (
        FakeCommandServer(sock_path, _tracking_responder(calls)),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        manager._login_frame_class = "ibgateway.ax"
        try:
            manager.schedule_retry(0.01)
            assert manager._retry_task is not None  # fire-and-forget, not awaited here
            await asyncio.wait_for(manager._retry_task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGING_IN
    assert "Username" in [c.get("target") for c in calls if "target" in c]


async def test_run_cancels_pending_retry_when_it_ends(sock_path, event_sock_path):
    """A retry still sleeping its cooldown must not outlive `run()`."""
    async with (
        FakeCommandServer(sock_path, _tracking_responder([])),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        task = asyncio.ensure_future(manager.run(login_timeout=5.0))
        try:
            await _feed(
                dispatcher,
                [_window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway")],
            )
            manager.schedule_retry(60.0)
            retry = manager._retry_task
            assert retry is not None
            await asyncio.sleep(0.05)
            assert not retry.done()
            dispatcher._window_events.emit(_splash_closed_event(2))
            await asyncio.wait_for(task, timeout=5.0)
            await asyncio.sleep(0)  # let the cancellation land
            assert retry.cancelled()
        finally:
            await dispatcher.stop()


async def test_schedule_retry_cancels_a_pending_retry(sock_path, event_sock_path):
    async with (
        FakeCommandServer(sock_path, _tracking_responder([])),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        try:
            manager.schedule_retry(60.0)
            first = manager._retry_task
            manager.schedule_retry(60.0)
            await asyncio.sleep(0)
            assert first is not None
            assert first.cancelled()
            assert manager._retry_task is not first
            assert manager._retry_task is not None
            manager._retry_task.cancel()
        finally:
            await dispatcher.stop()


async def test_too_many_failed_attempts_dialog_triggers_retry_via_watcher(
    sock_path, event_sock_path, tmp_path
):
    """The one genuinely cross-task path: the dialog is recognised and
    dismissed by the separate watcher (its own background task), which calls
    `manager.schedule_retry` -- a plain synchronous callback -- while
    `manager.run()`'s own `_wait_for_outcome` is still actively waiting.
    Confirms `retry_login` succeeds independently and converges on the same
    `LOGGED_IN` state `run()` was already waiting for."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            if request.get("window") == "Warning":
                return {
                    "ok": True,
                    "components": [
                        {
                            "class": "javax.swing.JTextArea",
                            "text": (
                                "Too many failed login attempts. Please wait "
                                "0 seconds before attempting to re-login again."
                            ),
                        }
                    ],
                }
            return {"ok": True, "components": []}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(relogin_after_mfa_timeout=True)
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        registry = RecognizerRegistry(
            [
                TooManyFailedLoginAttemptsRecognizer(
                    LABELS.too_many_failed_login_attempts,
                    relogin_enabled=True,
                    schedule_retry=manager.schedule_retry,
                )
            ]
        )
        watcher = asyncio.ensure_future(
            watch_for_unprompted_windows(registry, dispatcher)
        )
        run_task = asyncio.ensure_future(manager.run(login_timeout=5.0))
        try:
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(2, "window_opened", "javax.swing.JDialog", "Warning"),
                ],
            )
            # the watcher dumps/recognises/dismisses/schedules, then
            # retry_login's own sleep(wait_seconds) (parsed: 0+0+3=3s) elapses
            await asyncio.sleep(3.2)
            dispatcher._window_events.emit(_splash_closed_event(3))
            await asyncio.wait_for(run_task, timeout=5.0)
        finally:
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    dismiss_calls = [c for c in calls if c.get("target") == "OK"]
    assert dismiss_calls


# --- TWS path ---------------------------------------------------
#
# `_wait_for_outcome_tws`: main-window-open (same title as the login frame,
# different class) is the terminal signal. Tests default to
# `program=Program.GATEWAY`, so these select `Program.TWS` explicitly.

_TWS_LOGIN_CLASS = "twslaunch.tws.LoginFrame"
_TWS_MAIN_CLASS = "twslaunch.tws.MainFrame"
_TWS_TITLE = "Login"
_MFA_CLASS = "twslaunch.jauthentication.bh"
_MFA_TITLE = "Second Factor Authentication"


async def test_tws_happy_path_fills_credentials_and_reaches_logged_in(
    sock_path, event_sock_path, tmp_path
):
    calls: list[dict] = []
    responder = _tracking_responder(calls)

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(program=Program.TWS), LABELS, dispatcher, restart_expected=False
        )
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE),
                    _window_event(
                        2,
                        "window_opened",
                        _TWS_MAIN_CLASS,
                        _TWS_TITLE,
                        window_id="w-main",
                    ),
                ],
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    assert manager.main_window_id == "w-main"
    sent_targets = [c.get("target") for c in calls if "target" in c]
    assert "Username" in sent_targets
    assert "Password" in sent_targets


async def test_tws_skips_intermediate_window_then_matches_main_window_by_menu_item(
    sock_path, event_sock_path, tmp_path
):
    """Matching the TWS main window by *title* (Gateway's "same title as the
    login frame" heuristic) is wrong: its real main window title is
    account-specific ("DU6351764 Interactive Brokers
    (Simulated Trading)"), and a real run also opens an intermediate window
    ("Downloading settings from server") in between. Reproduces both: an
    intermediate window carrying no File/Lock Application menu item is
    skipped (the loop keeps waiting), and only the window that actually
    carries it (ported from IBC's own MainWindowFrameHandler.recogniseWindow)
    completes login."""

    def responder(request):
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
        if request.get("cmd") == "get_text":
            return {"ok": True, "value": ""}
        if request.get("cmd") == "menu_item_exists":
            has_it = request.get("window_id") == "w-main"
            return {"ok": True, "exists": has_it, "enabled": has_it}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(program=Program.TWS), LABELS, dispatcher, restart_expected=False
        )
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE),
                    _window_event(
                        2,
                        "window_opened",
                        "javax.swing.JDialog",
                        "Downloading settings from server",
                        window_id="w-intermediate",
                    ),
                    _window_event(
                        3,
                        "window_opened",
                        "jclient.qT",
                        "DU6351764 Interactive Brokers (Simulated Trading)",
                        window_id="w-main",
                    ),
                ],
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    assert manager.main_window_id == "w-main"


async def test_tws_2fa_is_handled_inline_then_reaches_logged_in(
    sock_path, event_sock_path, tmp_path
):
    """Unlike Gateway, the TWS path's own 2FA-close *is* part of what leads to
    the terminal main-window-open match (no splash frame involved at all) --
    same choreography this file used for the Gateway case before the fix."""
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(program=Program.TWS), LABELS, dispatcher, restart_expected=False
        )
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE),
                    _window_event(
                        2,
                        "window_opened",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                    _window_event(
                        3,
                        "window_closed",
                        "twslaunch.jauthentication.bh",
                        "Second Factor Authentication",
                    ),
                    _window_event(
                        4,
                        "window_opened",
                        _TWS_MAIN_CLASS,
                        _TWS_TITLE,
                        window_id="w-main",
                    ),
                ],
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN


@pytest.mark.parametrize(
    ("program", "login_event"),
    [
        (Program.TWS, ("window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE)),
        (Program.GATEWAY, ("window_opened", "ibgateway.ax", "IBKR Gateway")),
    ],
)
async def test_run_raises_when_scheduled_retry_fails(
    sock_path, event_sock_path, program, login_event
):
    """A retry whose click hits a closed frame ends `run()` with that error
    instead of leaving the outcome wait pending."""
    clicks = 0

    def responder(request):
        nonlocal clicks
        if request.get("cmd") == "click":
            clicks += 1
            if clicks > 1:  # the first click is the initial submit
                return {"ok": False, "error": "window_gone", "detail": "closed"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(program=program), LABELS, dispatcher, restart_expected=False
        )
        task = asyncio.ensure_future(manager.run(login_timeout=5.0))
        try:
            await _feed(dispatcher, [_window_event(1, *login_event)])
            await asyncio.sleep(0.05)
            manager.schedule_retry(0.01)
            with pytest.raises(WindowGoneError):
                await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()


@pytest.mark.parametrize(
    ("program", "login_event"),
    [
        (Program.TWS, ("window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE)),
        (Program.GATEWAY, ("window_opened", "ibgateway.ax", "IBKR Gateway")),
    ],
)
async def test_outcome_wait_has_no_overall_deadline(
    sock_path, event_sock_path, program, login_event
):
    """No outcome event ever arrives (e.g. no network): `run()` stays pending
    past `mfa_timeout` instead of raising, as IBC does."""
    async with (
        FakeCommandServer(sock_path, _tracking_responder([])),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(program=program, mfa_timeout=0.05)
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        task = asyncio.ensure_future(manager.run(login_timeout=5.0))
        try:
            await _feed(dispatcher, [_window_event(1, *login_event)])
            await asyncio.sleep(0.3)  # several mfa_timeout periods
            assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await dispatcher.stop()


async def test_gateway_unanswered_2fa_closing_after_the_deadline_relogs_in(
    sock_path, event_sock_path, monkeypatch
):
    """IB closes an unanswered 2FA dialog after the login deadline. The close
    wait is uncapped, so the late branch runs through the real wait and, with
    relogin on, re-submits the credentials and reaches `LOGGED_IN`."""
    monkeypatch.setattr("ibcontroller.login._IBC_RELOGIN_DELAY_SECONDS", 0.01)
    calls: list[dict] = []
    async with (
        FakeCommandServer(sock_path, _tracking_responder(calls)),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(relogin_after_mfa_timeout=True, mfa_timeout=0.2)
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(2, "window_opened", _MFA_CLASS, _MFA_TITLE),
                ],
            )
            await asyncio.sleep(0.4)  # past mfa_timeout
            assert not task.done()
            dispatcher._window_events.emit(
                _window_event(3, "window_closed", _MFA_CLASS, _MFA_TITLE)
            )
            await asyncio.sleep(0.1)  # relogin delay, then credentials again
            dispatcher._window_events.emit(_splash_closed_event(4))
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            await dispatcher.stop()
    assert manager.state is LoginState.LOGGED_IN
    sent_targets = [c.get("target") for c in calls if c.get("cmd") == "set_text"]
    assert sent_targets.count("Username") == 2


async def test_tws_unanswered_2fa_closing_after_the_deadline_relogs_in(
    sock_path, event_sock_path, monkeypatch
):
    """TWS mirror of the Gateway test above."""
    monkeypatch.setattr("ibcontroller.login._IBC_RELOGIN_DELAY_SECONDS", 0.01)
    calls: list[dict] = []
    async with (
        FakeCommandServer(sock_path, _tracking_responder(calls)),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            program=Program.TWS, relogin_after_mfa_timeout=True, mfa_timeout=0.2
        )
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE),
                    _window_event(2, "window_opened", _MFA_CLASS, _MFA_TITLE),
                ],
            )
            await asyncio.sleep(0.4)  # past mfa_timeout
            assert not task.done()
            dispatcher._window_events.emit(
                _window_event(3, "window_closed", _MFA_CLASS, _MFA_TITLE)
            )
            await asyncio.sleep(0.1)  # relogin delay, then credentials again
            dispatcher._window_events.emit(
                _window_event(
                    4,
                    "window_opened",
                    _TWS_MAIN_CLASS,
                    _TWS_TITLE,
                    window_id="w-main",
                )
            )
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            await dispatcher.stop()
    assert manager.state is LoginState.LOGGED_IN
    sent_targets = [c.get("target") for c in calls if c.get("cmd") == "set_text"]
    assert sent_targets.count("Username") == 2


async def _late_close_run(
    sock_path, event_sock_path, monkeypatch, calls, responder
) -> LoginManager:
    """TWS: unanswered 2FA closes after `mfa_timeout`, relogin on. Returns the
    manager with `run()` still pending after the close."""
    monkeypatch.setattr("ibcontroller.login._IBC_RELOGIN_DELAY_SECONDS", 0.01)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            program=Program.TWS, relogin_after_mfa_timeout=True, mfa_timeout=0.2
        )
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        task = asyncio.ensure_future(manager.run(login_timeout=5.0))
        try:
            await _feed(
                dispatcher,
                [_window_event(1, "window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE)],
            )
            await asyncio.sleep(0.4)  # past mfa_timeout
            dispatcher._window_events.emit(
                _window_event(2, "window_closed", _MFA_CLASS, _MFA_TITLE)
            )
            await asyncio.sleep(0.1)  # relogin delay, then the form check
            assert not task.done()
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await dispatcher.stop()
    return manager


async def test_tws_late_2fa_close_without_login_form_does_not_relogin(
    sock_path, event_sock_path, monkeypatch
):
    """The 2FA close is reported while the frame still shows 2FA :
    no credentials are refilled and the wait continues."""
    calls: list[dict] = []
    responder = _tracking_responder(calls, not_found_cmds=frozenset({"get_text"}))
    await _late_close_run(sock_path, event_sock_path, monkeypatch, calls, responder)
    sent = [c.get("target") for c in calls if c.get("cmd") == "set_text"]
    assert sent.count("Username") == 1  # the initial fill only


async def test_tws_failed_relogin_does_not_end_the_login_wait(
    sock_path, event_sock_path, monkeypatch
):
    """A resubmit that cannot find its controls is logged, not fatal."""
    calls: list[dict] = []
    base = _tracking_responder(calls)
    fills = 0

    def responder(request):
        nonlocal fills
        if request.get("cmd") == "set_text" and request.get("target") == "Username":
            fills += 1
        if request.get("cmd") == "click" and fills > 1:  # only the relogin fails
            calls.append(request)
            return {"ok": False, "error": "not_found", "detail": "no such component"}
        return base(request)

    await _late_close_run(sock_path, event_sock_path, monkeypatch, calls, responder)
    assert fills == 2  # the relogin was attempted


async def test_gateway_late_2fa_close_without_relogin_keeps_waiting(
    sock_path, event_sock_path
):
    """Relogin off (IBC: keep waiting): a late close does not end `run()` and
    does not resubmit; the splash closing completes the login."""
    calls: list[dict] = []
    async with (
        FakeCommandServer(sock_path, _tracking_responder(calls)),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(relogin_after_mfa_timeout=False, mfa_timeout=0.2)
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway"),
                    _window_event(2, "window_opened", _MFA_CLASS, _MFA_TITLE),
                ],
            )
            await asyncio.sleep(0.4)
            dispatcher._window_events.emit(
                _window_event(3, "window_closed", _MFA_CLASS, _MFA_TITLE)
            )
            await asyncio.sleep(0.1)
            assert not task.done()
            dispatcher._window_events.emit(_splash_closed_event(4))
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            await dispatcher.stop()
    assert manager.state is LoginState.LOGGED_IN
    sent_targets = [c.get("target") for c in calls if "target" in c]
    assert sent_targets.count("Username") == 1


async def _registry_seeded(dispatcher: Dispatcher, window_id: str) -> None:
    while window_id not in dispatcher.windows:
        await asyncio.sleep(0.01)


async def test_tws_window_opening_during_menu_check_is_not_lost(
    sock_path, event_sock_path
):
    """Both events land in one tick: the main window opens while the loop is
    still awaiting `menu_item_exists` for the intermediate one. The single
    subscription buffers it."""

    def responder(request):
        if request.get("cmd") == "menu_item_exists":
            has_it = request.get("window_id") == "w-main"
            return {"ok": True, "exists": has_it, "enabled": has_it}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(program=Program.TWS), LABELS, dispatcher, restart_expected=False
        )
        try:
            task = asyncio.ensure_future(manager._wait_for_outcome_tws())
            await asyncio.sleep(0.01)
            for seq, window_id in ((1, "w-intermediate"), (2, "w-main")):
                dispatcher._window_events.emit(
                    _window_event(
                        seq,
                        "window_opened",
                        "javax.swing.JDialog",
                        "Some window",
                        window_id=window_id,
                    )
                )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    assert manager.main_window_id == "w-main"


async def test_tws_candidate_window_gone_before_menu_check_is_skipped(
    sock_path, event_sock_path
):
    """An intermediate window that closes before `menu_item_exists` reaches it
    answers `window_gone`; the loop moves on to the main window."""

    def responder(request):
        if request.get("cmd") == "menu_item_exists":
            if request.get("window_id") == "w-gone":
                return {"ok": False, "error": "window_gone", "detail": "closed"}
            return {"ok": True, "exists": True, "enabled": True}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(program=Program.TWS), LABELS, dispatcher, restart_expected=False
        )
        try:
            task = asyncio.ensure_future(manager._wait_for_outcome_tws())
            await asyncio.sleep(0.01)
            for seq, window_id in ((1, "w-gone"), (2, "w-main")):
                dispatcher._window_events.emit(
                    _window_event(
                        seq,
                        "window_opened",
                        "javax.swing.JDialog",
                        "Downloading settings from server",
                        window_id=window_id,
                    )
                )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    assert manager.main_window_id == "w-main"


async def test_tws_outcome_finds_a_window_already_open_when_it_starts(
    sock_path, event_sock_path
):
    """A main window opened before the outcome loop subscribed (e.g. during
    the MFA-closed handling) is found through the window registry."""
    snapshot = {
        "type": "snapshot",
        "windows": [
            {"class": "jclient.qT", "title": "DU1 Main", "window_id": "w-main"}
        ],
    }
    async with (
        FakeCommandServer(sock_path, _tracking_responder([])),
        FakeEventServer(event_sock_path, [snapshot]),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(program=Program.TWS), LABELS, dispatcher, restart_expected=False
        )
        try:
            await _registry_seeded(dispatcher, "w-main")
            await asyncio.wait_for(manager._wait_for_outcome_tws(), timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    assert manager.main_window_id == "w-main"


async def test_login_frame_opened_before_run_is_found(sock_path, event_sock_path):
    """The login frame opened before `run()` waited for it; the registry
    still reports it, so the wait does not time out."""
    snapshot = {
        "type": "snapshot",
        "windows": [
            {"class": _TWS_LOGIN_CLASS, "title": _TWS_TITLE, "window_id": "w-login"}
        ],
    }
    async with (
        FakeCommandServer(sock_path, _tracking_responder([])),
        FakeEventServer(event_sock_path, [snapshot]),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(program=Program.TWS), LABELS, dispatcher, restart_expected=True
        )
        try:
            await _registry_seeded(dispatcher, "w-login")
            task = asyncio.ensure_future(manager.run(login_timeout=2.0))
            await asyncio.sleep(0.05)
            assert manager._login_frame_window_id == "w-login"
            dispatcher._window_events.emit(
                _window_event(
                    1, "window_opened", _TWS_MAIN_CLASS, "Main", window_id="w-main"
                )
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN


async def test_tws_2fa_close_without_open_arms_exit_interval_watchdog(
    sock_path, event_sock_path
):
    """Live TWS 10.51 sends no `window_opened` for 2FA (the login frame turns into
    it in place), only a `window_closed` with a null `window_id`. The close alone
    must arm the `mfa_exit_interval` watchdog."""
    async with (
        FakeCommandServer(sock_path, _tracking_responder([])),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            program=Program.TWS,
            relogin_after_mfa_timeout=True,
            mfa_timeout=5.0,
            mfa_exit_interval=0.2,
        )
        manager = LoginManager(config, LABELS, dispatcher, restart_expected=False)
        task = asyncio.ensure_future(manager.run(login_timeout=5.0))
        try:
            await _feed(
                dispatcher,
                [
                    _window_event(1, "window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE),
                    _window_event(2, "window_closed", _MFA_CLASS, _MFA_TITLE),
                ],
            )
            with pytest.raises(MfaTimeoutError):
                await asyncio.wait_for(task, timeout=2.0)
        finally:
            await dispatcher.stop()


def _poll_responder(calls: list[dict], components: list[dict]):
    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            return {"ok": True, "components": components}
        if request.get("cmd") == "menu_item_exists":
            return {"ok": True, "exists": True, "enabled": True}
        return {"ok": True}

    return responder


async def _poll_run(
    sock_path, event_sock_path, monkeypatch, components, **config
) -> tuple[LoginManager, list[dict], LoginState]:
    """Runs a TWS login until the frame poll had time to fire; returns the
    manager, the commands sent and the state seen mid-login."""
    monkeypatch.setattr("ibcontroller.login._TWS_2FA_POLL_SECONDS", 0.01)
    calls: list[dict] = []
    async with (
        FakeCommandServer(sock_path, _poll_responder(calls, components)),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(program=Program.TWS, **config),
            LABELS,
            dispatcher,
            restart_expected=False,
        )
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [_window_event(1, "window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE, "w1")],
            )
            await asyncio.sleep(0.2)
            mid_state = manager.state
            dispatcher._window_events.emit(
                _window_event(2, "window_opened", _TWS_MAIN_CLASS, _TWS_TITLE, "w-main")
            )
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            await dispatcher.stop()
    return manager, calls, mid_state


async def test_tws_poll_sets_mfa_in_progress_when_login_frame_shows_2fa_heading(
    sock_path, event_sock_path, monkeypatch
):
    components = [
        {
            "class": "javax.swing.JLabel",
            "accessible_name": "SECOND FACTOR AUTHENTICATION",
        }
    ]
    manager, calls, mid_state = await _poll_run(
        sock_path,
        event_sock_path,
        monkeypatch,
        components,
        trading_mode=TradingMode.LIVE,
    )
    assert mid_state is LoginState.MFA_IN_PROGRESS
    assert manager.state is LoginState.LOGGED_IN
    assert {c["window_id"] for c in calls if c["cmd"] == "dump"} == {"w1"}


async def test_tws_poll_ignores_the_login_heading(
    sock_path, event_sock_path, monkeypatch
):
    components = [
        {
            "class": "javax.swing.JLabel",
            "accessible_name": "Second Factor Authentication",
        }
    ]
    _, _, mid_state = await _poll_run(
        sock_path,
        event_sock_path,
        monkeypatch,
        components,
        trading_mode=TradingMode.LIVE,
    )
    assert mid_state is LoginState.LOGGING_IN


async def test_tws_paper_login_does_not_poll(sock_path, event_sock_path, monkeypatch):
    components = [
        {
            "class": "javax.swing.JLabel",
            "accessible_name": "SECOND FACTOR AUTHENTICATION",
        }
    ]
    _, calls, mid_state = await _poll_run(
        sock_path, event_sock_path, monkeypatch, components
    )
    assert mid_state is LoginState.LOGGING_IN
    assert not [c for c in calls if c["cmd"] == "dump"]


def _read_only_responder(calls: list[dict], click_error: str | None = None):
    components = [{"class": "j", "accessible_name": "SECOND FACTOR AUTHENTICATION"}]

    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            return {"ok": True, "components": components}
        if request.get("cmd") == "menu_item_exists":
            return {"ok": True, "exists": True, "enabled": True}
        if (
            click_error
            and request.get("cmd") == "click"
            and request.get("target") == "Enter Read Only"
        ):
            return {"ok": False, "error": click_error, "detail": "x"}
        return {"ok": True}

    return responder


async def _read_only_run(sock_path, event_sock_path, monkeypatch, responder, **config):
    monkeypatch.setattr("ibcontroller.login._TWS_2FA_POLL_SECONDS", 0.01)
    monkeypatch.setattr("ibcontroller.login._IBC_RELOGIN_DELAY_SECONDS", 0.01)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(
                program=Program.TWS,
                trading_mode=TradingMode.LIVE,
                read_only_login=True,
                **config,
            ),
            LABELS,
            dispatcher,
            restart_expected=False,
        )
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [_window_event(1, "window_opened", _TWS_LOGIN_CLASS, _TWS_TITLE, "w1")],
            )
            await asyncio.sleep(0.2)
            dispatcher._window_events.emit(
                _window_event(2, "window_closed", _MFA_CLASS, _MFA_TITLE)
            )
            await asyncio.sleep(0.1)
            dispatcher._window_events.emit(
                _window_event(3, "window_opened", _TWS_MAIN_CLASS, _TWS_TITLE, "w-main")
            )
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            await dispatcher.stop()
    return manager


async def test_tws_read_only_clicks_the_button_and_skips_2fa_close_handling(
    sock_path, event_sock_path, monkeypatch
):
    calls: list[dict] = []
    manager = await _read_only_run(
        sock_path,
        event_sock_path,
        monkeypatch,
        _read_only_responder(calls),
        relogin_after_mfa_timeout=True,
        mfa_timeout=0.0,  # a normal close would count as late and relogin
    )
    clicks = [
        c for c in calls if c["cmd"] == "click" and c["target"] == "Enter Read Only"
    ]
    assert [c["window_id"] for c in clicks] == ["w1"]
    assert not [c for c in calls if c["cmd"] == "set_text"][2:]  # filled once only
    assert manager.state is LoginState.LOGGED_IN


async def test_tws_read_only_click_failure_keeps_normal_2fa_handling(
    sock_path, event_sock_path, monkeypatch
):
    calls: list[dict] = []
    manager = await _read_only_run(
        sock_path,
        event_sock_path,
        monkeypatch,
        _read_only_responder(calls, click_error="not_found"),
    )
    assert manager._read_only_clicked is False
    assert manager.state is LoginState.LOGGED_IN


async def test_gateway_read_only_login_warns_and_logs_in_normally(
    sock_path, event_sock_path, caplog
):
    caplog.set_level(logging.WARNING)
    calls: list[dict] = []
    async with (
        FakeCommandServer(sock_path, _tracking_responder(calls)),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(
            _config(read_only_login=True), LABELS, dispatcher, restart_expected=False
        )
        try:
            task = asyncio.ensure_future(manager.run(login_timeout=5.0))
            await _feed(
                dispatcher,
                [_window_event(1, "window_opened", "ibgateway.ax", "IBKR Gateway")],
            )
            await asyncio.sleep(0.05)
            dispatcher._window_events.emit(_splash_closed_event(2))
            await asyncio.wait_for(task, timeout=2.0)
        finally:
            await dispatcher.stop()
    assert "read_only_login is not supported by Gateway" in caplog.text
    assert not [c for c in calls if c.get("target") == "Enter Read Only"]


_GW_LOGIN_CLASS = "ibgateway.ax"
_GW_MAIN_CLASS = "ibgateway.aw"
_GW_SNAPSHOT = {
    "type": "snapshot",
    "windows": [
        {"class": _GW_LOGIN_CLASS, "title": "IBKR Gateway", "window_id": "w1"},
        {"class": _GW_MAIN_CLASS, "title": "IBKR Gateway", "window_id": "w2"},
        {"class": "jutils.eh", "title": "Some dialog", "window_id": "w3"},
    ],
}


async def _gateway_capture(
    sock_path, event_sock_path, responder, snapshot=_GW_SNAPSHOT
) -> LoginManager:
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, [snapshot]),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, restart_expected=False)
        manager._login_frame_class = _GW_LOGIN_CLASS
        try:
            await _registry_seeded(dispatcher, "w2")
            await manager._capture_gateway_main_window()
        finally:
            await dispatcher.stop()
    return manager


async def test_gateway_main_window_is_found_by_menu_probe(sock_path, event_sock_path):
    """The login frame (same title, other class) is skipped; the frame with
    the About item becomes `main_window_id`."""
    probed: list[tuple[str, str]] = []

    def responder(request):
        if request.get("cmd") == "menu_item_exists":
            probed.append((request["window_id"], request["path"]))
            hit = request["window_id"] == "w2" and request["path"].endswith("Gateway")
            return {"ok": True, "exists": hit, "enabled": hit}
        return {"ok": True}

    manager = await _gateway_capture(sock_path, event_sock_path, responder)

    assert manager.main_window_id == "w2"
    assert all(window_id == "w2" for window_id, _ in probed)  # only w2 is a candidate


async def test_gateway_main_window_tries_the_alternative_label(
    sock_path, event_sock_path
):
    """Older Gateway versions label the item `About IB Gateway`."""

    def responder(request):
        if request.get("cmd") == "menu_item_exists":
            hit = request["path"] == "Help/About IB Gateway"
            return {"ok": True, "exists": hit, "enabled": hit}
        return {"ok": True}

    manager = await _gateway_capture(sock_path, event_sock_path, responder)

    assert manager.main_window_id == "w2"


async def test_gateway_main_window_stays_none_without_a_match(
    sock_path, event_sock_path
):
    """No frame has the menu item: consumers fall back to an unscoped search."""

    def responder(request):
        if request.get("cmd") == "menu_item_exists":
            return {"ok": True, "exists": False, "enabled": False}
        return {"ok": True}

    manager = await _gateway_capture(sock_path, event_sock_path, responder)

    assert manager.main_window_id is None


async def test_gateway_main_window_gone_before_probe_is_skipped(
    sock_path, event_sock_path
):
    """A frame that closes before the probe answers `window_gone`; capture
    ends without raising."""

    def responder(request):
        if request.get("cmd") == "menu_item_exists":
            return {"ok": False, "error": "window_gone", "detail": "closed"}
        return {"ok": True}

    manager = await _gateway_capture(sock_path, event_sock_path, responder)

    assert manager.main_window_id is None
