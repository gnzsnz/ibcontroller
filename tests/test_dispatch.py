"""Unit tests for dispatch.py -- no live Gateway needed (CLAUDE.md's "Working
method": unit tests at each layer, once that layer is validated). Runs a real
`Dispatcher` against the same fake Unix-socket server pattern agent_client.py's own
tests use, so the queue/worker/fan-out logic gets real socket-level coverage."""

from __future__ import annotations

import asyncio
import functools
import json
import logging

import pytest

from ibcontroller import dispatch
from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    ElementNotFoundError,
    PingResult,
    ProtocolError,
    WindowEvent,
    WindowInfo,
)
from ibcontroller.dispatch import Dispatcher
from ibcontroller.logging_setup import configure_trace, stop_logging
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio


async def _start(cmd_sock, event_sock) -> tuple[Dispatcher, AgentCommandConnection]:
    """Builds the same objects real calling code would: the caller owns the
    connection object it passes in, and keeps using it to build `call` thunks for
    `send_command` -- `Dispatcher` doesn't hand it back out."""
    cmd_conn = AgentCommandConnection(cmd_sock)
    dispatcher = Dispatcher(cmd_conn, AgentEventConnection(event_sock))
    await dispatcher.start()
    return dispatcher, cmd_conn


async def test_send_command_returns_result(sock_path, event_sock_path):
    event_sock = event_sock_path

    def responder(request):
        assert request == {"cmd": "ping"}
        return {"ok": True, "version": "0.0.1-dev", "uptime_s": 7}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock, []),
    ):
        dispatcher, cmd_conn = await _start(sock_path, event_sock)
        try:
            result = await dispatcher.send_command(cmd_conn.ping)
        finally:
            await dispatcher.stop()

    assert result == PingResult(version="0.0.1-dev", uptime_s=7)


async def test_send_command_propagates_typed_exception(sock_path, event_sock_path):
    event_sock = event_sock_path

    def responder(_request):
        return {"ok": False, "error": "not_found", "detail": "no such button"}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock, []),
    ):
        dispatcher, cmd_conn = await _start(sock_path, event_sock)
        try:
            with pytest.raises(ElementNotFoundError, match="no such button"):
                await dispatcher.send_command(lambda: cmd_conn.click("Log In"))
        finally:
            await dispatcher.stop()


async def test_concurrent_send_commands_stay_ordered(sock_path, event_sock_path):
    event_sock = event_sock_path
    call_order = []

    def responder(request):
        target = request["target"]
        call_order.append(target)
        return {"ok": True, "value": target}

    async def handle(reader, writer):
        while line := await reader.readline():
            request = json.loads(line)
            if request["target"] == "slow":
                await asyncio.sleep(0.05)
            response = responder(request)
            writer.write((json.dumps(response) + "\n").encode("utf-8"))
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock_path))
    async with FakeEventServer(event_sock, []):
        dispatcher, cmd_conn = await _start(sock_path, event_sock)
        try:
            results = await asyncio.gather(
                dispatcher.send_command(lambda: cmd_conn.get_text("slow")),
                dispatcher.send_command(lambda: cmd_conn.get_text("fast")),
            )
        finally:
            await dispatcher.stop()
            server.close()
            await server.wait_closed()

    assert results == ["slow", "fast"]
    assert call_order == ["slow", "fast"]


async def test_worker_survives_unexpected_exception(sock_path, event_sock_path):
    """A command whose handler raises something other than AgentClientError must
    not kill command_worker -- the next queued command still has to run."""
    event_sock = event_sock_path

    def responder(_request):
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock, []),
    ):
        dispatcher, cmd_conn = await _start(sock_path, event_sock)
        try:

            async def boom():
                raise RuntimeError("unexpected bug")

            with pytest.raises(RuntimeError, match="unexpected bug"):
                await dispatcher.send_command(boom)

            # the worker task must still be alive and processing commands
            result = await dispatcher.send_command(lambda: cmd_conn.click("IB API"))
            assert result is None
        finally:
            await dispatcher.stop()


async def test_window_events_receives_only_window_events(sock_path, event_sock_path):
    event_sock = event_sock_path
    scripted = [
        {"type": "hello", "protocol_version": 1},
        {"type": "snapshot", "windows": []},
        {
            "type": "event",
            "seq": 1,
            "kind": "window_opened",
            "window": {"class": "ibgateway.ax", "title": "IBKR Gateway"},
        },
        {"type": "keepalive", "ts": 100},
    ]

    async with FakeCommandServer(sock_path, lambda _req: {"ok": True}):
        async with FakeEventServer(event_sock, scripted):
            dispatcher, _cmd_conn = await _start(sock_path, event_sock)
            received = []
            async for msg in dispatcher._window_events.aiter():
                received.append(msg)
                break
            await dispatcher.stop()

    assert received == [
        WindowEvent(
            seq=1,
            kind="window_opened",
            window=WindowInfo(class_="ibgateway.ax", title="IBKR Gateway"),
        )
    ]


async def test_overflow_ends_the_event_reader(sock_path, event_sock_path, caplog):
    """Events were lost, so the registry can't be trusted: warn, then end the
    reader with ProtocolError (L6 reports CONNECTION_LOST)."""
    scripted = [{"type": "overflow", "from_seq": 42}]
    received = []

    async with FakeCommandServer(sock_path, lambda _req: {"ok": True}):
        async with FakeEventServer(event_sock_path, scripted):
            dispatcher, _cmd_conn = await _start(sock_path, event_sock_path)
            dispatcher._window_events.connect(received.append)
            with caplog.at_level(logging.WARNING, logger="ibcontroller.dispatch"):
                await asyncio.wait([dispatcher.tasks[0]], timeout=2)
            exc = dispatcher.tasks[0].exception()
            await dispatcher.stop()

    assert isinstance(exc, ProtocolError)
    assert "overflow" in str(exc)
    assert received == []
    assert any("overflow" in message.lower() for message in caplog.messages)


async def test_event_arrivals_logged_at_debug(sock_path, event_sock_path, caplog):
    """Every arriving event message is logged at DEBUG as one human-readable line
    (2026-09-08) -- IBC's own always-on per-window-event one-liner
    (`TwsListener.logWindow`), mapped onto this project's level-based logging at its
    single event chokepoint. Independent of trace mode (the structured NDJSON
    mirror, gated by configure_trace); `_window_events` fan-out is unaffected. At the
    INFO default these lines never appear (DEBUG is filtered at the logger level)."""
    scripted = [
        {"type": "hello", "protocol_version": 1},
        {"type": "snapshot", "windows": []},
        {
            "type": "event",
            "seq": 1,
            "kind": "window_opened",
            "window": {"class": "ibgateway.ax", "title": "IBKR Gateway"},
        },
        {"type": "keepalive", "ts": 100},
        {"type": "overflow", "from_seq": 42},
    ]

    received = []

    async with FakeCommandServer(sock_path, lambda _req: {"ok": True}):
        async with FakeEventServer(event_sock_path, scripted):
            dispatcher, _cmd_conn = await _start(sock_path, event_sock_path)
            dispatcher._window_events.connect(received.append)
            with caplog.at_level(logging.DEBUG, logger="ibcontroller.dispatch"):
                await asyncio.sleep(0.05)
            await dispatcher.stop()

    assert received == [
        WindowEvent(
            seq=1,
            kind="window_opened",
            window=WindowInfo(class_="ibgateway.ax", title="IBKR Gateway"),
        )
    ]
    assert [m for m in caplog.messages if m.startswith("event:")] == [
        "event: hello protocol_version=1",
        "event: snapshot windows=0",
        "event: window_opened class=ibgateway.ax title='IBKR Gateway' seq=1",
        "event: keepalive ts=100",
        "event: overflow from_seq=42",
    ]


async def test_stop_cancels_background_tasks(sock_path, event_sock_path):
    event_sock = event_sock_path

    async with FakeCommandServer(sock_path, lambda _req: {"ok": True}):
        async with FakeEventServer(event_sock, []):
            dispatcher, _cmd_conn = await _start(sock_path, event_sock)
            tasks = list(dispatcher._tasks)
            await dispatcher.stop()

    assert all(task.done() for task in tasks)


async def test_trace_mode_writes_both_files(sock_path, event_sock_path, tmp_path):
    """Replaces the socat watch-relay workflow: one JSONL line per command
    (sent/result) and one per raw event message, independent of the typed
    `_window_events` fan-out. Tracing itself is configured through
    logging_setup.configure_trace (2026-09-08); the Dispatcher only emits into
    the instance-scoped loggers."""
    trace_dir = tmp_path / "trace"
    configure_trace(instance="paper", enabled=True, trace_dir=trace_dir)

    def responder(request):
        assert request == {"cmd": "click", "target": "IB API"}
        return {"ok": True}

    async with FakeCommandServer(sock_path, responder):
        async with FakeEventServer(event_sock_path, [{"type": "keepalive", "ts": 1}]):
            cmd_conn = AgentCommandConnection(sock_path)
            dispatcher = Dispatcher(
                cmd_conn,
                AgentEventConnection(event_sock_path),
                instance="paper",
            )
            await dispatcher.start()
            await dispatcher.send_command(functools.partial(cmd_conn.click, "IB API"))
            await asyncio.sleep(0.05)  # let the event line land
            await dispatcher.stop()

    # drain the listener thread so every emitted line is guaranteed on disk
    stop_logging()

    cmd_lines = [
        json.loads(line)
        for line in (trace_dir / "cmd-paper.jsonl").read_text().splitlines()
    ]
    event_lines = [
        json.loads(line)
        for line in (trace_dir / "events-paper.jsonl").read_text().splitlines()
    ]

    assert [entry["direction"] for entry in cmd_lines] == ["sent", "result"]
    assert cmd_lines[0]["cmd"] == "click"
    assert cmd_lines[0]["args"] == ["IB API"]
    assert all("_ts" in entry for entry in cmd_lines)

    assert [entry["class"] for entry in event_lines] == ["Keepalive"]
    # the message's own "ts" field (1) must survive distinctly from the trace
    # line's own "_ts" wall-clock timestamp -- the exact collision being tested
    assert event_lines[0]["ts"] == 1
    assert "_ts" in event_lines[0]


async def test_trace_mode_truncates_stale_files_on_start(
    sock_path, event_sock_path, tmp_path
):
    """A trace file only makes sense for the current running session (2026-09-05,
    per the user's own steer) -- stale content from a long-dead earlier instance
    must not survive a fresh run, unlike the old append-mode behaviour that let
    cmd-paper.jsonl grow across every relaunch all session. configure_trace opens
    the files in "w" mode (2026-09-08), truncating on configure rather than on the
    Dispatcher's first write."""
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    (trace_dir / "cmd-paper.jsonl").write_text("stale content from a dead instance\n")
    (trace_dir / "events-paper.jsonl").write_text(
        "stale content from a dead instance\n"
    )
    configure_trace(instance="paper", enabled=True, trace_dir=trace_dir)

    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        cmd_conn = AgentCommandConnection(sock_path)
        dispatcher = Dispatcher(
            cmd_conn, AgentEventConnection(event_sock_path), instance="paper"
        )
        await dispatcher.start()
        await dispatcher.send_command(functools.partial(cmd_conn.click, "IB API"))
        await dispatcher.stop()

    stop_logging()
    cmd_text = (trace_dir / "cmd-paper.jsonl").read_text()
    events_text = (trace_dir / "events-paper.jsonl").read_text()
    assert "stale content" not in cmd_text
    assert "stale content" not in events_text
    assert '"cmd": "click"' in cmd_text


async def test_trace_disabled_emits_no_files(sock_path, event_sock_path, tmp_path):
    """`trace_enabled` off means no trace at all -- through the real Dispatcher,
    not just the logging-level gate. configure_trace(enabled=False) sets the
    instance loggers above CRITICAL with no handlers, so the Dispatcher's
    isEnabledFor(DEBUG) gate is false and nothing is ever emitted; no files are
    even created."""
    trace_dir = tmp_path / "trace"
    configure_trace(instance="paper", enabled=False, trace_dir=trace_dir)

    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        cmd_conn = AgentCommandConnection(sock_path)
        dispatcher = Dispatcher(
            cmd_conn, AgentEventConnection(event_sock_path), instance="paper"
        )
        await dispatcher.start()
        await dispatcher.send_command(functools.partial(cmd_conn.click, "IB API"))
        await dispatcher.stop()

    stop_logging()
    assert not (trace_dir / "cmd-paper.jsonl").exists()
    assert not (trace_dir / "events-paper.jsonl").exists()


class _SilentCommandServer:
    """Accepts a command connection and reads requests but never answers."""

    def __init__(self, path):
        self._path = str(path)
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> _SilentCommandServer:
        self._server = await asyncio.start_unix_server(self._handle, path=self._path)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @staticmethod
    async def _handle(reader, writer):
        try:
            while await reader.readline():
                pass
        finally:
            writer.close()


async def test_stop_returns_and_closes_after_event_reader_failed(
    sock_path, event_sock_path
):
    """A malformed event line ends the reader with ProtocolError; stop() must not
    re-raise it and must still close the command connection."""
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, ["not json"]),
    ):
        dispatcher, cmd_conn = await _start(sock_path, event_sock_path)
        await asyncio.wait([dispatcher.tasks[0]], timeout=2)
        assert isinstance(dispatcher.tasks[0].exception(), ProtocolError)
        await dispatcher.stop()

    with pytest.raises(ProtocolError, match="not connected"):
        await cmd_conn.ping()


async def test_stop_fails_in_flight_and_queued_callers(sock_path, event_sock_path):
    async with _SilentCommandServer(sock_path), FakeEventServer(event_sock_path, []):
        dispatcher, cmd_conn = await _start(sock_path, event_sock_path)
        in_flight = asyncio.ensure_future(dispatcher.send_command(cmd_conn.ping))
        queued = asyncio.ensure_future(dispatcher.send_command(cmd_conn.ping))
        await asyncio.sleep(0.05)
        await dispatcher.stop()
        for future in (in_flight, queued):
            with pytest.raises(ProtocolError, match="dispatcher stopped"):
                await asyncio.wait_for(future, timeout=1)
        with pytest.raises(ProtocolError, match="dispatcher stopped"):
            await dispatcher.send_command(cmd_conn.ping)


async def test_broken_command_connection_ends_worker_and_fails_pending(
    sock_path, event_sock_path
):
    cmd_conn = AgentCommandConnection(sock_path, request_timeout=0.1)
    async with _SilentCommandServer(sock_path), FakeEventServer(event_sock_path, []):
        dispatcher = Dispatcher(cmd_conn, AgentEventConnection(event_sock_path))
        await dispatcher.start()
        try:
            first = asyncio.ensure_future(dispatcher.send_command(cmd_conn.ping))
            queued = asyncio.ensure_future(dispatcher.send_command(cmd_conn.ping))
            with pytest.raises(ProtocolError, match="no response"):
                await asyncio.wait_for(first, timeout=2)
            with pytest.raises(ProtocolError, match="connection broken"):
                await asyncio.wait_for(queued, timeout=2)
            await asyncio.wait([dispatcher.tasks[1]], timeout=2)
            assert dispatcher.tasks[1].done()
            with pytest.raises(ProtocolError, match="connection broken"):
                await dispatcher.send_command(cmd_conn.ping)
        finally:
            await dispatcher.stop()


async def test_unusable_response_shape_does_not_end_worker(sock_path, event_sock_path):
    """A failure after a fully read response (here: a missing field) leaves the
    connection usable, so the worker keeps running."""

    def responder(request):
        if request["cmd"] == "ping":
            return {"ok": True}  # no version/uptime_s
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher, cmd_conn = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(ProtocolError):
                await dispatcher.send_command(cmd_conn.ping)
            assert not cmd_conn.broken
            assert not dispatcher.tasks[1].done()
            await dispatcher.send_command(lambda: cmd_conn.click("IB API"))
        finally:
            await dispatcher.stop()


async def test_start_closes_command_connection_when_event_connect_fails(
    sock_path, event_sock_path
):
    """No event server is listening: start() fails, the command side is closed."""
    async with FakeCommandServer(sock_path, lambda _req: {"ok": True}):
        cmd_conn = AgentCommandConnection(sock_path)
        dispatcher = Dispatcher(cmd_conn, AgentEventConnection(event_sock_path))
        with pytest.raises(OSError):
            await dispatcher.start()
        with pytest.raises(ProtocolError, match="not connected"):
            await cmd_conn.ping()


async def test_trace_sent_line_is_written_when_the_worker_takes_it(
    sock_path, event_sock_path, tmp_path
):
    """The first command holds the wire for 0.2s; the second's "sent" line must
    not be stamped until the worker picks it up."""
    trace_dir = tmp_path / "trace"
    configure_trace(instance="paper", enabled=True, trace_dir=trace_dir)

    async def handle(reader, writer):
        while line := await reader.readline():
            if json.loads(line)["target"] == "slow":
                await asyncio.sleep(0.2)
            writer.write(b'{"ok": true, "value": "x"}\n')
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock_path))
    async with FakeEventServer(event_sock_path, []):
        cmd_conn = AgentCommandConnection(sock_path)
        dispatcher = Dispatcher(
            cmd_conn, AgentEventConnection(event_sock_path), instance="paper"
        )
        await dispatcher.start()
        try:
            await asyncio.gather(
                dispatcher.send_command(functools.partial(cmd_conn.get_text, "slow")),
                dispatcher.send_command(functools.partial(cmd_conn.get_text, "fast")),
            )
        finally:
            await dispatcher.stop()
            server.close()
            await server.wait_closed()

    stop_logging()
    lines = [
        json.loads(line)
        for line in (trace_dir / "cmd-paper.jsonl").read_text().splitlines()
    ]
    sent = {entry["seq"]: entry for entry in lines if entry["direction"] == "sent"}
    result = {entry["seq"]: entry for entry in lines if entry["direction"] == "result"}
    assert sent[2]["_ts"] >= result[1]["_ts"]


async def test_trace_payloads_not_built_when_tracing_is_off(
    sock_path, event_sock_path, monkeypatch
):
    def boom(*_args, **_kwargs):
        raise AssertionError("trace payload built with tracing off")

    monkeypatch.setattr(dispatch, "_describe_call", boom)
    monkeypatch.setattr(dispatch, "_safe_unstructure", boom)
    monkeypatch.setattr(dispatch, "_describe_event", boom)

    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, [{"type": "keepalive", "ts": 1}]),
    ):
        dispatcher, cmd_conn = await _start(sock_path, event_sock_path)
        try:
            await dispatcher.send_command(lambda: cmd_conn.click("IB API"))
            await asyncio.sleep(0.05)  # let the keepalive event through
        finally:
            await dispatcher.stop()


def _opened(seq, window_id, title="T"):
    return {
        "type": "event",
        "seq": seq,
        "kind": "window_opened",
        "window": {"class": "c", "title": title, "window_id": window_id},
    }


def _closed(seq, window_id):
    return {
        "type": "event",
        "seq": seq,
        "kind": "window_closed",
        "window": {"class": "c", "window_id": window_id},
    }


async def _registry_after(sock_path, event_sock_path, scripted):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, scripted),
    ):
        dispatcher, _cmd_conn = await _start(sock_path, event_sock_path)
        await asyncio.wait([dispatcher.tasks[0]], timeout=2)
        windows = dict(dispatcher.windows)
        await dispatcher.stop()
    return windows


async def test_registry_is_seeded_from_the_snapshot(sock_path, event_sock_path):
    scripted = [
        {"type": "hello", "protocol_version": 1},
        {
            "type": "snapshot",
            "windows": [
                {"class": "c", "title": "A", "window_id": "w1"},
                {"class": "c", "title": "no id"},  # not tracked
            ],
        },
    ]
    windows = await _registry_after(sock_path, event_sock_path, scripted)
    assert windows == {"w1": WindowInfo(class_="c", title="A", window_id="w1")}


async def test_registry_follows_open_and_close_events(sock_path, event_sock_path):
    scripted = [
        {"type": "snapshot", "windows": []},
        _opened(1, "w1"),
        _opened(2, "w2"),
        _closed(3, "w1"),
        _closed(4, "w9"),  # unknown id: ignored
        _closed(5, None),  # no id: ignored
    ]
    windows = await _registry_after(sock_path, event_sock_path, scripted)
    assert list(windows) == ["w2"]


async def test_registry_ignores_a_repeated_event(sock_path, event_sock_path):
    scripted = [
        {"type": "snapshot", "windows": []},
        _opened(1, "w1"),
        _opened(1, "w1"),
        _closed(2, "w1"),
        _closed(2, "w1"),
    ]
    assert await _registry_after(sock_path, event_sock_path, scripted) == {}


async def test_a_later_snapshot_replaces_the_registry(sock_path, event_sock_path):
    scripted = [
        _opened(1, "w1"),  # older than the snapshot below
        {"type": "snapshot", "windows": [{"class": "c", "window_id": "w2"}]},
        _opened(2, "w3"),
    ]
    windows = await _registry_after(sock_path, event_sock_path, scripted)
    assert list(windows) == ["w2", "w3"]


async def test_registry_is_updated_before_the_event_is_emitted(
    sock_path, event_sock_path
):
    seen = []
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, [_opened(1, "w1")]),
    ):
        cmd_conn = AgentCommandConnection(sock_path)
        dispatcher = Dispatcher(cmd_conn, AgentEventConnection(event_sock_path))
        dispatcher._window_events.connect(
            lambda e: seen.append(set(dispatcher.windows))
        )
        await dispatcher.start()
        await asyncio.wait([dispatcher.tasks[0]], timeout=2)
        await dispatcher.stop()
    assert seen == [{"w1"}]


async def test_subscription_gets_every_event_in_order_and_disconnects(
    sock_path, event_sock_path
):
    scripted = [_opened(1, "w1"), _opened(2, "w2"), _closed(3, "w1")]
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, scripted),
    ):
        cmd_conn = AgentCommandConnection(sock_path)
        dispatcher = Dispatcher(cmd_conn, AgentEventConnection(event_sock_path))
        with dispatcher.subscribe() as first, dispatcher.subscribe() as second:
            await dispatcher.start()  # events are emitted from here on
            await asyncio.wait([dispatcher.tasks[0]], timeout=2)
            got_first = [await first.__anext__() for _ in range(3)]
            got_second = [await second.__anext__() for _ in range(3)]
        await dispatcher.stop()

    assert [e.seq for e in got_first] == [1, 2, 3]
    assert [e.seq for e in got_second] == [1, 2, 3]
    assert len(dispatcher._window_events) == 0


async def test_subscription_wait_skips_non_matching_and_times_out(
    sock_path, event_sock_path
):
    scripted = [_opened(1, "w1", "other"), _closed(2, "w1"), _opened(3, "w2", "want")]
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, scripted),
    ):
        cmd_conn = AgentCommandConnection(sock_path)
        dispatcher = Dispatcher(cmd_conn, AgentEventConnection(event_sock_path))
        with dispatcher.subscribe() as sub:
            await dispatcher.start()
            event = await sub.wait(
                "window_opened", lambda e: e.window.title == "want", timeout=2
            )
            with pytest.raises(TimeoutError):
                await sub.wait("window_opened", timeout=0.05)
        await dispatcher.stop()
    assert event.window.window_id == "w2"


async def test_subscription_wait_propagates_a_predicate_error(
    sock_path, event_sock_path
):
    def bad(_event):
        raise KeyError("predicate bug")

    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, [_opened(1, "w1")]),
    ):
        cmd_conn = AgentCommandConnection(sock_path)
        dispatcher = Dispatcher(cmd_conn, AgentEventConnection(event_sock_path))
        with dispatcher.subscribe() as sub:
            await dispatcher.start()
            with pytest.raises(KeyError, match="predicate bug"):
                await sub.wait("window_opened", bad, timeout=2)
        await dispatcher.stop()
