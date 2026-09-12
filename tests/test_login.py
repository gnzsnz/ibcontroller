"""Unit tests for login.py (L6) -- no live Gateway needed, same fake-socket-server
pattern the rest of this project's tests use. Multi-step scenarios (2FA,
existing-session) drive `Dispatcher.window_events` directly with controlled
timing (`asyncio.sleep(0)` between emits), matching actions.py's own established
pattern for this -- avoids any race with a fake event *socket* server that would
otherwise dump every scripted message at once, well before a later step in the
sequence has its own listener connected.

**`LoginManager` no longer takes a registry at all (2026-09-05, a real bug fixed,
not a design choice from the start)** -- reacting to existing-session/login-failed/
non-brokerage dialogs is entirely `recognisers.watch_for_unprompted_windows`'s job
now, run as a separate concurrent task. The tests for those scenarios run both
`LoginManager.run()` and the watcher together, matching how they're actually meant
to be wired -- see `test_login_failed_is_raised_by_the_watcher_not_by_run` for why
`LoginFailedError` no longer propagates from `run()` itself."""

from __future__ import annotations

import asyncio
import contextlib
from tempfile import gettempdir

import attrs
import pytest
from typed_settings.types import Secret

from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    WindowEvent,
    WindowInfo,
)
from ibcontroller.config import Config, ExistingSessionAction, TradingMode
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import load_labels
from ibcontroller.login import (
    LoginError,
    LoginManager,
    LoginState,
    find_autorestart_hash,
    is_restart,
)
from ibcontroller.recognisers import (
    DeclarativeDismissRecognizer,
    ExistingSessionRecognizer,
    LoginFailedError,
    LoginFailedRecognizer,
    RecognizerRegistry,
    TooManyFailedLoginAttemptsRecognizer,
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
        program="gateway",
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
# already relabelled to this by the time it fires, confirmed live 2026-09-06/07.
# The class is arbitrary (matching is by title only, same as IBC's own
# `titleContains` check) -- this mirrors the real one seen in live/paper testing.
_SPLASH_CLASS = "twslaunch.feature.welcome.C"


def _splash_closed_event(seq: int) -> WindowEvent:
    return _window_event(seq, "window_closed", _SPLASH_CLASS, "Starting application...")


def _tracking_responder(calls: list[dict], *, menu_item_exists: bool = True):
    """`dump` needs a `components` key (empty is fine -- these tests recognise
    existing-session/login-failed by event title, not component content);
    `menu_item_exists` needs `exists`/`enabled` keys (`login.py`'s TWS
    main-window detection, 2026-09-09 -- default `True` matches every
    existing TWS test's own fed "main window" event); everything else just
    needs `{"ok": True}`."""

    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
        if request.get("cmd") == "menu_item_exists":
            return {"ok": True, "exists": menu_item_exists, "enabled": menu_item_exists}
        return {"ok": True}

    return responder


async def _feed(dispatcher: Dispatcher, events: list[WindowEvent]) -> None:
    """Lets the currently-running task(s) reach their own next `wait_for_event`/
    watcher filter-connection point before each emit."""
    for event in events:
        await asyncio.sleep(0.01)
        dispatcher.window_events.emit(event)


# --- is_restart -------------------------------------------------------------


async def test_is_restart_false_when_no_marker_file(tmp_path):
    assert await is_restart(tmp_path) is False


async def test_is_restart_true_when_marker_file_present(tmp_path):
    (tmp_path / "autorestart").write_text("")
    assert await is_restart(tmp_path) is True


async def test_find_autorestart_hash_none_when_no_marker(tmp_path):
    assert await find_autorestart_hash(tmp_path) is None


async def test_find_autorestart_hash_returns_the_subdirectory_name(tmp_path):
    account_dir = tmp_path / "nlabafcdedmocmpmkmkcecpfjmillhejiljogfeh"
    account_dir.mkdir()
    (account_dir / "autorestart").write_text("")
    assert await find_autorestart_hash(tmp_path) == account_dir.name


async def test_find_autorestart_hash_none_when_ambiguous(tmp_path):
    """Ported from IBC's own `find_auto_restart` -- "IBC can't determine
    which is the right one" when more than one marker exists. Unlike IBC
    (which deletes both), this leaves the files in place and just returns
    `None` -- the caller falls back to a full credential fill either way."""
    for name in ("hash1", "hash2"):
        d = tmp_path / name
        d.mkdir()
        (d / "autorestart").write_text("")
    assert await find_autorestart_hash(tmp_path) is None
    # non-destructive: both files are still there
    assert (tmp_path / "hash1" / "autorestart").exists()
    assert (tmp_path / "hash2" / "autorestart").exists()


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
        manager = LoginManager(_config(), LABELS, dispatcher, settings_dir=tmp_path)
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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
    """Regression test for a known, previously-worked-around gap (Build plan
    step 4/5): the login frame's FIX Login and IB API sections both carry
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
        manager = LoginManager(_config(), LABELS, dispatcher, settings_dir=tmp_path)
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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


async def test_restart_skips_credential_fill(sock_path, event_sock_path, tmp_path):
    (tmp_path / "autorestart").write_text("")
    calls: list[dict] = []
    responder = _tracking_responder(calls)

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, settings_dir=tmp_path)
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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
    # no click/set_text commands at all -- credential fill was skipped entirely
    assert calls == []


async def test_restart_expected_true_skips_fill_even_with_no_marker_file(
    sock_path, event_sock_path, tmp_path
):
    """2026-09-07: `restart_expected` overrides the filesystem check entirely
    -- the real, live-caught bug this closes: once `-Drestart=<hash>` actually
    works, Gateway can delete the marker file quickly as part of a genuinely
    successful silent relogin, so a check against `settings_dir` done *after*
    relaunch can find it already gone. `control_loop.py` passes this
    explicitly instead, from what it already knew before relaunching."""
    # deliberately no autorestart file written -- tmp_path is empty
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
            settings_dir=tmp_path,
            restart_expected=True,
        )
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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


async def test_restart_expected_false_fills_credentials_even_with_marker_file(
    sock_path, event_sock_path, tmp_path
):
    """The other direction of the same override -- a stale marker file left
    over from an earlier cycle must not cause a fresh launch to skip
    credentials when the caller has already determined (via `restart_hash`)
    that this launch is not a restart."""
    (tmp_path / "autorestart").write_text("")
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
            settings_dir=tmp_path,
            restart_expected=False,
        )
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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


async def test_2fa_is_handled_inline_then_reaches_logged_in(
    sock_path, event_sock_path, tmp_path
):
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, settings_dir=tmp_path)
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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


async def test_login_frame_never_appears_raises_login_error(
    sock_path, event_sock_path, tmp_path
):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, settings_dir=tmp_path)
        try:
            with pytest.raises(LoginError):
                await manager.run(login_timeout=0.2, outcome_timeout=1.0)
        finally:
            await dispatcher.stop()


# --- LoginManager, unprompted dialogs handled by the separate watcher --------


async def test_existing_session_handled_by_watcher_while_login_still_waits(
    sock_path, event_sock_path, tmp_path
):
    """Confirms the real, corrected wiring: `LoginManager.run()`'s own filter
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
        manager = LoginManager(_config(), LABELS, dispatcher, settings_dir=tmp_path)
        # not logged in -> ExistingSession scenario 3
        registry = _registry(lambda: False)
        watcher = asyncio.ensure_future(
            watch_for_unprompted_windows(registry, dispatcher)
        )
        try:
            run_task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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
    """The real behavioural consequence of the fix: `LoginFailedError` no
    longer propagates from `LoginManager.run()` (its own filter never
    captures "Login failed" at all) -- it propagates from the separate
    watcher task instead, since that's the only thing left dispatching to the
    registry. `run()` itself just keeps waiting until its own timeout."""
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, settings_dir=tmp_path)
        registry = _registry(lambda: False)
        watcher = asyncio.ensure_future(
            watch_for_unprompted_windows(registry, dispatcher)
        )
        run_task = asyncio.ensure_future(
            manager.run(login_timeout=5.0, outcome_timeout=1.0)
        )
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
            with pytest.raises(LoginFailedError):
                await asyncio.wait_for(watcher, timeout=5.0)
            assert manager.state is LoginState.LOGGING_IN  # run() never saw it
        finally:
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task
            await dispatcher.stop()


# --- login/2FA timeout + retry (2026-09-05) ----------------------------------


async def test_run_uses_config_sourced_timeouts_when_not_overridden(
    sock_path, event_sock_path, tmp_path
):
    """`login_timeout`/`outcome_timeout` default from
    `Config.login_dialog_display_timeout`/`second_factor_authentication_timeout`
    (IBC's own real settings) when `run()` isn't given explicit values --
    confirmed here by NOT passing either argument."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            login_dialog_display_timeout=5.0,
            second_factor_authentication_timeout=5.0,
        )
        manager = LoginManager(config, LABELS, dispatcher, settings_dir=tmp_path)
        try:
            task = asyncio.ensure_future(manager.run())  # no explicit timeouts
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


async def test_2fa_watchdog_succeeds_within_exit_interval(
    sock_path, event_sock_path, tmp_path
):
    """Fast 2FA close (well within `second_factor_authentication_timeout`)
    with `relogin_after_2fa_timeout` enabled -- the watchdog just bounds the
    rest of the wait by `second_factor_authentication_exit_interval`; the main
    window still arrives in time, so this behaves exactly like the plain 2FA
    case, just with a shorter timeout applied."""
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            relogin_after_2fa_timeout=True,
            second_factor_authentication_timeout=5.0,
            second_factor_authentication_exit_interval=5.0,
        )
        manager = LoginManager(config, LABELS, dispatcher, settings_dir=tmp_path)
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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


async def test_2fa_watchdog_raises_when_main_window_never_appears(
    sock_path, event_sock_path, tmp_path
):
    """Ported from IBC's `restartAfterTime` -- IBC exits/restarts the JVM if
    login hasn't completed within `second_factor_authentication_exit_interval`
    of 2FA closing; we have no restart primitive at this layer, so this raises
    `LoginError` instead, matching how `LoginFailedError` is already treated as
    a real failure signal for whoever eventually supervises Login."""
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            relogin_after_2fa_timeout=True,
            second_factor_authentication_timeout=5.0,
            second_factor_authentication_exit_interval=0.05,
        )
        manager = LoginManager(config, LABELS, dispatcher, settings_dir=tmp_path)
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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
            with pytest.raises(LoginError):
                await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()


async def test_2fa_late_close_relogin_disabled_does_nothing(
    sock_path, event_sock_path, tmp_path
):
    """2FA closes *after* `second_factor_authentication_timeout` (the user
    answered too slowly) with `relogin_after_2fa_timeout` disabled (the
    default) -- matches IBC's own intent (just a log line, no retry) while
    still keeping `run()`'s own contract: it doesn't return early leaving
    `state` stuck at `TWO_FA_IN_PROGRESS` (a real bug this test caught, fixed
    in `_after_2fa_closed_gateway`/`_after_2fa_closed_tws`) -- it keeps
    waiting for the outcome, same as always, just without ever retrying."""
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        config = _config(
            relogin_after_2fa_timeout=False,
            second_factor_authentication_timeout=0.01,
        )
        manager = LoginManager(config, LABELS, dispatcher, settings_dir=tmp_path)
        task = asyncio.ensure_future(
            manager.run(login_timeout=5.0, outcome_timeout=5.0)
        )
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
            dispatcher.window_events.emit(
                _window_event(
                    3,
                    "window_closed",
                    "twslaunch.jauthentication.bh",
                    "Second Factor Authentication",
                )
            )
            await asyncio.sleep(0.05)
            assert manager.state is LoginState.TWO_FA_IN_PROGRESS
        finally:
            # `run()` completes normally here (nothing more to do, matching
            # IBC exactly) -- `task` may already be done by this point, so
            # `cancel()` can be a no-op. `contextlib.suppress`, not
            # `pytest.raises(asyncio.CancelledError)`: the latter hangs when
            # the awaited task did *not* raise CancelledError (a real,
            # empirically-confirmed pytest/asyncio interaction, not a
            # production bug -- found live while building this test, not
            # assumed). `pytest.raises` around `await task` is only safe when
            # the task is genuinely still running and cancellation is certain
            # to raise, as in this file's other cancel-then-await tests.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await dispatcher.stop()


async def test_2fa_late_close_relogin_enabled_retries_then_succeeds(
    sock_path, event_sock_path, tmp_path, monkeypatch
):
    """2FA closes late, `relogin_after_2fa_timeout` enabled -- re-initiates the
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
            relogin_after_2fa_timeout=True,
            second_factor_authentication_timeout=0.01,
            second_factor_authentication_exit_interval=5.0,
        )
        manager = LoginManager(config, LABELS, dispatcher, settings_dir=tmp_path)
        task = asyncio.ensure_future(
            manager.run(login_timeout=5.0, outcome_timeout=5.0)
        )
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
            dispatcher.window_events.emit(
                _window_event(
                    3,
                    "window_closed",
                    "twslaunch.jauthentication.bh",
                    "Second Factor Authentication",
                )
            )
            # let retry_login's sleep(0.01) elapse, then it re-fills + re-clicks
            await asyncio.sleep(0.1)
            dispatcher.window_events.emit(_splash_closed_event(4))
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    # credentials were genuinely re-submitted, not just the original click
    sent_targets = [c.get("target") for c in calls if "target" in c]
    assert sent_targets.count("Username") == 2


async def test_retry_login_refills_credentials_and_reaches_logged_in(
    sock_path, event_sock_path, tmp_path
):
    """`retry_login` in isolation -- mirrors IBC's `initiateLogin(getLoginFrame())`:
    re-fills and re-clicks without waiting for a fresh login-frame
    `window_opened` (there won't be one for an already-open frame), then waits
    for the outcome on its own."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, settings_dir=tmp_path)
        # normally set by run()'s own initial wait for the login frame --
        # this test exercises retry_login on its own, bypassing run() entirely.
        manager._login_frame_class = "ibgateway.ax"
        task = asyncio.ensure_future(manager.retry_login(0.01, outcome_timeout=5.0))
        try:
            await asyncio.sleep(0.05)
            dispatcher.window_events.emit(_splash_closed_event(1))
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    sent_targets = [c.get("target") for c in calls if "target" in c]
    assert "Username" in sent_targets


async def test_schedule_retry_fires_a_background_task(
    sock_path, event_sock_path, tmp_path
):
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        manager = LoginManager(_config(), LABELS, dispatcher, settings_dir=tmp_path)
        manager._login_frame_class = "ibgateway.ax"
        try:
            manager.schedule_retry(0.01)
            assert manager._retry_task is not None  # fire-and-forget, not awaited here
            await asyncio.sleep(0.05)
            dispatcher.window_events.emit(_splash_closed_event(1))
            await asyncio.wait_for(manager._retry_task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN


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
        config = _config(relogin_after_2fa_timeout=True)
        manager = LoginManager(config, LABELS, dispatcher, settings_dir=tmp_path)
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
        run_task = asyncio.ensure_future(
            manager.run(login_timeout=5.0, outcome_timeout=10.0)
        )
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
            dispatcher.window_events.emit(_splash_closed_event(3))
            await asyncio.wait_for(run_task, timeout=5.0)
        finally:
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
            await dispatcher.stop()

    assert manager.state is LoginState.LOGGED_IN
    dismiss_calls = [c for c in calls if c.get("target") == "OK"]
    assert dismiss_calls


# --- TWS path (2026-09-07) ---------------------------------------------------
#
# `_wait_for_outcome_tws` is today's original logic, completely untouched by the
# Gateway fix above -- but until now it was only ever exercised implicitly,
# since every test in this file defaulted to `program="gateway"`. Still not
# validated against a real TWS install (CLAUDE.md's own disclosed gap), but at
# least the code path itself is now covered: main-window-open (same title as
# the login frame, different class) is the terminal signal, exactly as before.

_TWS_LOGIN_CLASS = "twslaunch.tws.LoginFrame"
_TWS_MAIN_CLASS = "twslaunch.tws.MainFrame"
_TWS_TITLE = "Login"


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
            _config(program="tws"), LABELS, dispatcher, settings_dir=tmp_path
        )
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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
    sent_targets = [c.get("target") for c in calls if "target" in c]
    assert "Username" in sent_targets
    assert "Password" in sent_targets


async def test_tws_skips_intermediate_window_then_matches_main_window_by_menu_item(
    sock_path, event_sock_path, tmp_path
):
    """Real bug, live-caught 2026-09-09 against an actual TWS install: the old
    predicate matched the main window by *title*, mirroring Gateway's "same
    title as the login frame" heuristic -- wrong for TWS, whose real main
    window title is account-specific ("DU6351764 Interactive Brokers
    (Simulated Trading)"), and a real run also opens an intermediate window
    ("Downloading settings from server") in between. Reproduces both: an
    intermediate window carrying no File/Lock Application menu item is
    skipped (the loop keeps waiting), and only the window that actually
    carries it (ported from IBC's own MainWindowFrameHandler.recogniseWindow)
    completes login."""

    def responder(request):
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
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
            _config(program="tws"), LABELS, dispatcher, settings_dir=tmp_path
        )
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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
            _config(program="tws"), LABELS, dispatcher, settings_dir=tmp_path
        )
        try:
            task = asyncio.ensure_future(
                manager.run(login_timeout=5.0, outcome_timeout=5.0)
            )
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
