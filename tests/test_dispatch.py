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

from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    ElementNotFoundError,
    PingResult,
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
            async for msg in dispatcher.window_events.aiter():
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


async def test_overflow_is_logged_not_forwarded(sock_path, event_sock_path, caplog):
    event_sock = event_sock_path
    scripted = [{"type": "overflow", "from_seq": 42}]

    received = []

    async with FakeCommandServer(sock_path, lambda _req: {"ok": True}):
        async with FakeEventServer(event_sock, scripted):
            dispatcher, _cmd_conn = await _start(sock_path, event_sock)
            dispatcher.window_events.connect(received.append)
            with caplog.at_level(logging.WARNING, logger="ibcontroller.dispatch"):
                await asyncio.sleep(0.05)
            await dispatcher.stop()

    assert received == []
    assert any("overflow" in message.lower() for message in caplog.messages)


async def test_event_arrivals_logged_at_debug(sock_path, event_sock_path, caplog):
    """Every arriving event message is logged at DEBUG as one human-readable line
    (2026-09-08) -- IBC's own always-on per-window-event one-liner
    (`TwsListener.logWindow`), mapped onto this project's level-based logging at its
    single event chokepoint. Independent of trace mode (the structured NDJSON
    mirror, gated by configure_trace); `window_events` fan-out is unaffected. At the
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
            dispatcher.window_events.connect(received.append)
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
    `window_events` fan-out. Tracing itself is configured through
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
