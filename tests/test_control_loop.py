"""Unit tests for control_loop.py's pure, no-live-Gateway-needed half --
`StartupState`/`_log_transition`, `build_default_registry` wiring, and
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
    ElementNotFoundError,
    ProtocolError,
)
from ibcontroller.config import Config, MfaTimeoutAction, Program, TradingMode
from ibcontroller.control_loop import (
    ShutdownCause,
    StartupState,
    _apply_declarative_settings_or_log,
    _log_transition,
    _PhaseAborted,
    _run_phase_watching_process,
    _sleep_until_scheduled_shutdown,
    _wait_for_first_completion,
    run_control_loop,
)
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import load_labels
from ibcontroller.launcher import LaunchedInstance
from ibcontroller.login import (
    CredentialsRejectedError,
    LoginFrameTimeoutError,
    MfaTimeoutError,
    TransientLoginError,
)
from ibcontroller.schedule import ScheduledAction, ScheduledShutdown
from tests.fakes import FakeProcess

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
        program=Program.GATEWAY,
        tws_version="10.50",
        trading_mode=TradingMode.PAPER,
        userid=Secret("u"),
        password=Secret("p"),
        log_dir=f"{gettempdir()}/log",
    )
    return attrs.evolve(base, **overrides)


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

    task = asyncio.ensure_future(
        run_control_loop(_config(), "agent.jar", labels=LABELS)
    )
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
    config = _config(program=Program.TWS, closedown_at="22:00")
    cause = await asyncio.wait_for(_sleep_until_scheduled_shutdown(config), timeout=5.0)
    assert cause is ShutdownCause.TIDY_CLOSEDOWN


async def test_sleep_until_scheduled_shutdown_follows_wall_clock_jump(monkeypatch):
    # Wall clock jumps forward (DST, host resume) while the monotonic clock
    # barely moves: the sleep must notice on its next chunk and fire.
    start = datetime(2026, 3, 8, 1, 0)
    clock = {"now": start}

    fake_datetime = types.SimpleNamespace(now=lambda: clock["now"])
    target = ScheduledShutdown(
        at=start + timedelta(hours=2), action=ScheduledAction.COLD_RESTART
    )
    monkeypatch.setattr(control_loop, "datetime", fake_datetime)
    monkeypatch.setattr(
        control_loop, "next_scheduled_shutdown", lambda config, now: target
    )
    monkeypatch.setattr(control_loop, "_SCHEDULE_POLL_SECONDS", 0.01)
    task = asyncio.ensure_future(
        _sleep_until_scheduled_shutdown(_config(cold_restart_time="Sunday 03:00"))
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # target still two wall-clock hours away
    clock["now"] = start + timedelta(hours=2)  # clocks jump
    cause = await asyncio.wait_for(task, timeout=5.0)
    assert cause is ShutdownCause.COLD_RESTART


async def test_sleep_until_scheduled_shutdown_sleeps_forever_when_unconfigured():
    config = _config(program=Program.TWS)
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


@pytest.mark.parametrize("exc_type", [CredentialsRejectedError, TransientLoginError])
async def test_ready_state_watcher_exception_propagates_unchanged(exc_type):
    """The edge case login succeeds, then the watcher raises later (e.g. a
    reconnect attempt hits a credential or transient dialog): `_run_one_cycle`'s
    READY-state wait must propagate the watcher's real exception type, exactly
    like `_run_phase_watching_process` does during login/settings -- not
    collapse it into a fixed `ShutdownCause` the way it used to (gitea #62).
    Exercises the same one-line `if finished is watcher: await watcher` shape
    the READY block uses, the smallest piece that can be isolated without a
    real launch/login sequence (this module's own stated testing boundary)."""
    process = _FakeProcess()
    process_done = await _never_completing_task()
    scheduled_shutdown = await _never_completing_task()

    async def _watcher_raises():
        raise exc_type("post-login failure")

    watcher = asyncio.ensure_future(_watcher_raises())
    try:
        finished = await asyncio.wait_for(
            _wait_for_first_completion(
                main_tasks=[watcher, scheduled_shutdown],
                process_done=process_done,
                dispatcher_tasks=[],
                process=process,
            ),
            timeout=1.0,
        )
        assert finished is watcher
        with pytest.raises(exc_type, match="post-login failure"):
            await watcher
    finally:
        scheduled_shutdown.cancel()
        process_done.cancel()


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


@pytest.mark.parametrize(
    ("returncode", "expected_cause"),
    [
        (0, ShutdownCause.PROCESS_CLOSED),  # exited by choice, e.g. File>Close
        (-9, ShutdownCause.PROCESS_EXITED),  # killed
    ],
)
async def test_run_phase_watching_process_aborts_on_process_exit(
    returncode, expected_cause
):
    """Issue #43's shape: the process exits (e.g. the login window is closed
    manually) while `phase` is still waiting on an event that will now never
    arrive -- must raise `_PhaseAborted` promptly instead of hanging on
    `phase`'s own (possibly unbounded) timeout, and must cancel the
    now-pointless `phase` task. Returncode 0 is `PROCESS_CLOSED`, anything
    else `PROCESS_EXITED`."""
    process = _FakeProcess()
    process_done = asyncio.ensure_future(
        _delayed_process_exit(process, delay=0.01, returncode=returncode)
    )
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
    assert exc_info.value.cause is expected_cause
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


async def test_run_phase_watching_process_propagates_watcher_failure_immediately():
    """Gitea #62's root cause 2: a watcher exception during login must abort
    `phase` right away, not be missed until `phase`'s own (much longer)
    timeout expires."""
    process = _FakeProcess()
    process_done = await _never_completing_task()

    async def _watcher_raises():
        await asyncio.sleep(0)
        raise CredentialsRejectedError("unrecognized username or password")

    watcher = asyncio.ensure_future(_watcher_raises())
    phase_task = asyncio.ensure_future(asyncio.sleep(3600))  # stands in for mfa_timeout
    try:
        with pytest.raises(CredentialsRejectedError):
            await asyncio.wait_for(
                _run_phase_watching_process(
                    phase_task,
                    process_done=process_done,
                    dispatcher_tasks=[],
                    process=process,
                    watcher=watcher,
                ),
                timeout=1.0,
            )
    finally:
        process_done.cancel()
    assert phase_task.cancelled()


async def test_run_phase_watching_process_leaves_watcher_running_when_phase_wins():
    """`watcher` is shared across phases -- it must not be cancelled here,
    only `phase` is this function's own task to own."""
    process = _FakeProcess()
    process_done = await _never_completing_task()
    watcher = await _never_completing_task()
    try:
        await _run_phase_watching_process(
            asyncio.sleep(0),
            process_done=process_done,
            dispatcher_tasks=[],
            process=process,
            watcher=watcher,
        )
        assert not watcher.done()
    finally:
        process_done.cancel()
        watcher.cancel()


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
        return ShutdownCause.CONNECTION_LOST, f"{gettempdir()}/settings"

    monkeypatch.setattr(control_loop, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(control_loop, "stop_logging", lambda: None)

    with caplog.at_level(logging.WARNING, logger="ibcontroller.control_loop"):
        cause = await run_control_loop(_config(), "agent.jar", labels=LABELS)

    assert cause is ShutdownCause.CONNECTION_LOST
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

    cause = await run_control_loop(_config(), "agent.jar", labels=LABELS)

    assert cause is ShutdownCause.TIDY_CLOSEDOWN
    assert calls == 1


async def test_run_control_loop_login_frame_timeout_always_relaunches(
    monkeypatch, caplog
):
    """`LoginFrameTimeoutError` (IBC error code 1112) always relaunches,
    unconditionally -- not gated by any config field, unlike `MfaTimeoutError`
    below (gitea #60)."""
    calls: list[dict | None] = []

    async def _fake_run_one_cycle(config, agent_jar, labels, *, restart_hash=None):
        calls.append(restart_hash)
        if len(calls) == 1:
            raise LoginFrameTimeoutError("login frame never appeared")
        return ShutdownCause.CONNECTION_LOST, f"{gettempdir()}/settings"

    monkeypatch.setattr(control_loop, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(control_loop, "stop_logging", lambda: None)

    with caplog.at_level(logging.WARNING, logger="ibcontroller.control_loop"):
        cause = await run_control_loop(_config(), "agent.jar", labels=LABELS)

    assert cause is ShutdownCause.CONNECTION_LOST
    assert calls == [None, None]
    assert any("login frame never appeared" in message for message in caplog.messages)


async def test_run_control_loop_relaunches_on_transient_login_error(
    monkeypatch, caplog
):
    """`TransientLoginError` (a "Login failed"/"Login Error" dialog, or an
    unlisted Gateway "Connection to server failed" reason) always relaunches
    with a full fresh login, unconditionally -- gitea #62."""
    calls: list[dict | None] = []

    async def _fake_run_one_cycle(config, agent_jar, labels, *, restart_hash=None):
        calls.append(restart_hash)
        if len(calls) == 1:
            raise TransientLoginError("login error dialog; server disconnected")
        return ShutdownCause.CONNECTION_LOST, f"{gettempdir()}/settings"

    monkeypatch.setattr(control_loop, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(control_loop, "stop_logging", lambda: None)

    with caplog.at_level(logging.WARNING, logger="ibcontroller.control_loop"):
        cause = await run_control_loop(_config(), "agent.jar", labels=LABELS)

    assert cause is ShutdownCause.CONNECTION_LOST
    assert calls == [None, None]
    assert any("transient login failure" in message for message in caplog.messages)


@pytest.mark.parametrize(
    "cause", [ShutdownCause.PROCESS_CLOSED, ShutdownCause.PROCESS_EXITED]
)
async def test_run_control_loop_process_gone_relaunches_only_with_marker(
    monkeypatch, cause
):
    """A scheduled restart can end the JVM with either returncode, so both
    process-gone causes check the `autorestart` marker; without it the loop
    stops and returns the cause unchanged."""
    marker_present = [True, False]
    calls: list[str | None] = []

    async def _fake_run_one_cycle(config, agent_jar, labels, *, restart_hash=None):
        calls.append(restart_hash)
        return cause, f"{gettempdir()}/settings"

    async def _restart_marker(settings_dir):
        return marker_present.pop(0)

    async def _fake_find_autorestart_hash(settings_dir):
        return "abc123"

    monkeypatch.setattr(control_loop, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(control_loop, "_is_restart_with_grace", _restart_marker)
    monkeypatch.setattr(
        control_loop, "find_autorestart_hash", _fake_find_autorestart_hash
    )
    monkeypatch.setattr(control_loop, "stop_logging", lambda: None)

    assert await run_control_loop(_config(), "agent.jar", labels=LABELS) is cause
    assert calls == [None, "abc123"]


async def test_run_control_loop_login_frame_timeout_keeps_restart_hash(monkeypatch):
    """A frame timeout on the relaunch after a scheduled restart keeps the
    restart hash, matching `ibcstart.sh`'s no-op branch for IBC error code
    1112 -- the next lap still relogins silently instead of pushing 2FA."""
    calls: list[str | None] = []

    async def _fake_run_one_cycle(config, agent_jar, labels, *, restart_hash=None):
        calls.append(restart_hash)
        if len(calls) == 1:
            return ShutdownCause.PROCESS_EXITED, f"{gettempdir()}/settings"
        if len(calls) == 2:
            raise LoginFrameTimeoutError("login frame never appeared")
        return ShutdownCause.CONNECTION_LOST, f"{gettempdir()}/settings"

    async def _restart_marker_present(settings_dir):
        return True

    async def _fake_find_autorestart_hash(settings_dir):
        return "abc123"

    monkeypatch.setattr(control_loop, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(control_loop, "_is_restart_with_grace", _restart_marker_present)
    monkeypatch.setattr(
        control_loop, "find_autorestart_hash", _fake_find_autorestart_hash
    )
    monkeypatch.setattr(control_loop, "stop_logging", lambda: None)

    cause = await run_control_loop(_config(), "agent.jar", labels=LABELS)

    assert cause is ShutdownCause.CONNECTION_LOST
    assert calls == [None, "abc123", "abc123"]


async def test_run_control_loop_mfa_timeout_relaunches_when_action_is_restart(
    monkeypatch, caplog
):
    calls: list[dict | None] = []

    async def _fake_run_one_cycle(config, agent_jar, labels, *, restart_hash=None):
        calls.append(restart_hash)
        if len(calls) == 1:
            raise MfaTimeoutError("mfa_exit_interval watchdog fired")
        return ShutdownCause.CONNECTION_LOST, f"{gettempdir()}/settings"

    monkeypatch.setattr(control_loop, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(control_loop, "stop_logging", lambda: None)

    with caplog.at_level(logging.WARNING, logger="ibcontroller.control_loop"):
        cause = await run_control_loop(
            _config(mfa_timeout_action=MfaTimeoutAction.RESTART),
            "agent.jar",
            labels=LABELS,
        )

    assert cause is ShutdownCause.CONNECTION_LOST
    assert calls == [None, None]
    assert any("mfa_timeout_action=restart" in message for message in caplog.messages)


async def test_run_control_loop_mfa_timeout_propagates_when_action_is_exit(
    monkeypatch,
):
    """`mfa_timeout_action=exit`, the default -- `MfaTimeoutError` propagates
    unchanged instead of relaunching, so `cli.py` can map it to exit 75."""
    calls: list[dict | None] = []

    async def _fake_run_one_cycle(config, agent_jar, labels, *, restart_hash=None):
        calls.append(restart_hash)
        raise MfaTimeoutError("mfa_exit_interval watchdog fired")

    monkeypatch.setattr(control_loop, "_run_one_cycle", _fake_run_one_cycle)
    monkeypatch.setattr(control_loop, "stop_logging", lambda: None)

    with pytest.raises(MfaTimeoutError):
        await run_control_loop(
            _config(mfa_timeout_action=MfaTimeoutAction.EXIT),
            "agent.jar",
            labels=LABELS,
        )

    assert calls == [None]


@pytest.mark.parametrize(
    ("error", "has_traceback"),
    [
        (ElementNotFoundError("no component Configure/Nope"), False),
        (TimeoutError("dialog never opened"), False),
        (RuntimeError("bug"), True),
    ],
)
async def test_apply_declarative_settings_or_log_traceback_only_for_unexpected(
    monkeypatch, caplog, error, has_traceback
):
    async def _raise(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(control_loop, "apply_declarative_settings", _raise)

    with caplog.at_level(logging.ERROR, logger="ibcontroller.control_loop"):
        await _apply_declarative_settings_or_log(
            types.SimpleNamespace(dispatcher=None),  # type: ignore[arg-type]
            LABELS,
            _config(),
            types.SimpleNamespace(main_window_id=None),  # type: ignore[arg-type]
        )

    (record,) = caplog.records
    assert "continuing without them" in record.getMessage()
    assert (record.exc_info is not None) is has_traceback
    if not has_traceback:
        assert str(error) in record.getMessage()


async def test_run_to_completion_survives_repeated_cancellation():
    """A second SIGINT/SIGTERM during `clean_shutdown` must not abort it
    midway and leave the JVM running (#79). The shutdown still finishes, then
    the cancellation propagates."""
    finished = asyncio.Event()

    async def shutdown() -> None:
        await asyncio.sleep(0.2)
        finished.set()

    task = asyncio.ensure_future(control_loop._run_to_completion(shutdown()))
    for _ in range(3):
        await asyncio.sleep(0.02)
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_run_to_completion_propagates_failure_and_plain_success():
    async def boom() -> None:
        raise RuntimeError("shutdown broke")

    with pytest.raises(RuntimeError, match="shutdown broke"):
        await control_loop._run_to_completion(boom())
    await control_loop._run_to_completion(asyncio.sleep(0))


async def test_run_one_cycle_cancels_watcher_when_clean_shutdown_raises(
    monkeypatch, sock_path, event_sock_path
):
    """The watcher stays up during shutdown (exiting can raise dialogs), but a
    failing `clean_shutdown` must not leak it (#79). The process has already
    exited, so the cycle goes straight to shutdown."""
    watcher_cancelled = asyncio.Event()

    async def watcher(*_args, **_kwargs):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            watcher_cancelled.set()
            raise

    async def forever(*_args, **_kwargs):
        await asyncio.sleep(3600)

    class _Manager:
        state = None
        main_window_id = None

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self) -> None:
            await asyncio.sleep(3600)

        def is_logged_in(self) -> bool:
            return False

        def schedule_retry(self, _wait_seconds: float) -> None:
            pass

    async def launch(*_args, **_kwargs):
        return LaunchedInstance(
            # pyrefly: ignore [bad-argument-type]
            process=FakeProcess(returncode=0),
            dispatcher=Dispatcher(
                AgentCommandConnection(sock_path), AgentEventConnection(event_sock_path)
            ),
            command_socket_path=str(sock_path),
            event_socket_path=str(event_sock_path),
            settings_dir=f"{gettempdir()}/settings",
            stdout_drain_task=asyncio.ensure_future(asyncio.sleep(0)),
        )

    async def failing_shutdown(*_args, **_kwargs):
        raise RuntimeError("shutdown broke")

    monkeypatch.setattr(control_loop, "launch_instance", launch)
    monkeypatch.setattr(control_loop, "LoginManager", _Manager)
    monkeypatch.setattr(control_loop, "build_default_registry", lambda *_a: None)
    monkeypatch.setattr(control_loop, "watch_for_unprompted_windows", watcher)
    monkeypatch.setattr(control_loop, "watch_for_diagnostics", forever)
    monkeypatch.setattr(control_loop, "clean_shutdown", failing_shutdown)

    with pytest.raises(RuntimeError, match="shutdown broke"):
        await control_loop._run_one_cycle(_config(), "agent.jar", LABELS)
    assert watcher_cancelled.is_set()


async def test_run_one_cycle_shuts_down_when_diagnostics_watcher_died(
    monkeypatch, sock_path, event_sock_path, caplog
):
    """A background task that died with an exception must not abort the
    `finally` before `clean_shutdown` runs, or the JVM is left running (#97)."""
    shutdown_called = asyncio.Event()

    async def forever(*_args, **_kwargs):
        await asyncio.sleep(3600)

    async def dead_diagnostics(*_args, **_kwargs):
        raise ProtocolError("socket closed")

    class _Manager:
        state = None
        main_window_id = None

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run(self) -> None:
            await asyncio.sleep(3600)

        def is_logged_in(self) -> bool:
            return False

        def schedule_retry(self, _wait_seconds: float) -> None:
            pass

    async def launch(*_args, **_kwargs):
        return LaunchedInstance(
            # pyrefly: ignore [bad-argument-type]
            process=FakeProcess(returncode=0),
            dispatcher=Dispatcher(
                AgentCommandConnection(sock_path), AgentEventConnection(event_sock_path)
            ),
            command_socket_path=str(sock_path),
            event_socket_path=str(event_sock_path),
            settings_dir=f"{gettempdir()}/settings",
            stdout_drain_task=asyncio.ensure_future(asyncio.sleep(0)),
        )

    async def shutdown(*_args, **_kwargs):
        shutdown_called.set()

    monkeypatch.setattr(control_loop, "launch_instance", launch)
    monkeypatch.setattr(control_loop, "LoginManager", _Manager)
    monkeypatch.setattr(control_loop, "build_default_registry", lambda *_a: None)
    monkeypatch.setattr(control_loop, "watch_for_unprompted_windows", forever)
    monkeypatch.setattr(control_loop, "watch_for_diagnostics", dead_diagnostics)
    monkeypatch.setattr(control_loop, "clean_shutdown", shutdown)

    with caplog.at_level(logging.WARNING):
        await control_loop._run_one_cycle(_config(), "agent.jar", LABELS)
    assert shutdown_called.is_set()
    assert "diagnostics watcher ended with" in caplog.text


async def _fails_with(exc: BaseException) -> None:
    raise exc


@pytest.mark.parametrize("exc", [ProtocolError("closed"), ConnectionResetError()])
async def test_wait_for_first_completion_grace_period_applies_to_a_main_task_lost_agent(
    exc,
):
    """F2: a watcher whose command failed because the JVM went away is
    reported before `process_done`; the grace period lets `returncode` land."""
    process = _FakeProcess()
    process_done = asyncio.ensure_future(_delayed_process_exit(process, delay=0.05))
    watcher = asyncio.ensure_future(_fails_with(exc))
    try:
        finished = await asyncio.wait_for(
            _wait_for_first_completion(
                main_tasks=[watcher],
                process_done=process_done,
                dispatcher_tasks=[],
                process=process,
                grace_period=2.0,
            ),
            timeout=2.0,
        )
    finally:
        process_done.cancel()

    assert finished is watcher
    assert process.returncode == 0


async def test_wait_for_first_completion_no_grace_for_a_non_transport_failure():
    """A login-dialog outcome is not a process-exit candidate: no wait."""
    process = _FakeProcess()
    process_done = await _never_completing_task()
    watcher = asyncio.ensure_future(_fails_with(TransientLoginError("x")))
    try:
        finished = await asyncio.wait_for(
            _wait_for_first_completion(
                main_tasks=[watcher],
                process_done=process_done,
                dispatcher_tasks=[],
                process=process,
                grace_period=5.0,
            ),
            timeout=1.0,
        )
    finally:
        process_done.cancel()
        with contextlib.suppress(TransientLoginError):
            await watcher

    assert finished is watcher


@pytest.mark.parametrize(
    ("returncode", "expected_cause"),
    [(0, ShutdownCause.PROCESS_CLOSED), (-9, ShutdownCause.PROCESS_EXITED)],
)
async def test_run_phase_watching_process_aborts_when_phase_failed_as_agent_died(
    returncode, expected_cause
):
    """F2, login-phase case: `phase` ends with `ProtocolError` ahead of
    `process_done`, but the process is gone -- a process exit, not an error."""
    process = _FakeProcess()
    process_done = asyncio.ensure_future(
        _delayed_process_exit(process, delay=0.1, returncode=returncode)
    )
    try:
        with pytest.raises(_PhaseAborted) as exc_info:
            await asyncio.wait_for(
                _run_phase_watching_process(
                    _fails_with(ProtocolError("closed")),
                    process_done=process_done,
                    dispatcher_tasks=[],
                    process=process,
                ),
                timeout=3.0,
            )
    finally:
        process_done.cancel()
    assert exc_info.value.cause is expected_cause


async def test_run_phase_watching_process_aborts_when_watcher_failed_as_agent_died():
    process = _FakeProcess()
    process_done = asyncio.ensure_future(_delayed_process_exit(process, delay=0.1))
    watcher = asyncio.ensure_future(_fails_with(ProtocolError("closed")))
    phase_task = asyncio.ensure_future(asyncio.sleep(3600))
    try:
        with pytest.raises(_PhaseAborted) as exc_info:
            await asyncio.wait_for(
                _run_phase_watching_process(
                    phase_task,
                    process_done=process_done,
                    dispatcher_tasks=[],
                    process=process,
                    watcher=watcher,
                ),
                timeout=3.0,
            )
    finally:
        process_done.cancel()
    assert exc_info.value.cause is ShutdownCause.PROCESS_CLOSED


async def test_run_phase_watching_process_reraises_lost_agent_when_process_alive():
    """No process exit after the grace period: the transport error stands."""
    process = _FakeProcess()
    process_done = await _never_completing_task()
    try:
        with pytest.raises(ProtocolError):
            await _run_phase_watching_process(
                _fails_with(ProtocolError("closed")),
                process_done=process_done,
                dispatcher_tasks=[],
                process=process,
            )
    finally:
        process_done.cancel()
