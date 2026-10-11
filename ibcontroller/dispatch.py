"""Serializes access to one running agent instance: events in, commands out,
both serialized through one `Dispatcher`. Built on `agent_client.py`'s typed
connections -- this module only ever handles `PingResult`/`Component`/
`WindowEvent` and the rest of that module's vocabulary, never a raw line or
dict itself.

One `Dispatcher` per running agent instance (live and paper are separate
objects, each with its own connections, queue, and event stream) -- nothing
here is a module-level singleton.

**Trace mode**: pass `instance` (the same name `logging_setup.configure_trace`
was called with) to emit every command and every raw event message as NDJSON
to `cmd-{instance}.jsonl`/`events-{instance}.jsonl`, via the
`ibcontroller.trace.{instance}.{cmd,event}` loggers. This module only builds
the JSON line and calls `logger.debug(line)` -- the files, handlers, and
listener threads are owned by `logging_setup.py`, so nothing here ever
blocks the event loop on a write.

Event arrivals are also logged at DEBUG in the normal app log (one
human-readable line per message, via `_describe_event`), independent of
trace mode.

Credentials never reach this file as plaintext: `Config.username`/`password`
are `Secret`, not `str`; `agent_client.set_text` is the only place that ever
unwraps one, so nothing downstream (including this module) ever sees the
bare value. A `Secret` passed as a `set_text` argument serializes as
`"Secret('<redacted>')"` via this file's existing `repr` fallback.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, TypeVar

import eventkit

from ibcontroller.agent_client import (
    AgentClientError,
    AgentCommandConnection,
    AgentEventConnection,
    AgentEventMessage,
    Hello,
    Keepalive,
    Overflow,
    ProtocolError,
    Snapshot,
    WindowEvent,
    WindowInfo,
    converter,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

_CommandCall = Callable[[], Awaitable[T]]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _describe_call(call: _CommandCall[object]) -> dict[str, Any]:
    """Best-effort trace-file description of `call`. Only a
    `functools.partial` of a bound `AgentCommandConnection` method (the
    documented convention for `send_command` calls) can actually be named
    and have its arguments logged."""
    if isinstance(call, functools.partial):
        return {
            "cmd": getattr(call.func, "__name__", repr(call.func)),
            "args": list(call.args),
        }
    return {"cmd": getattr(call, "__name__", repr(call))}


def _safe_unstructure(value: object) -> object:
    """Unstructures `value` for the trace file, falling back to `repr` for
    anything `cattrs` can't handle -- trace serialization must never be the
    reason a real command fails."""
    try:
        return converter.unstructure(value)
    except Exception:
        return repr(value)


def _describe_event(msg: AgentEventMessage) -> str:
    """Returns one human-readable line describing `msg`, for the DEBUG app
    log (`_event_reader`). The raw JSON of the same message still lives in
    the trace file when tracing is enabled. Event content is window metadata
    only (class/title/window_id/seq), never field values, so it's always
    safe to log."""
    if isinstance(msg, Hello):
        return f"event: hello protocol_version={msg.protocol_version}"
    if isinstance(msg, Snapshot):
        return f"event: snapshot windows={len(msg.windows)}"
    if isinstance(msg, WindowEvent):
        win = msg.window
        title = f" title={win.title!r}" if win.title else ""
        wid = f" window_id={win.window_id}" if win.window_id else ""
        return f"event: {msg.kind} class={win.class_}{title}{wid} seq={msg.seq}"
    if isinstance(msg, Overflow):
        return f"event: overflow from_seq={msg.from_seq}"
    if isinstance(msg, Keepalive):
        return f"event: keepalive ts={msg.ts}"
    return f"event: {type(msg).__name__}"


class Subscription:
    """A buffered, ordered stream of `WindowEvent`s from `Dispatcher.subscribe()`.
    Events emitted while the subscription is open queue up in wire order, so
    none is missed between awaits. Iterate it or call `wait`."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[WindowEvent] = asyncio.Queue()

    def _put(self, event: WindowEvent) -> None:
        self._queue.put_nowait(event)

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> WindowEvent:
        return await self._queue.get()

    async def wait(
        self,
        kind: str,
        matches: Callable[[WindowEvent], bool] = lambda _event: True,
        *,
        timeout: float | None,
    ) -> WindowEvent:
        """Returns the next queued or arriving event of `kind` for which
        `matches(event)` is true; events that don't match are consumed.
        Raises `TimeoutError` after `timeout` seconds (`None` waits forever).
        An exception in `matches` propagates."""
        async with asyncio.timeout(timeout):
            while True:
                event = await self._queue.get()
                if event.kind == kind and matches(event):
                    return event


class Dispatcher:
    """Owns one command socket connection and one event socket connection
    for a single running agent instance.

    `_window_events` carries `WindowEvent` messages only -- `Hello` is a
    connection-lifecycle marker and `Keepalive` is a liveness signal, neither
    is forwarded. `Snapshot` seeds `windows`. `Overflow` (events lost on the
    agent's side) ends the event reader with `ProtocolError`. All still reach
    the trace file when tracing is enabled (see the module docstring) -- that
    stream mirrors raw traffic, unlike `_window_events`.

    `windows` is the registry of windows currently open, keyed by
    `window_id`. Only the event reader writes it, before it emits the event,
    so a consumer lagging behind the stream may see a registry already ahead
    of the event it is handling.
    """

    def __init__(
        self,
        command_conn: AgentCommandConnection,
        event_conn: AgentEventConnection,
        *,
        instance: str | None = None,
    ) -> None:
        """`command_conn`/`event_conn` are the two socket connections this
        Dispatcher owns. `instance`, if given, enables trace mode under that
        name (see the module docstring)."""
        self._command_conn = command_conn
        self._event_conn = event_conn
        self._command_queue: asyncio.Queue[
            tuple[_CommandCall[object], asyncio.Future[object], int]
        ] = asyncio.Queue()
        self._window_events: eventkit.Event = eventkit.Event()
        self._windows: dict[str, WindowInfo] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._in_flight: asyncio.Future[object] | None = None
        # Set once the worker can no longer run commands; later sends fail with it.
        self._closed_reason: str | None = None
        self._trace_cmd_logger: logging.Logger | None = None
        self._trace_event_logger: logging.Logger | None = None
        # A single gate covering both streams: configure_trace sets both to
        # the same level, so the .cmd logger's level is the reliable answer.
        # Resolved once at construction -- configure_trace must have run
        # (with this instance's name) before the Dispatcher is built.
        self._trace_enabled = False
        if instance is not None:
            prefix = f"ibcontroller.trace.{instance}"
            self._trace_cmd_logger = logging.getLogger(f"{prefix}.cmd")
            self._trace_event_logger = logging.getLogger(f"{prefix}.event")
            self._trace_enabled = self._trace_cmd_logger.isEnabledFor(logging.DEBUG)
        self._trace_seq = 0

    async def start(self) -> None:
        """Connects both sockets and starts the event-reader/command-worker
        coroutines as background tasks. Call once; not idempotent."""
        await self._command_conn.connect()
        try:
            await self._event_conn.connect()
        except BaseException:
            await self._command_conn.close()
            raise
        self._tasks = [
            asyncio.create_task(self._event_reader(), name="ibcontroller-event-reader"),
            asyncio.create_task(
                self._command_worker(), name="ibcontroller-command-worker"
            ),
        ]

    @property
    def command_conn(self) -> AgentCommandConnection:
        """The `AgentCommandConnection` this Dispatcher owns -- lets
        `actions.py`'s functions build `send_command` calls against it
        without threading a second, always-the-same reference alongside
        `dispatcher`."""
        return self._command_conn

    @property
    def windows(self) -> Mapping[str, WindowInfo]:
        """Read-only live view of the open windows by `window_id`. Mirrors
        the agent's id registry: a `Snapshot` replaces it, `window_opened`
        adds, `window_closed` removes; a window without an id is not tracked."""
        return MappingProxyType(self._windows)

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[Subscription]:
        """Connects a `Subscription` to `_window_events` immediately (no event
        emitted after entering is missed) and disconnects it on exit. Each
        subscription gets every event, in wire order."""
        subscription = Subscription()
        self._window_events.connect(subscription._put)
        try:
            yield subscription
        finally:
            self._window_events.disconnect(subscription._put)

    @property
    def tasks(self) -> tuple[asyncio.Task[None], ...]:
        """The background event-reader/command-worker tasks, for an external
        supervisor to watch for either one ending unexpectedly (e.g. the
        agent process died, or Gateway closed). `_event_reader` ends on an
        event connection loss; `_command_worker` ends when the command
        connection is broken."""
        return tuple(self._tasks)

    async def stop(self) -> None:
        """Cancels both background tasks and closes both connections; never
        raises. A task that ended with an exception is logged once. Any
        command still queued or in flight fails with `ProtocolError("dispatcher
        stopped")`, and so does any later `send_command`. Trace streams are
        `logging_setup.py`'s concern, not this layer's; the caller drains
        those separately via `logging_setup.stop_logging()`."""
        for task in self._tasks:
            task.cancel()
        outcomes = await asyncio.gather(*self._tasks, return_exceptions=True)
        for task, outcome in zip(self._tasks, outcomes, strict=True):
            if isinstance(outcome, Exception):
                logger.warning("%s ended with %r", task.get_name(), outcome)
        self._fail_pending("dispatcher stopped")
        try:
            await self._command_conn.close()
        finally:
            await self._event_conn.close()

    async def send_command(self, call: _CommandCall[T]) -> T:
        """Queues `call` and waits for the single `_command_worker` to run
        it, never overlapping another command on the same connection (the
        wire protocol has no request IDs). `call` should be a
        `functools.partial` of a bound `AgentCommandConnection` method, e.g.
        `functools.partial(dispatcher.command_conn.set_text, "u", "x")` --
        the trace file can only name the command and its arguments when it
        can introspect `call.func`/`call.args`. Raises `ProtocolError` at once
        when the dispatcher has stopped or the command connection is broken."""
        if self._closed_reason is not None:
            raise ProtocolError(self._closed_reason)
        loop = asyncio.get_running_loop()
        done: asyncio.Future[T] = loop.create_future()
        self._trace_seq += 1
        # The queue is declared over `object` so one queue can carry every command's
        # differently-typed result; a per-call `T` narrows correctly at the type
        # checker's boundary here but not through the queue's own invariant generic.
        self._command_queue.put_nowait((call, done, self._trace_seq))  # type: ignore[arg-type]
        return await done

    def _fail_pending(self, reason: str) -> None:
        """Fails the in-flight and every queued command with
        `ProtocolError(reason)` and makes later sends fail the same way.
        Keeps the first reason when called again."""
        if self._closed_reason is None:
            self._closed_reason = reason
        pending = [self._in_flight] if self._in_flight is not None else []
        self._in_flight = None
        while not self._command_queue.empty():
            pending.append(self._command_queue.get_nowait()[1])
        for future in pending:
            if not future.done():
                future.set_exception(ProtocolError(self._closed_reason))

    async def _event_reader(self) -> None:
        """Reads events from `AgentEventConnection.messages()`, keeps
        `windows` current, emits each `WindowEvent` on `_window_events`, and
        logs every message at DEBUG and to the trace file. Deliberately
        doesn't catch anything -- a malformed line or an `Overflow` (events
        lost, so `windows` can no longer be trusted) raises `ProtocolError`;
        the task ending is the correct signal for whatever supervises this
        instance to notice and act on."""
        async for msg in self._event_conn.messages():
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("%s", _describe_event(msg))
            if self._trace_enabled:
                self._trace_event(
                    {"class": type(msg).__name__, **_safe_unstructure(msg)}  # type: ignore[dict-item]
                )
            if isinstance(msg, Snapshot):
                self._windows = {
                    w.window_id: w for w in msg.windows if w.window_id is not None
                }
            elif isinstance(msg, WindowEvent):
                self._track(msg)
                self._window_events.emit(msg)
            elif isinstance(msg, Overflow):
                logger.warning(
                    "agent event queue overflow, events lost from seq=%s",
                    msg.from_seq,
                )
                raise ProtocolError("agent event queue overflow")

    def _track(self, event: WindowEvent) -> None:
        """Applies `event` to `windows`. Ids are never reused, so a repeated
        or late event is harmless; an event without an id is ignored."""
        window_id = event.window.window_id
        if window_id is None:
            return
        if event.kind == "window_opened":
            self._windows[window_id] = event.window
        else:
            self._windows.pop(window_id, None)

    async def _command_worker(self) -> None:
        """Runs one queued command at a time, resolving its future with the
        result or the raised exception. Catches broad `Exception`
        deliberately -- one bad command must not kill this task, which
        would silently stop every future command for this instance from
        ever being processed. The exception is a broken command connection:
        the task ends so the supervisor sees it (see `tasks`). However it
        ends, pending commands are failed."""
        try:
            while True:
                call, done, seq = await self._command_queue.get()
                if done.cancelled():
                    continue
                self._in_flight = done
                if self._trace_enabled:
                    self._trace_cmd(
                        {"seq": seq, "direction": "sent", **_describe_call(call)}
                    )
                try:
                    result = await call()
                except Exception as exc:
                    if self._trace_enabled:
                        code = (
                            exc.__class__.__name__
                            if isinstance(exc, AgentClientError)
                            else None
                        )
                        self._trace_cmd(
                            {
                                "seq": seq,
                                "direction": "error",
                                "error": code or repr(exc),
                            }
                        )
                    if not done.cancelled():
                        done.set_exception(exc)
                    if self._command_conn.broken:
                        self._closed_reason = "connection broken"
                        return
                else:
                    if self._trace_enabled:
                        self._trace_cmd(
                            {
                                "seq": seq,
                                "direction": "result",
                                "result": _safe_unstructure(result),
                            }
                        )
                    if not done.cancelled():
                        done.set_result(result)
                self._in_flight = None
        finally:
            self._fail_pending("dispatcher stopped")

    def _trace_cmd(self, payload: dict[str, Any]) -> None:
        self._trace(self._trace_cmd_logger, payload)

    def _trace_event(self, payload: dict[str, Any]) -> None:
        self._trace(self._trace_event_logger, payload)

    def _trace(
        self, trace_logger: logging.Logger | None, payload: dict[str, Any]
    ) -> None:
        if not self._trace_enabled or trace_logger is None:
            return
        # "_ts" (not "ts"): a real message field can be named "ts" too
        # (Keepalive's own payload has one) -- a flat merge with a matching
        # key would silently overwrite this trace-line's own timestamp.
        line = json.dumps({"_ts": _now_iso(), **payload}, default=repr)
        # A DEBUG emit into the instance-scoped trace logger; the write itself
        # happens on `logging_setup`'s listener thread, never on this event loop.
        trace_logger.debug(line)
