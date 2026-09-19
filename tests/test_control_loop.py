"""Unit tests for control_loop.py's pure, no-live-Gateway-needed half --
`StartupState`/`_log_transition`, `_build_registry`, and
`run_control_loop`'s cancellation handling (`ShutdownCause.REQUESTED`).
`run_control_loop`'s real launch/login/settings sequence needs a real install
to mean anything (same as `launcher.launch_instance`'s own precedent) and
isn't unit-tested here -- but the cancellation path is pure control flow
around a monkeypatched `_run_one_cycle`, so it is."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import types
from datetime import datetime, timedelta
from tempfile import gettempdir

import attrs
import pytest
from typed_settings.types import Secret

from ibcontroller import control_loop
from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    WindowEvent,
    WindowInfo,
)
from ibcontroller.config import Config, TradingMode
from ibcontroller.control_loop import (
    ShutdownCause,
    StartupState,
    _apply_declarative_settings,
    _build_registry,
    _log_transition,
    _PhaseAborted,
    _run_phase_watching_process,
    _sleep_until_scheduled_shutdown,
    _wait_for_first_completion,
    run_control_loop,
)
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import load_labels
from ibcontroller.login import LoginManager
from ibcontroller.recognisers import (
    AcceptIncomingConnectionsRecognizer,
    DeclarativeDismissRecognizer,
    ExistingSessionRecognizer,
    LoginFailedRecognizer,
    TooManyFailedLoginAttemptsRecognizer,
)
from ibcontroller.schedule import ScheduledAction, ScheduledShutdown
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio

LABELS = load_labels()


def test_startup_state_has_no_separate_awaiting_dialogs_step():
    """Open item 5's originally-proposed `AWAITING_STARTUP_DIALOGS` state was
    dropped before being built -- `navigate_menu`'s `isEnabled()`-retry
    (2026-09-06) absorbed "wait for a blocking startup dialog to clear" into
    the act of opening Settings itself, so there's no separate step left."""
    assert {s.name for s in StartupState} == {
        "LAUNCHING",
        "LOGGING_IN",
        "APPLYING_SETTINGS",
        "READY",
        "SHUTTING_DOWN",
    }


def test_log_transition_names_both_states(caplog):
    with caplog.at_level(logging.INFO, logger="ibcontroller.control_loop"):
        _log_transition(StartupState.LAUNCHING, StartupState.LOGGING_IN)
    assert any(
        "LAUNCHING" in message and "LOGGING_IN" in message
        for message in caplog.messages
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


def test_build_registry_contains_declarative_rules_and_hand_written_built_ins():
    config = _config()

    class _FakeDispatcher:
        pass

    manager = LoginManager(
        config,
        LABELS,
        _FakeDispatcher(),  # type: ignore[arg-type]
        settings_dir=f"{gettempdir()}/settings",
    )
    registry = _build_registry(config, LABELS, manager)
    declarative = [
        r for r in registry._registry if isinstance(r, DeclarativeDismissRecognizer)
    ]
    hand_written_types = {
        type(r)
        for r in registry._registry
        if not isinstance(r, DeclarativeDismissRecognizer)
    }
    # Declarative rules come first, matching the documented priority order.
    assert registry._registry[: len(declarative)] == declarative
    assert {r.rule.name for r in declarative} == {
        rule.name for rule in LABELS.dismiss_rules
    }
    assert hand_written_types == {
        ExistingSessionRecognizer,
        AcceptIncomingConnectionsRecognizer,
        LoginFailedRecognizer,
        TooManyFailedLoginAttemptsRecognizer,
    }


async def test_run_control_loop_returns_requested_when_task_is_cancelled(
    monkeypatch, caplog
):
    """The real bug: cancelling `run_control_loop`'s own task used to raise
    `CancelledError` straight through its `finally` instead of ever reaching
    `return cause`, so `ShutdownCause.REQUESTED` was unreachable as a value
    (CLAUDE.md Open item 5's last loose end, fixed 2026-09-09). `_run_one_cycle`
    is monkeypatched to a coroutine that never completes on its own, matching
    "cancel this coroutine's own task at any point" -- the documented stop
    mechanism -- rather than exercising the real launch/login sequence."""

    async def _never_completes(*args, **kwargs):
        await asyncio.sleep(3600)
        raise AssertionError("should have been cancelled before this ever fires")

    monkeypatch.setattr(control_loop, "_run_one_cycle", _never_completes)
    monkeypatch.setattr(control_loop, "stop_logging", lambda: None)

    task = asyncio.ensure_future(run_control_loop(_config(), "agent.jar"))
    await asyncio.sleep(0)  # let the loop actually start awaiting _run_one_cycle
    task.cancel()

    with caplog.at_level(logging.INFO, logger="ibcontroller.control_loop"):
        cause = await task

    assert cause is ShutdownCause.REQUESTED
    assert not task.cancelled()
    assert any("stop requested" in message for message in caplog.messages)


async def test_sleep_until_scheduled_shutdown_fires_the_matching_cause(monkeypatch):
    # closedown_at's real format is minute-granular, so a live-clock test
    # can't exercise a genuinely near-immediate fire without either flaking
    # (the target minute may already have passed by the time this runs) or
    # sleeping up to a full day -- monkeypatch next_scheduled_shutdown
    # itself instead, matching this project's split-testing discipline
    # (schedule.py's own math is unit-tested separately in
    # tests/test_schedule.py; this test only exercises the sleep-then-return
    # wiring in control_loop.py).
    soon = ScheduledShutdown(
        at=datetime.now() + timedelta(seconds=0.05),
        action=ScheduledAction.TIDY_CLOSEDOWN,
    )
    monkeypatch.setattr(
        control_loop, "next_scheduled_shutdown", lambda config, now: soon
    )
    config = _config(program="tws", closedown_at="22:00")
    cause = await asyncio.wait_for(_sleep_until_scheduled_shutdown(config), timeout=5.0)
    assert cause is ShutdownCause.TIDY_CLOSEDOWN


async def test_sleep_until_scheduled_shutdown_sleeps_forever_when_unconfigured():
    config = _config(program="tws")
    task = asyncio.ensure_future(_sleep_until_scheduled_shutdown(config))
    await asyncio.sleep(0.05)
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _never_completing_task():
    return asyncio.ensure_future(asyncio.sleep(3600))


@attrs.define
class _FakeProcess:
    """Stand-in for `asyncio.subprocess.Process` in `_wait_for_first_completion`
    tests -- an explicit attribute, not `SimpleNamespace`'s `__getattr__`, so it
    structurally satisfies `control_loop._HasReturncode` for pyrefly."""

    returncode: int | None = None


async def _delayed_process_exit(process, *, delay: float, returncode: int = 0) -> int:
    """Stand-in for `launched.process.wait()` -- like the real coroutine, only
    returns once `returncode` is already set, so callers can't observe one
    without the other."""
    await asyncio.sleep(delay)
    process.returncode = returncode
    return returncode


async def test_wait_for_first_completion_returns_process_done_immediately():
    """The common case: the process actually exits and gets reaped before any
    dispatcher task notices -- no grace-period wait needed."""
    process = _FakeProcess()
    process_done = asyncio.ensure_future(_delayed_process_exit(process, delay=0.01))
    watcher = await _never_completing_task()
    scheduled_shutdown = await _never_completing_task()
    try:
        finished = await asyncio.wait_for(
            _wait_for_first_completion(
                main_tasks=[watcher, scheduled_shutdown],
                process_done=process_done,
                dispatcher_tasks=[],
                process=process,
                grace_period=5.0,
            ),
            timeout=2.0,
        )
    finally:
        watcher.cancel()
        scheduled_shutdown.cancel()

    assert finished is process_done
    assert process.returncode == 0


async def test_wait_for_first_completion_grace_period_catches_a_delayed_reap():
    """The bug this guards against (confirmed live, 2026-09-14): a dispatcher
    task notices TWS's socket close and finishes before asyncio has reaped the
    already-exiting process. A real scheduled restart was misclassified
    `CONNECTION_LOST` this way. As long as the process is actually reaped
    within the grace period, `process.returncode` must be set by the time
    this returns -- regardless of which task it reports as `finished` --
    since `_run_one_cycle`'s own classification falls back to checking
    `returncode` directly."""
    process = _FakeProcess()
    process_done = asyncio.ensure_future(_delayed_process_exit(process, delay=0.05))
    dispatcher_task = asyncio.ensure_future(asyncio.sleep(0))
    watcher = await _never_completing_task()
    scheduled_shutdown = await _never_completing_task()
    try:
        finished = await asyncio.wait_for(
            _wait_for_first_completion(
                main_tasks=[watcher, scheduled_shutdown],
                process_done=process_done,
                dispatcher_tasks=[dispatcher_task],
                process=process,
                grace_period=2.0,
            ),
            timeout=2.0,
        )
    finally:
        watcher.cancel()
        scheduled_shutdown.cancel()

    assert finished is dispatcher_task
    assert process.returncode == 0


async def test_wait_for_first_completion_gives_up_after_grace_period(caplog):
    """The process never actually exits (a genuine `CONNECTION_LOST`) -- the
    grace-period wait must not block indefinitely."""
    process = _FakeProcess()
    process_done = asyncio.ensure_future(asyncio.sleep(3600))
    dispatcher_task = asyncio.ensure_future(asyncio.sleep(0))
    watcher = await _never_completing_task()
    scheduled_shutdown = await _never_completing_task()
    try:
        finished = await asyncio.wait_for(
            _wait_for_first_completion(
                main_tasks=[watcher, scheduled_shutdown],
                process_done=process_done,
                dispatcher_tasks=[dispatcher_task],
                process=process,
                grace_period=0.05,
            ),
            timeout=2.0,
        )
    finally:
        watcher.cancel()
        scheduled_shutdown.cancel()
        process_done.cancel()

    assert finished is dispatcher_task
    assert process.returncode is None


async def test_wait_for_first_completion_returns_watcher_without_grace_delay():
    """`watcher`/`scheduled_shutdown` finishing is never a process-exit
    candidate -- no grace-period wait should apply, so this returns promptly
    even with a long grace period and a process that never exits."""
    process = _FakeProcess()
    process_done = asyncio.ensure_future(asyncio.sleep(3600))
    scheduled_shutdown = await _never_completing_task()

    async def _watcher_raises():
        raise RuntimeError("boom")

    watcher = asyncio.ensure_future(_watcher_raises())
    try:
        finished = await asyncio.wait_for(
            _wait_for_first_completion(
                main_tasks=[watcher, scheduled_shutdown],
                process_done=process_done,
                dispatcher_tasks=[],
                process=process,
                grace_period=5.0,
            ),
            timeout=1.0,
        )
    finally:
        scheduled_shutdown.cancel()
        process_done.cancel()
        with contextlib.suppress(RuntimeError):
            await watcher

    assert finished is watcher
    assert process.returncode is None


async def test_run_phase_watching_process_returns_normally_when_phase_wins():
    """The common case: login/settings completes before the process ever
    exits -- no `_PhaseAborted`, `phase`'s own result is what matters."""
    process = _FakeProcess()
    process_done = await _never_completing_task()
    try:
        await _run_phase_watching_process(
            asyncio.sleep(0),
            process_done=process_done,
            dispatcher_tasks=[],
            process=process,
        )
    finally:
        process_done.cancel()


async def test_run_phase_watching_process_propagates_a_real_phase_failure():
    """A genuine login/settings failure (e.g. `LoginError`) is not caused by
    the process dying -- it must propagate unchanged, not be reclassified as
    a `ShutdownCause` (#43/#48's fix must not swallow real login failures)."""
    process = _FakeProcess()
    process_done = await _never_completing_task()

    async def _phase_raises():
        raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            await _run_phase_watching_process(
                _phase_raises(),
                process_done=process_done,
                dispatcher_tasks=[],
                process=process,
            )
    finally:
        process_done.cancel()


async def test_run_phase_watching_process_aborts_on_process_exit():
    """Issue #43's shape: the process exits (e.g. the login window is closed
    manually) while `phase` is still waiting on an event that will now never
    arrive -- must raise `_PhaseAborted(PROCESS_EXITED)` promptly instead of
    hanging on `phase`'s own (possibly unbounded) timeout, and must cancel
    the now-pointless `phase` task."""
    process = _FakeProcess()
    process_done = asyncio.ensure_future(_delayed_process_exit(process, delay=0.01))
    phase_task = asyncio.ensure_future(asyncio.sleep(3600))
    with pytest.raises(_PhaseAborted) as exc_info:
        await asyncio.wait_for(
            _run_phase_watching_process(
                phase_task,
                process_done=process_done,
                dispatcher_tasks=[],
                process=process,
            ),
            timeout=2.0,
        )
    assert exc_info.value.cause is ShutdownCause.PROCESS_EXITED
    assert phase_task.cancelled()


async def test_run_phase_watching_process_aborts_on_connection_lost():
    """Issue #48's shape: the process is still alive but a dispatcher task
    (the socket connection) has ended -- classified `CONNECTION_LOST`, the
    same distinction `_run_one_cycle`'s READY-state wait already makes."""
    process = _FakeProcess()
    process_done = await _never_completing_task()
    dispatcher_task = asyncio.ensure_future(asyncio.sleep(0))
    phase_task = asyncio.ensure_future(asyncio.sleep(3600))
    try:
        with pytest.raises(_PhaseAborted) as exc_info:
            await asyncio.wait_for(
                _run_phase_watching_process(
                    phase_task,
                    process_done=process_done,
                    dispatcher_tasks=[dispatcher_task],
                    process=process,
                ),
                timeout=5.0,
            )
        assert exc_info.value.cause is ShutdownCause.CONNECTION_LOST
        assert phase_task.cancelled()
    finally:
        process_done.cancel()


async def test_run_control_loop_cold_restart_relaunches_with_no_restart_hash(
    monkeypatch, caplog
):
    """COLD_RESTART deliberately forces a full fresh login on the next lap --
    unlike PROCESS_EXITED's marker-gated silent relogin, it must pass
    `restart_hash=None` to the next `_run_one_cycle` call regardless of
    anything a marker file might say, since the entire point of cold restart
    is forcing reauth."""
    calls: list[dict | None] = []

    async def _fake_run_one_cycle(config, agent_jar, labels, *, restart_hash=None):
        calls.append(restart_hash)
        if len(calls) == 1:
            return ShutdownCause.COLD_RESTART, f"{gettempdir()}/settings"
        return ShutdownCause.LOGIN_FAILED, f"{gettempdir()}/settings"

    monkeypatch.setattr(control_loop, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(control_loop, "stop_logging", lambda: None)

    with caplog.at_level(logging.WARNING, logger="ibcontroller.control_loop"):
        cause = await run_control_loop(_config(), "agent.jar")

    assert cause is ShutdownCause.LOGIN_FAILED
    assert calls == [None, None]
    assert any("cold_restart_time reached" in message for message in caplog.messages)


async def test_run_control_loop_tidy_closedown_stops_without_relaunch(monkeypatch):
    """TIDY_CLOSEDOWN needs no new branch -- it falls through to the existing
    `return cause`, same as every other terminal cause, so the loop must not
    call `_run_one_cycle` a second time."""
    calls = 0

    async def _fake_run_one_cycle(config, agent_jar, labels, *, restart_hash=None):
        nonlocal calls
        calls += 1
        return ShutdownCause.TIDY_CLOSEDOWN, f"{gettempdir()}/settings"

    monkeypatch.setattr(control_loop, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(control_loop, "stop_logging", lambda: None)

    cause = await run_control_loop(_config(), "agent.jar")

    assert cause is ShutdownCause.TIDY_CLOSEDOWN
    assert calls == 1


def _tracking_responder(calls: list[dict]):
    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
        return {"ok": True}

    return responder


async def test_apply_declarative_settings_applies_builtin_with_no_settings_file(
    sock_path, event_sock_path
):
    """The real gap this fix closes: `read_only_api` set in `ibcontroller.toml`
    alone, with `Config.settings_file` left at its `None` default, used to be a
    silent no-op -- nothing ever opened the Configuration dialog. Now the
    built-in entry applies from `Config` alone."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = Dispatcher(
            AgentCommandConnection(sock_path), AgentEventConnection(event_sock_path)
        )
        await dispatcher.start()
        launched = types.SimpleNamespace(dispatcher=dispatcher)
        config = _config(settings_file=None, read_only_api=False)
        try:
            task = asyncio.ensure_future(
                _apply_declarative_settings(
                    launched,  # type: ignore[arg-type]
                    LABELS,
                    config,
                    None,
                )
            )
            await asyncio.sleep(0.02)
            dispatcher.window_events.emit(
                WindowEvent(
                    seq=1,
                    kind="window_opened",
                    window=WindowInfo(
                        class_="feature.configure.ai",
                        title="DU123 Trader Workstation Configuration "
                        "(Simulated Trading)",
                        window_id="w9",
                    ),
                )
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "set_checkbox",
        "target": "Read-Only API",
        "checked": False,
        "window_id": "w9",
    } in calls


async def test_apply_declarative_settings_applies_builtin_when_user_file_missing(
    sock_path, event_sock_path, caplog
):
    """Issues #27/#30: a `Config.settings_file` that fails to load (missing
    here) used to abort the whole function before `open_settings_dialog` ever
    ran, silently skipping the built-in `read_only_api` entry too. Now the
    load failure is isolated and the built-in entry still applies."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = Dispatcher(
            AgentCommandConnection(sock_path), AgentEventConnection(event_sock_path)
        )
        await dispatcher.start()
        launched = types.SimpleNamespace(dispatcher=dispatcher)
        config = _config(
            settings_file="/nonexistent/ibkr_settings.toml", read_only_api=False
        )
        try:
            with caplog.at_level(logging.ERROR, logger="ibcontroller.control_loop"):
                task = asyncio.ensure_future(
                    _apply_declarative_settings(
                        launched,  # type: ignore[arg-type]
                        LABELS,
                        config,
                        None,
                    )
                )
                await asyncio.sleep(0.02)
                dispatcher.window_events.emit(
                    WindowEvent(
                        seq=1,
                        kind="window_opened",
                        window=WindowInfo(
                            class_="feature.configure.ai",
                            title="DU123 Trader Workstation Configuration "
                            "(Simulated Trading)",
                            window_id="w9",
                        ),
                    )
                )
                await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "set_checkbox",
        "target": "Read-Only API",
        "checked": False,
        "window_id": "w9",
    } in calls
    assert any(
        "could not load" in message and "nonexistent" in message
        for message in caplog.messages
    )


async def test_apply_declarative_settings_closes_dialog_even_when_apply_raises(
    sock_path, event_sock_path, monkeypatch
):
    """Real, live-caught bug's fix (2026-09-12, issue #1): a `finally` around
    `apply_settings_from_file` now guarantees `close_settings_dialog` runs even
    when applying entries fails outright -- before this fix, the Configuration
    dialog was left open on the live Gateway for good. Forces an exception past
    `apply_settings_from_file`'s own per-entry isolation (which only catches
    `SettingsError`/`ElementNotFoundError`) to exercise the guaranteed-close
    path specifically, not the per-entry-skip path already covered in
    `test_settings.py`."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)

    async def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(control_loop, "apply_settings_from_file", _raise)

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = Dispatcher(
            AgentCommandConnection(sock_path), AgentEventConnection(event_sock_path)
        )
        await dispatcher.start()
        launched = types.SimpleNamespace(dispatcher=dispatcher)
        config = _config(settings_file=None, read_only_api=False)
        try:
            task = asyncio.ensure_future(
                _apply_declarative_settings(
                    launched,  # type: ignore[arg-type]
                    LABELS,
                    config,
                    None,
                )
            )
            await asyncio.sleep(0.02)
            dispatcher.window_events.emit(
                WindowEvent(
                    seq=1,
                    kind="window_opened",
                    window=WindowInfo(
                        class_="feature.configure.ai",
                        title="DU123 Trader Workstation Configuration "
                        "(Simulated Trading)",
                        window_id="w9",
                    ),
                )
            )
            with pytest.raises(RuntimeError, match="boom"):
                await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert {"cmd": "click", "target": "OK", "window_id": "w9"} in calls


async def test_apply_declarative_settings_passes_second_factor_timeout(
    monkeypatch,
):
    """#37 follow-up: `open_settings_dialog`'s own `timeout` default (180.0)
    must not be relied on silently -- it needs to track
    `Config.second_factor_authentication_timeout` (the same field
    `login.py` already reads), so a deployment that changes one also
    changes the other.

    Also covers #40: `main_window_id` (`LoginManager.main_window_id`) must
    reach `open_settings_dialog` unchanged, so `navigate_menu` scopes to the
    already-validated main window instead of a global search."""
    captured: dict[str, object] = {}

    async def _fake_open_settings_dialog(*_args, **kwargs):
        captured.update(kwargs)
        return "w9"

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        control_loop, "open_settings_dialog", _fake_open_settings_dialog
    )
    monkeypatch.setattr(control_loop, "apply_settings_from_file", _noop)
    monkeypatch.setattr(control_loop, "close_settings_dialog", _noop)

    launched = types.SimpleNamespace(dispatcher=None)
    config = _config(
        settings_file=None,
        read_only_api=False,
        second_factor_authentication_timeout=42.0,
    )
    await _apply_declarative_settings(
        launched,  # type: ignore[arg-type]
        LABELS,
        config,
        "w5",
    )

    assert captured["timeout"] == 42.0
    assert captured["main_window_id"] == "w5"
