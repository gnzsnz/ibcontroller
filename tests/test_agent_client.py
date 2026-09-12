"""Unit tests for agent_client.py -- no live Gateway needed (CLAUDE.md's "Working
method": unit tests at each layer, once that layer is validated). Command/event
connection tests run against a small fake Unix-socket server started in-process, so the
locking, error-mapping, and parsing logic gets real socket-level coverage without
depending on the Java agent at all -- the live agent is validated separately, by hand,
through the existing socat watch relay."""

from __future__ import annotations

import asyncio
import json

import pytest

from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentError,
    AgentEventConnection,
    BadRequestError,
    Component,
    CredentialRefusedError,
    ElementNotFoundError,
    Hello,
    Keepalive,
    Overflow,
    PingResult,
    ProtocolError,
    Snapshot,
    UnknownCommandError,
    WindowEvent,
    WindowGoneError,
    WindowInfo,
    derive_event_socket_path,
    parse_event_message,
)
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio


# --- derive_event_socket_path -----------------------------------------------------


def test_derive_event_socket_path_conforming(tmp_path):
    cmd = tmp_path / "ibcontroller-agent-dev-cmd.sock"
    assert (
        derive_event_socket_path(cmd) == tmp_path / "ibcontroller-agent-dev-events.sock"
    )


def test_derive_event_socket_path_non_conforming(tmp_path):
    cmd = tmp_path / "custom.sock"
    assert derive_event_socket_path(cmd) == tmp_path / "custom.sock.events"


# --- parse_event_message -----------------------------------------------------------


def test_parse_event_message_hello():
    msg = parse_event_message({"type": "hello", "protocol_version": 1})
    assert msg == Hello(protocol_version=1)


def test_parse_event_message_snapshot():
    msg = parse_event_message(
        {
            "type": "snapshot",
            "windows": [{"class": "ibgateway.ax", "title": "IBKR Gateway"}],
        }
    )
    assert isinstance(msg, Snapshot)
    assert msg.windows[0].class_ == "ibgateway.ax"
    assert msg.windows[0].title == "IBKR Gateway"


def test_parse_event_message_window_event():
    msg = parse_event_message(
        {
            "type": "event",
            "seq": 3,
            "kind": "window_opened",
            "window": {
                "class": "twslaunch.jauthentication.bh",
                "title": "Second Factor",
            },
        }
    )
    assert isinstance(msg, WindowEvent)
    assert msg.seq == 3
    assert msg.kind == "window_opened"
    assert msg.window.class_ == "twslaunch.jauthentication.bh"
    assert msg.window.window_id is None


def test_parse_event_message_window_event_carries_window_id():
    """`window_id` (2026-09-06, `EventBridge.java`'s own registry) -- confirms
    it parses through when present, matching the real agent's wire shape."""
    msg = parse_event_message(
        {
            "type": "event",
            "seq": 4,
            "kind": "window_opened",
            "window": {
                "class": "some.class",
                "title": "IBKR Gateway",
                "window_id": "w7",
            },
        }
    )
    assert isinstance(msg, WindowEvent)
    assert msg.window.window_id == "w7"


def test_parse_event_message_overflow():
    assert parse_event_message({"type": "overflow", "from_seq": 7}) == Overflow(
        from_seq=7
    )


def test_parse_event_message_keepalive():
    assert parse_event_message({"type": "keepalive", "ts": 123}) == Keepalive(ts=123)


def test_parse_event_message_unknown_type_raises():
    with pytest.raises(ProtocolError, match="unknown event message type"):
        parse_event_message({"type": "something_new"})


# --- AgentCommandConnection ---------------------------------------------------------


async def test_ping_success(sock_path):
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "ping"}
        return {"ok": True, "version": "0.0.1-dev", "uptime_s": 42}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            result = await conn.ping()
    assert result == PingResult(version="0.0.1-dev", uptime_s=42)


async def test_dump_success(sock_path):
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "dump"}
        return {
            "ok": True,
            "components": [
                {
                    "class": "twslaunch.trader.common.document.q",
                    "credential_field": True,
                    "enabled": True,
                    "showing": True,
                    "visible": True,
                    "text": "<redacted password len=0>",
                }
            ],
        }

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            components = await conn.dump()
    assert components == [
        Component(
            class_="twslaunch.trader.common.document.q",
            credential_field=True,
            enabled=True,
            showing=True,
            visible=True,
            text="<redacted password len=0>",
        )
    ]


async def test_dump_parses_text_truncation_fields(sock_path):
    """Regression test for a real, live-caught crash (2026-09-09): Gateway's own
    File > Gateway Logs > View Logs window renders its whole log file into one
    JTextComponent -- dump()'s untruncated text field produced a response line past
    asyncio's default 64 KiB stream limit, crashing the read loop with a raw
    ValueError. ComponentLookup now truncates and reports it; this confirms the wire
    shape actually round-trips through cattrs into typed fields, not just that Java
    sends them."""
    sock = sock_path

    def responder(request):
        return {
            "ok": True,
            "components": [
                {
                    "class": "javax.swing.JTextArea",
                    "credential_field": False,
                    "enabled": True,
                    "showing": True,
                    "visible": True,
                    "text": "x" * 2000,
                    "text_truncated": True,
                    "text_length": 5_000_000,
                }
            ],
        }

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            components = await conn.dump()
    assert components == [
        Component(
            class_="javax.swing.JTextArea",
            credential_field=False,
            enabled=True,
            showing=True,
            visible=True,
            text="x" * 2000,
            text_truncated=True,
            text_length=5_000_000,
        )
    ]


async def test_dump_survives_a_response_past_the_default_stream_limit(sock_path):
    """The actual crash, reproduced directly: asyncio's stock stream-reader limit is
    64 KiB; before the fix (`AgentCommandConnection.connect`'s `limit=
    _STREAM_READ_LIMIT`), a response line this large raised a raw `ValueError` out of
    `readline()`. A real component's text is now also capped Java-side
    (`ComponentLookup.MAX_TEXT_LENGTH`), but this test exercises the Python-side
    buffer bump directly and in isolation, with many components rather than one huge
    field -- the case that cap alone doesn't cover."""
    sock = sock_path

    def responder(_request):
        # ~50 components, each with a near-cap-sized text field -- comfortably past
        # 64 KiB in aggregate, comfortably under the new 8 MiB limit.
        components = [
            {
                "class": "javax.swing.JLabel",
                "credential_field": False,
                "enabled": True,
                "showing": True,
                "visible": True,
                "text": "x" * 2000,
            }
            for _ in range(50)
        ]
        return {"ok": True, "components": components}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            components = await conn.dump()
    assert len(components) == 50


async def test_dump_with_window_filter(sock_path):
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "dump", "window": "login"}
        return {"ok": True, "components": []}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            await conn.dump(window="login")


async def test_dump_with_window_id_ignores_title_filter_on_the_wire(sock_path):
    """Regression test for a real, live-caught bug (2026-09-06): a title like
    "IBKR Gateway" can match several simultaneously-open windows at once (the
    persisting login frame, the real main window, a fresh popup); on a live
    account, dumping all of them into one response was large enough to
    corrupt the shared command connection. `window_id` (when given) is what
    scopes the dump to exactly one window."""
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "dump", "window": "IBKR Gateway", "window_id": "w7"}
        return {"ok": True, "components": []}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            await conn.dump(window="IBKR Gateway", window_id="w7")


async def test_dump_window_gone_raises_window_gone_error(sock_path):
    sock = sock_path

    def responder(request):
        return {
            "ok": False,
            "error": "window_gone",
            "detail": "window w7 is no longer open",
        }

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            with pytest.raises(WindowGoneError, match="no longer open"):
                await conn.dump(window_id="w7")


async def test_get_text_success(sock_path):
    sock = sock_path

    async with FakeCommandServer(sock, lambda _req: {"ok": True, "value": "jsmith"}):
        async with AgentCommandConnection(sock) as conn:
            assert await conn.get_text("Username") == "jsmith"


async def test_set_text_success(sock_path):
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "set_text", "target": "Username", "value": "jsmith"}
        return {"ok": True}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            await conn.set_text("Username", "jsmith")


async def test_set_checkbox_success(sock_path):
    sock = sock_path

    def responder(request):
        assert request == {
            "cmd": "set_checkbox",
            "target": "Paper Trading",
            "checked": True,
        }
        return {"ok": True}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            await conn.set_checkbox("Paper Trading", True)


async def test_click_success(sock_path):
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "click", "target": "IB API"}
        return {"ok": True}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            await conn.click("IB API")


async def test_click_omits_window_id_when_not_given(sock_path):
    """Backward compatibility: a caller that doesn't pass `window_id` gets the
    original wire shape exactly, no `null` field added."""
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "click", "target": "IB API"}
        return {"ok": True}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            await conn.click("IB API")


async def test_click_includes_window_id_when_given(sock_path):
    """Regression test for a real, live-caught bug (2026-09-06): scoping the
    agent's own search to one window is what this field exists for."""
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "click", "target": "OK", "window_id": "w7"}
        return {"ok": True}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            await conn.click("OK", window_id="w7")


async def test_click_window_gone_raises_window_gone_error(sock_path):
    sock = sock_path

    def responder(request):
        return {
            "ok": False,
            "error": "window_gone",
            "detail": "window w7 is no longer open",
        }

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            with pytest.raises(WindowGoneError, match="no longer open"):
                await conn.click("OK", window_id="w7")


async def test_navigate_menu_success(sock_path):
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "navigate_menu", "path": "File/Close"}
        return {"ok": True, "clicked": True}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            assert await conn.navigate_menu("File/Close") is True


async def test_navigate_menu_reports_disabled_without_clicking(sock_path):
    """`clicked: False` means the resolved menu item exists but Gateway hasn't
    enabled it yet (ported from IBC's own `Utils.invokeMenuItem`'s `isEnabled()`
    check) -- a real, retriable condition `actions.navigate_menu` retries on,
    not an error."""
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "navigate_menu", "path": "Configure/Settings"}
        return {"ok": True, "clicked": False}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            assert await conn.navigate_menu("Configure/Settings") is False


async def test_navigate_menu_defaults_clicked_true_when_field_omitted(sock_path):
    """Backward/forward compatibility: an older or fake server that predates the
    `clicked` field still gets treated as a successful click, not a KeyError."""
    sock = sock_path

    def responder(request):
        return {"ok": True}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            assert await conn.navigate_menu("File/Close") is True


async def test_expand_tree_success(sock_path):
    sock = sock_path

    def responder(request):
        assert request == {"cmd": "expand_tree", "path": "API/Settings"}
        return {"ok": True}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            await conn.expand_tree("API/Settings")


async def test_set_text_near_label_success(sock_path):
    sock = sock_path

    def responder(request):
        assert request == {
            "cmd": "set_text_near_label",
            "label": "Set Auto Restart Time (HH:MM)",
            "index": 0,
            "value": "09:30",
        }
        return {"ok": True}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            await conn.set_text_near_label("Set Auto Restart Time (HH:MM)", 0, "09:30")


@pytest.mark.parametrize(
    ("code", "exc_type"),
    [
        ("not_found", ElementNotFoundError),
        ("refused_credential_field", CredentialRefusedError),
        ("bad_request", BadRequestError),
        ("unknown_command", UnknownCommandError),
        ("agent_error", AgentError),
        (
            "some_future_code",
            AgentError,
        ),  # forward-compatible: unknown code -> AgentError
    ],
)
async def test_error_codes_map_to_typed_exceptions(sock_path, code, exc_type):
    sock = sock_path

    def responder(_req):
        return {"ok": False, "error": code, "detail": "boom"}

    async with FakeCommandServer(sock, responder):
        async with AgentCommandConnection(sock) as conn:
            with pytest.raises(exc_type, match="boom"):
                await conn.get_text("Password")


async def test_malformed_response_raises_protocol_error(sock_path):
    sock = sock_path

    async def handle(reader, writer):
        await reader.readline()
        writer.write(b"not json\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock))
    try:
        async with AgentCommandConnection(sock) as conn:
            with pytest.raises(ProtocolError, match="malformed response"):
                await conn.ping()
    finally:
        server.close()
        await server.wait_closed()


async def test_connection_closed_raises_protocol_error(sock_path):
    sock = sock_path

    async def handle(reader, writer):
        await reader.readline()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock))
    try:
        async with AgentCommandConnection(sock) as conn:
            with pytest.raises(ProtocolError, match="closed"):
                await conn.ping()
    finally:
        server.close()
        await server.wait_closed()


async def test_request_before_connect_raises_protocol_error():
    conn = AgentCommandConnection("/nonexistent/does-not-matter.sock")
    with pytest.raises(ProtocolError, match="not connected"):
        await conn.ping()


async def test_concurrent_requests_do_not_interleave(sock_path):
    """The wire protocol has no request IDs -- two concurrent callers on one
    connection must be serialized by `_request`'s lock, or a fast second response
    could be read by the first caller's `readline()`."""
    sock = sock_path

    call_order = []

    async def responder(request):
        target = request["target"]
        call_order.append(target)
        if target == "slow":
            await asyncio.sleep(0.05)
        return {"ok": True, "value": target}

    async def handle(reader, writer):
        while line := await reader.readline():
            response = await responder(json.loads(line))
            writer.write((json.dumps(response) + "\n").encode("utf-8"))
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock))
    try:
        async with AgentCommandConnection(sock) as conn:
            results = await asyncio.gather(
                conn.get_text("slow"),
                conn.get_text("fast"),
            )
    finally:
        server.close()
        await server.wait_closed()

    assert results == ["slow", "fast"]
    assert call_order == ["slow", "fast"]


# --- AgentEventConnection -----------------------------------------------------------


async def test_messages_yields_parsed_events_in_order(sock_path):
    sock = sock_path

    scripted = [
        {"type": "hello", "protocol_version": 1},
        {"type": "snapshot", "windows": []},
        {
            "type": "event",
            "seq": 1,
            "kind": "window_opened",
            "window": {"class": "ibgateway.ax", "title": "IBKR Gateway"},
        },
        {"type": "overflow", "from_seq": 1},
        {"type": "keepalive", "ts": 100},
    ]
    async with FakeEventServer(sock, scripted):
        async with AgentEventConnection(sock) as conn:
            received = [msg async for msg in conn.messages()]

    assert received == [
        Hello(protocol_version=1),
        Snapshot(windows=[]),
        WindowEvent(
            seq=1,
            kind="window_opened",
            window=WindowInfo(class_="ibgateway.ax", title="IBKR Gateway"),
        ),
        Overflow(from_seq=1),
        Keepalive(ts=100),
    ]


async def test_messages_stops_cleanly_on_eof(sock_path):
    sock = sock_path

    async with FakeEventServer(sock, [{"type": "hello", "protocol_version": 1}]):
        async with AgentEventConnection(sock) as conn:
            received = [msg async for msg in conn.messages()]
    assert received == [Hello(protocol_version=1)]


async def test_messages_raises_on_malformed_line(sock_path):
    sock = sock_path

    async with FakeEventServer(sock, ["not json"]):
        async with AgentEventConnection(sock) as conn:
            with pytest.raises(ProtocolError, match="malformed event line"):
                async for _ in conn.messages():
                    pass


async def test_event_messages_before_connect_raises_protocol_error():
    conn = AgentEventConnection("/nonexistent/does-not-matter.sock")
    with pytest.raises(ProtocolError, match="not connected"):
        async for _ in conn.messages():
            pass
