"""control_loop.py -- the top-level orchestrator.

launch -> login (with the always-on background watcher running concurrently) ->
declarative settings (built-in entries always, a user's own ibkr_settings.toml
merged in when configured -- see settings.py's own "Two tiers, not one" section) ->
a main loop that keeps the instance alive -> clean shutdown, whatever the reason.

Ties together L1/L5/L6/L7 (`launcher.py`/`recognisers.py`/`login.py`/
`settings.py`) into one continuously-running instance session.

Not a state machine -- a fixed sequence (like Login and Settings) that falls
into an indefinite wait once there's nothing left to actively do. The real
work for as long as an instance runs happens in `watch_for_unprompted_windows`
(a background task) and `Dispatcher`'s own event-reader/command-worker tasks;
this module's "main loop" just waits for one of those to end, or for the
caller to cancel it.

Shutdown has six causes, raced together (`asyncio.wait(...,
return_when=FIRST_COMPLETED)`), all converging on one exit path:

- **REQUESTED**: caller cancels this coroutine's own task.
- **PROCESS_EXITED**: `launched.process.wait()` returns -- a manual close, or
  a scheduled daily restart (confirmed live: it kills the whole JVM, not an
  in-place UI restart).
- **CONNECTION_LOST**: `Dispatcher.tasks` ends while the process is still
  alive -- a distinct failure mode from process-exit.
- **LOGIN_FAILED**: `watch_for_unprompted_windows` raises
  (`recognisers.LoginFailedError`, or `login.LoginError` from the 2FA-timeout
  watchdog).
- **COLD_RESTART**: `Config.cold_restart_time` reached (TWS and Gateway
  alike, see `schedule.py`) -- a self-scheduled tidy close-down followed by a
  full fresh relogin via `launch_instance` (no restart hash, deliberately not
  a silent relogin, and not IBC's native-launcher `File > Restart`
  mechanism), forcing the weekly full reauth IBKR requires around Sunday
  01:00 US/Eastern token invalidation.
- **TIDY_CLOSEDOWN**: `Config.closedown_at` reached (TWS and Gateway alike)
  -- a self-scheduled tidy close-down with no relaunch; the control loop
  stops for good, same as any other terminal cause.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

from ibcontroller.config import Config
from ibcontroller.labels import Labels, load_labels
from ibcontroller.launcher import LaunchedInstance, clean_shutdown, launch_instance
from ibcontroller.logging_setup import stop_logging
from ibcontroller.login import LoginManager, find_autorestart_hash, is_restart
from ibcontroller.recognisers import (
    AcceptIncomingConnectionsRecognizer,
    DeclarativeDismissRecognizer,
    ExistingSessionRecognizer,
    LoginFailedRecognizer,
    RecognizerRegistry,
    TooManyFailedLoginAttemptsRecognizer,
    watch_for_unprompted_windows,
)
from ibcontroller.schedule import ScheduledAction, next_scheduled_shutdown
from ibcontroller.settings import (
    SettingsError,
    SettingsFile,
    apply_settings_from_file,
    close_settings_dialog,
    load_builtin_settings_file,
    load_settings_file,
    merge_settings_files,
    open_settings_dialog,
)

logger = logging.getLogger(__name__)


class ShutdownCause(Enum):
    REQUESTED = auto()
    PROCESS_EXITED = auto()
    CONNECTION_LOST = auto()
    LOGIN_FAILED = auto()
    COLD_RESTART = auto()
    TIDY_CLOSEDOWN = auto()


class StartupState(Enum):
    """One instance's coarse lifecycle step, logged at each transition.
    Observer only -- has zero effect on control flow.

    `LOGGING_IN` deliberately covers both a fresh login and a restart-skip
    relogin -- `login.py`'s own `LoginManager.state` (a finer `LoginState`
    enum) already distinguishes those; this coarser field doesn't need to
    duplicate it."""

    LAUNCHING = auto()
    LOGGING_IN = auto()
    APPLYING_SETTINGS = auto()
    READY = auto()
    SHUTTING_DOWN = auto()


def _log_transition(previous: StartupState, new: StartupState) -> None:
    logger.info("IBController > startup state: %s -> %s", previous.name, new.name)


def _build_registry(
    config: Config, labels: Labels, manager: LoginManager
) -> RecognizerRegistry:
    # Declarative rules first, hand-written built-ins as the fallback --
    # matching RecognizerRegistry's own stated priority order (config
    # rules/plugins checked first, built-ins checked last).
    declarative: list[DeclarativeDismissRecognizer] = [
        DeclarativeDismissRecognizer(rule) for rule in labels.dismiss_rules
    ]
    return RecognizerRegistry(
        [
            *declarative,
            ExistingSessionRecognizer(
                labels.existing_session,
                config.existing_session_action,
                manager.is_logged_in,
            ),
            AcceptIncomingConnectionsRecognizer(
                labels.accept_incoming_connection,
                config.accept_incoming_connections,
            ),
            LoginFailedRecognizer(labels.login_failed),
            TooManyFailedLoginAttemptsRecognizer(
                labels.too_many_failed_login_attempts,
                config.relogin_after_2fa_timeout,
                manager.schedule_retry,
            ),
        ]
    )


async def _apply_declarative_settings(
    launched: LaunchedInstance, labels: Labels, config: Config
) -> None:
    """Applies the bundled built-in entries (`read_only_api`/
    `auto_restart_time`, from `Config` alone -- see `settings.py`'s own "Two
    tiers, not one" section), merged with a user's own `Config.settings_file`
    when set.

    `window_id` (from `open_settings_dialog`'s return value) scopes every
    subsequent action to the exact dialog that opened, and `close_settings_dialog`
    always runs in `finally` -- both entries applying cleanly and one raising
    still leave the dialog closed.

    A `config.settings_file` that fails to load (missing, invalid TOML, bad
    structure -- `settings.SettingsError`) is logged and treated as absent
    rather than aborting this whole function -- otherwise a typo'd path or a
    bad edit would silently take the load-bearing built-in entries
    (`read_only_api`, `auto_restart_time`) down with it, since this runs
    before `open_settings_dialog`."""
    builtin_settings = await load_builtin_settings_file()
    user_settings = None
    if config.settings_file is not None:
        try:
            user_settings = await load_settings_file(config.settings_file)
        except SettingsError as exc:
            logger.error(
                "IBController > settings: could not load %s -- continuing "
                "with built-in settings only: %s",
                config.settings_file,
                exc,
            )
    merged = merge_settings_files(builtin_settings, user_settings)
    # open_settings_dialog -> apply_settings_from_file -> close_settings_dialog
    window_id = await open_settings_dialog(
        launched.dispatcher,
        labels.settings,
        program=config.program,
    )
    try:
        await apply_settings_from_file(
            launched.dispatcher,
            labels,
            config,
            SettingsFile(settings=merged),
            window_id=window_id,
        )
    finally:
        await close_settings_dialog(
            launched.dispatcher, labels.settings, window_id=window_id
        )
    if user_settings is not None:
        logger.info(
            "IBController > declarative settings applied (built-in + %s)",
            config.settings_file,
        )
    else:
        logger.info("IBController > declarative settings applied (built-in only)")


async def _apply_declarative_settings_or_log(
    launched: LaunchedInstance, labels: Labels, config: Config
) -> None:
    """`_apply_declarative_settings`, with a local error boundary -- a
    Settings-application failure must not tear down an otherwise-healthy,
    already-logged-in session. Matches IBC's own `ConfigurationTask.java`,
    which recovers from exactly this step (`catch (Exception e) {
    Utils.logException(e); }`) while every other step stays fatal on an
    unhandled exception."""
    try:
        await _apply_declarative_settings(launched, labels, config)
    except Exception:
        logger.exception(
            "IBController > declarative settings application failed -- "
            "continuing without them"
        )


async def run_control_loop(
    config: Config, agent_jar: str | Path, *, labels: Labels | None = None
) -> ShutdownCause:
    """The outer loop -- ports IBC's own `ibcstart.sh` shape (a `while` loop
    wrapped around the entire JVM invocation) around one lap of
    `_run_one_cycle`. Gateway/TWS restarts on a schedule daily and that
    restart kills the whole JVM, so a naive single-lap loop would stop on the
    first restart. On `PROCESS_EXITED`, checks the `autorestart` marker file
    (`login.is_restart`) -- if present, Gateway expects a relaunch that skips
    full authentication, so loop back to `launch_instance`; if absent, this
    was a genuine unscheduled exit and the loop stops for good. Every other
    cause returns immediately -- only a confirmed scheduled restart loops.

    On a confirmed restart, extracts the marker's account hash
    (`login.find_autorestart_hash`) and passes it to the next lap's
    `launch_instance`, which adds `-Drestart=<hash>` to the JVM command line
    -- matching IBC's own `ibcstart.sh` and ibctl; the marker's mere presence
    is not sufficient on its own.

    Cancelling this coroutine's own task requests a graceful stop at any
    point, including mid-relaunch, and always returns
    `ShutdownCause.REQUESTED` rather than propagating `CancelledError` --
    this coroutine is the documented cancellation boundary, so nothing else
    needs a `try`/`except CancelledError` around it."""
    labels = labels if labels is not None else load_labels()
    restart_hash: str | None = None
    try:
        while True:
            cause, settings_dir = await _run_one_cycle(
                config, agent_jar, labels, restart_hash=restart_hash
            )
            if cause is ShutdownCause.COLD_RESTART:
                restart_hash = None
                logger.warning(
                    "IBController > cold_restart_time reached -- relaunching "
                    "with a full fresh login (no restart hash)"
                )
                continue
            if cause is ShutdownCause.PROCESS_EXITED and await _is_restart_with_grace(
                settings_dir
            ):
                restart_hash = await find_autorestart_hash(settings_dir)
                logger.warning(
                    "Gateway restarted on its own schedule (autorestart marker "
                    "present at %s, hash=%s) -- relaunching",
                    settings_dir,
                    restart_hash,
                )
                continue
            return cause
    except asyncio.CancelledError:
        logger.info("IBController > control loop stop requested (task cancelled)")
        return ShutdownCause.REQUESTED
    finally:
        # Drain `logging_setup`'s listener threads on the way out -- whatever the
        # last lap logged or traced is still in its queues until this joins them.
        stop_logging()


async def _is_restart_with_grace(
    settings_dir: str, *, grace_period: float = 5.0, poll_interval: float = 0.2
) -> bool:
    """`is_restart()`, retried for a short bounded window -- the `autorestart`
    marker can show up on disk slightly after `process.wait()` returns, so a
    single immediate check can miss a genuine restart. Same shape as
    `settings._await_menu_ready`'s splash-close wait: poll a short bounded
    window rather than trusting an instant check or waiting forever."""
    for _ in range(int(grace_period / poll_interval) + 1):
        if await is_restart(settings_dir):
            return True
        await asyncio.sleep(poll_interval)
    return False


async def _sleep_until_scheduled_shutdown(config: Config) -> ShutdownCause:
    """Sleeps until `schedule.next_scheduled_shutdown` says to act, then
    returns the matching `ShutdownCause` -- raced alongside `watcher`/
    `process_done` in `_run_one_cycle`'s own `asyncio.wait`. Sleeps forever
    (until cancelled by that race's `finally`) if neither `cold_restart_time`
    nor `closedown_at` applies, so this task never itself decides to end the
    cycle in that case.

    Uses `asyncio.sleep`, not `loop.call_at` -- matches every other scheduled
    wait in this codebase (`login.py`'s `schedule_retry`,
    `_is_restart_with_grace` above, `launcher._wait_for_ready`); `call_at`
    schedules against the monotonic loop clock, the wrong primitive for a
    wall-clock target like "next Sunday 07:05 local time". Recomputed fresh
    each `_run_one_cycle` lap (never carried over from a previous cycle), so
    this only matters within one already-running cycle, not indefinitely."""
    scheduled = next_scheduled_shutdown(config, datetime.now())
    if scheduled is None:
        await asyncio.Event().wait()
        raise AssertionError("unreachable: the Event above is never set")
    delay = max((scheduled.at - datetime.now()).total_seconds(), 0.0)
    logger.info(
        "IBController > scheduled %s at %s (in %.0fs)",
        scheduled.action.name,
        scheduled.at,
        delay,
    )
    await asyncio.sleep(delay)
    return (
        ShutdownCause.COLD_RESTART
        if scheduled.action is ScheduledAction.COLD_RESTART
        else ShutdownCause.TIDY_CLOSEDOWN
    )


async def _run_one_cycle(  # noqa: PLR0915
    config: Config,
    agent_jar: str | Path,
    labels: Labels,
    *,
    restart_hash: str | None = None,
) -> tuple[ShutdownCause, str]:
    """One lap: launch -> login (+ background watcher) -> settings (if
    configured) -> wait until something ends -> clean shutdown. Returns
    `(cause, settings_dir)` so `run_control_loop` can check `is_restart`
    against the same directory this lap actually used. Propagates any
    exception from login itself (`LoginError`/`LoginFailedError`/
    `TimeoutError`) unchanged -- a failed login is not a `ShutdownCause`.

    `restart_hash`, when set, is passed to `launch_instance` (see
    `run_control_loop`) and to `LoginManager` as `restart_expected` rather
    than letting `login.py` re-derive the same fact from a later filesystem
    check -- Gateway can delete the marker quickly as part of a successful
    silent relogin, so a later check could wrongly find it gone and attempt
    a full credential fill on top of an already-succeeding relogin."""
    state = StartupState.LAUNCHING
    launched = await launch_instance(config, agent_jar, restart_hash=restart_hash)
    manager = LoginManager(
        config,
        labels,
        launched.dispatcher,
        settings_dir=launched.settings_dir,
        restart_expected=restart_hash is not None,
    )
    registry = _build_registry(config, labels, manager)
    watcher = asyncio.ensure_future(
        watch_for_unprompted_windows(registry, launched.dispatcher)
    )
    cause = ShutdownCause.REQUESTED
    try:
        _log_transition(state, StartupState.LOGGING_IN)
        state = StartupState.LOGGING_IN
        await manager.run()
        logger.info("IBController > login completed, state=%s", manager.state)

        _log_transition(state, StartupState.APPLYING_SETTINGS)
        state = StartupState.APPLYING_SETTINGS
        await _apply_declarative_settings_or_log(launched, labels, config)

        _log_transition(state, StartupState.READY)
        state = StartupState.READY

        process_done = asyncio.ensure_future(launched.process.wait())
        scheduled_shutdown = asyncio.ensure_future(
            _sleep_until_scheduled_shutdown(config)
        )
        try:
            done, _pending = await asyncio.wait(
                [
                    watcher,
                    process_done,
                    scheduled_shutdown,
                    *launched.dispatcher.tasks,
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            process_done.cancel()
            scheduled_shutdown.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await process_done
            with contextlib.suppress(asyncio.CancelledError):
                await scheduled_shutdown

        finished = next(iter(done))
        if finished is watcher:
            exc = watcher.exception()
            cause = ShutdownCause.LOGIN_FAILED
            logger.warning("background watcher ended, raising=%r", exc)
        elif finished is scheduled_shutdown:
            cause = scheduled_shutdown.result()
            logger.warning("IBController > scheduled shutdown fired: %s", cause.name)
        elif finished is process_done or launched.process.returncode is not None:
            # A restart kills the process and every socket it held at once, so
            # `asyncio.wait`'s FIRST_COMPLETED race between `process_done` and
            # the event-reader noticing EOF isn't a reliable "process died" vs
            # "connection dropped" signal. `returncode` is ground truth.
            cause = ShutdownCause.PROCESS_EXITED
            logger.warning(
                "agent process exited on its own (returncode=%s)",
                launched.process.returncode,
            )
        else:
            cause = ShutdownCause.CONNECTION_LOST
            logger.warning(
                "dispatcher background task ended unexpectedly, process still "
                "alive: %r",
                finished,
            )
    except Exception:
        # `cause` defaults to REQUESTED (placeholder, above) -- log here so a
        # genuine mid-cycle failure isn't misreported as a clean requested
        # stop by the unconditional log line after `finally`. `except
        # Exception` (not `BaseException`) leaves `asyncio.CancelledError`
        # untouched, so a real cancellation still propagates.
        logger.exception(
            "IBController > control loop cycle aborted by an unhandled exception"
        )
        raise
    finally:
        _log_transition(state, StartupState.SHUTTING_DOWN)
        state = StartupState.SHUTTING_DOWN
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await clean_shutdown(
            launched,
            program=config.program,
            logged_in=manager.is_logged_in(),
            labels=labels.shutdown,
        )
    logger.info("IBController > control loop cycle stopped: %s", cause.name)
    return cause, launched.settings_dir
