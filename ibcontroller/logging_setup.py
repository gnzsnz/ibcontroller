"""Shared logging configuration.

**File handlers run on listener threads, not the caller's.** `configure_logging`'s
app-log file handler, `configure_trace`'s trace handlers, and
`configure_gateway_stdout`'s launched-process stdout handler all attach to their
loggers as `logging.handlers.QueueHandler`s, with the real `FileHandler`s owned by
per-stream `QueueListener` threads. This is the stdlib's canonical answer to
"don't block the event loop on disk I/O": a coroutine's
`logger.info(...)`/`logger.debug(...)` is a synchronous `queue.put()`; the file
write happens on the listener thread. (There is no `asyncio` logging handler in
the stdlib -- `QueueHandler`/`QueueListener` is the documented replacement, and
it is what `dispatch.py`'s trace path and `launcher.py`'s stdout-drain both rely
on to keep their per-line writes off the event loop.) The console
`StreamHandler` deliberately stays attached directly (synchronous): a tty write
is the cheap case, and keeping it sync means console output can't be lost in a
dying process. `stop_logging()` drains and joins the listener threads; call it
at the final shutdown path (control_loop) and in any test that needs to read a
file it wrote.

**Sanitization is a separate concern, already solved elsewhere, not this module's
job.** `secret.py`'s `Secret` protects a value at its own origin (`config.py`'s
`Config.username`/`password`) -- its `__str__`/`__repr__` already return
`"<redacted>"`, so a `Secret` passed into any `%s`-style log call
(`logger.info("login as %s", config.username)`) is already safe by construction,
the same way it's already safe in `dispatch.py`'s trace line. This module only
decides where log records go and how they're formatted; it doesn't need to know
`Secret` exists.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# No timestamp/level/name prefix -- for streams where each line already carries
# its own content and must not be altered: trace files (NDJSON, one line = one
# JSON object) and the launched process's own raw stdout passthrough (already
# Gateway/TWS's own formatted log output). The FileHandler's own terminator
# supplies the trailing newline.
_MESSAGE_ONLY_FORMAT = "%(message)s"
# When trace is disabled the loggers are still created (so `dispatch.py`'s
# `isEnabledFor(DEBUG)` gate is false even if a stale Dispatcher references
# them) but set above CRITICAL with no handlers.
_TRACE_LEVEL_DISABLED = logging.CRITICAL + 1

# Every live QueueListener, keyed by owner so reconfiguration and shutdown can
# find exactly the right ones: "app" for the `ibcontroller` logger's file
# handler, f"trace:{instance}" for one instance's two trace streams,
# f"stdout:{instance}" for one instance's launched-process stdout passthrough.
# Keys exist because `configure_logging`/`configure_trace`/
# `configure_gateway_stdout` are called independently (launch_instance calls
# all three, back to back); stopping a reconfiguring owner's *own* previous
# listeners must not stop another owner's.
_active_listeners: dict[str, list[logging.handlers.QueueListener]] = {}


class _FlushFileHandler(logging.FileHandler):
    """FileHandler that flushes after every record.

    Needed for the trace files specifically: `cmd-{instance}.jsonl`/
    `events-{instance}.jsonl` exist to be tailed live, and a stock FileHandler
    only flushes when a record is at or above the listener's `flush_level`
    (default: ERROR) or when the buffer fills/wraps out -- at DEBUG level lines
    would otherwise sit in the buffered stream indefinitely. Flushing per record
    restores exactly the write()+flush()-per-line behaviour `dispatch.py`'s old
    hand-rolled trace had, now running on the listener thread instead of the
    event loop."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


def _stop_listeners(key: str | None = None) -> None:
    """Stop (drain, then join) every QueueListener registered under `key`, or
    every listener when `key` is None; closes each listener's handlers, which
    flushes whatever those handlers still buffered. Idempotent."""
    keys = [key] if key is not None else list(_active_listeners)
    for k in keys:
        for listener in _active_listeners.pop(k, []):
            listener.stop()
            for handler in listener.handlers:
                handler.close()


def _queued_file_handler(
    path: Path, *, key: str, mode: str = "a", fmt: str = _FORMAT
) -> logging.handlers.QueueHandler:
    """Build one QueueHandler/QueueListener pair for `path` -- the QueueHandler
    is what callers attach to a logger (a synchronous `queue.put()`), the
    QueueListener owns the real `_FlushFileHandler` and runs it on its own
    thread. The listener is registered under `key` so `_stop_listeners`/reconfig
    can find it."""
    handler = _FlushFileHandler(path, mode=mode, encoding="utf-8")
    handler.setFormatter(logging.Formatter(fmt))
    q = queue.Queue()
    listener = logging.handlers.QueueListener(q, handler)
    listener.start()
    _active_listeners.setdefault(key, []).append(listener)
    return logging.handlers.QueueHandler(q)


def stop_logging() -> None:
    """Drain and shut down every active listener, closing its handler. Safe to
    call any number of times (idempotent). The final shutdown path
    (`control_loop.run_control_loop`) calls this; tests that assert against a
    real log/trace file must call it too, because the records they just logged
    may still be sitting in a listener's queue until the thread drains it."""
    _stop_listeners()


def configure_logging(
    *,
    level: int = logging.INFO,
    log_dir: str | Path | None = None,
    filename: str = "ibcontroller.log",
) -> None:
    """Configures the `ibcontroller` logger hierarchy (every submodule's
    `logging.getLogger(__name__)` is a child of it, e.g. `ibcontroller.dispatch`)
    -- console always (synchronous `StreamHandler`), plus a file handler under
    `log_dir` if given (`ibcontroller.log` by default, alongside `dispatch.py`'s
    own `cmd-{instance}.jsonl`/`events-{instance}.jsonl` under the same flat
    directory convention -- app_dirs.py's log dir, typically). The file handler
    is queued: the logger sees a `QueueHandler`, and the real `FileHandler`
    lives on a listener thread (see the module docstring). `propagate = False`
    so nothing double-logs through the root logger if something else ever
    configures that too. Safe to call more than once -- replaces this logger's
    handlers each time rather than accumulating them (previous "app" listeners
    are stopped first).

    **`filename` exists for the instance-isolation design principle.**
    All of ibcontroller's files live flat in one shared `log_dir` (2026-09-08:
    the old per-instance `log_dir/{instance}` subdirectory was removed), so two
    instances never collide because every filename carries the instance name --
    `launcher.py`'s `launch_instance` passes
    `filename=f"ibcontroller-{instance}.log"`, keeping the file self-identifying
    whether it's copied, tailed, or globbed alongside another instance's log in
    the same directory. This matches the convention `build_launch_plan` already
    uses for socket filenames (`ibcontroller-agent-{instance}-{cmd,events}.sock`,
    one shared runtime directory) -- one consistent scheme across all per-instance
    files.
    """
    logger = logging.getLogger("ibcontroller")
    _stop_listeners("app")
    logger.setLevel(level)
    logger.propagate = False
    logger.handlers = []

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(console)

    if log_dir is not None:
        path = Path(log_dir)
        path.mkdir(parents=True, exist_ok=True)
        logger.addHandler(
            _queued_file_handler(path / filename, key="app", mode="a", fmt=_FORMAT)
        )


def configure_trace(
    *,
    instance: str,
    enabled: bool,
    trace_dir: str | Path | None = None,
) -> None:
    """Configures one instance's two trace streams, `ibcontroller.trace.{instance}`
    child loggers `.cmd` and `.event` -- the loggers `dispatch.py`'s `Dispatcher`
    emits its NDJSON lines to (one line per command, one per raw event message).

    `enabled` must be `True` **and** `trace_dir` given for tracing to be on;
    otherwise the two loggers are set to a level above CRITICAL with no
    handlers, so `Dispatcher`'s `isEnabledFor(DEBUG)` gate is false and nothing
    is ever emitted (and no files created). When enabled, each stream gets its
    own `FileHandler` (`cmd-{instance}.jsonl`/`events-{instance}.jsonl` under
    `trace_dir`, created here) with a message-only formatter, behind its own
    QueueHandler/QueueListener pair -- file open is `"w"` (truncate on start,
    2026-09-05 per the user's own steer: a trace file only makes sense for the
    current session, stale lines from long-dead instances must not survive a
    relaunch).

    Safe to call more than once for the same instance -- previous listeners for
    that instance are stopped first. Reconfiguring a different instance leaves
    this one's streams untouched."""
    key = f"trace:{instance}"
    _stop_listeners(key)
    cmd_logger = logging.getLogger(f"ibcontroller.trace.{instance}.cmd")
    event_logger = logging.getLogger(f"ibcontroller.trace.{instance}.event")
    for trace_logger in (cmd_logger, event_logger):
        trace_logger.handlers = []
        trace_logger.propagate = False

    if not enabled or trace_dir is None:
        cmd_logger.setLevel(_TRACE_LEVEL_DISABLED)
        event_logger.setLevel(_TRACE_LEVEL_DISABLED)
        return

    path = Path(trace_dir)
    path.mkdir(parents=True, exist_ok=True)
    cmd_logger.setLevel(logging.DEBUG)
    event_logger.setLevel(logging.DEBUG)
    cmd_logger.addHandler(
        _queued_file_handler(
            path / f"cmd-{instance}.jsonl", key=key, mode="w", fmt=_MESSAGE_ONLY_FORMAT
        )
    )
    event_logger.addHandler(
        _queued_file_handler(
            path / f"events-{instance}.jsonl",
            key=key,
            mode="w",
            fmt=_MESSAGE_ONLY_FORMAT,
        )
    )


def configure_gateway_stdout(instance: str, log_dir: str | Path) -> logging.Logger:
    """Configures one instance's queued logger for the launched process's raw
    stdout (Gateway/TWS's own console/log4j output) -- `gateway-{instance}.log`
    under `log_dir`. Appended across the process's whole life (unlike
    `configure_trace`'s per-session truncate: a restarted process should keep
    adding to the same file, not lose the prior run's lines). Message-only
    formatter, since each line is already Gateway/TWS's own formatted output;
    behind the same QueueHandler/QueueListener pattern as `configure_logging`/
    `configure_trace`, so a caller feeding lines into the returned logger never
    blocks on the write.

    Safe to call more than once for the same instance -- previous listeners for
    that instance are stopped first, matching `configure_trace`'s own contract.
    """
    key = f"stdout:{instance}"
    _stop_listeners(key)
    logger = logging.getLogger(f"ibcontroller.stdout.{instance}")
    logger.handlers = []
    logger.propagate = False
    logger.setLevel(logging.INFO)

    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)
    logger.addHandler(
        _queued_file_handler(
            path / f"gateway-{instance}.log",
            key=key,
            mode="a",
            fmt=_MESSAGE_ONLY_FORMAT,
        )
    )
    return logger
