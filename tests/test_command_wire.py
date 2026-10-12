"""Golden test of the request each agent command puts on the wire: exact keys,
omitted optional fields, `Secret` unwrapped. Pins the wire contract with the Java
agent independently of how the client builds the request."""

from __future__ import annotations

import pytest
from typed_settings.types import Secret

from ibcontroller.agent_client import AgentCommandConnection
from tests.fakes import FakeCommandServer

pytestmark = pytest.mark.asyncio

_OK = {"ok": True}

# (id, call on a connection, expected wire request, canned response)
_CASES = [
    (
        "ping",
        lambda c: c.ping(),
        {"cmd": "ping"},
        {"ok": True, "version": "1", "uptime_s": 2},
    ),
    (
        "dump",
        lambda c: c.dump(),
        {"cmd": "dump"},
        {"ok": True, "components": []},
    ),
    (
        "dump-scoped",
        lambda c: c.dump("Title", "w1"),
        {"cmd": "dump", "window": "Title", "window_id": "w1"},
        {"ok": True, "components": []},
    ),
    (
        "get_text",
        lambda c: c.get_text("Field"),
        {"cmd": "get_text", "target": "Field"},
        {"ok": True, "value": "x"},
    ),
    (
        "get_text-scoped",
        lambda c: c.get_text("Field", "w1"),
        {"cmd": "get_text", "target": "Field", "window_id": "w1"},
        {"ok": True, "value": "x"},
    ),
    (
        "set_text",
        lambda c: c.set_text("Field", "abc"),
        {"cmd": "set_text", "target": "Field", "value": "abc"},
        _OK,
    ),
    (
        "set_text-secret",
        lambda c: c.set_text("Field", Secret("s3cret"), "w1"),
        {"cmd": "set_text", "target": "Field", "value": "s3cret", "window_id": "w1"},
        _OK,
    ),
    (
        "set_checkbox",
        lambda c: c.set_checkbox("Box", True),
        {"cmd": "set_checkbox", "target": "Box", "checked": True},
        _OK,
    ),
    (
        "set_checkbox-scoped",
        lambda c: c.set_checkbox("Box", False, "w1"),
        {"cmd": "set_checkbox", "target": "Box", "checked": False, "window_id": "w1"},
        _OK,
    ),
    ("click", lambda c: c.click("OK"), {"cmd": "click", "target": "OK"}, _OK),
    (
        "click-scoped",
        lambda c: c.click("OK", "w1"),
        {"cmd": "click", "target": "OK", "window_id": "w1"},
        _OK,
    ),
    (
        "set_text_near_label",
        lambda c: c.set_text_near_label("Socket port", 0, "7497"),
        {
            "cmd": "set_text_near_label",
            "label": "Socket port",
            "index": 0,
            "value": "7497",
        },
        _OK,
    ),
    (
        "set_text_near_label-secret",
        lambda c: c.set_text_near_label("Label", 1, Secret("s3cret"), "w1"),
        {
            "cmd": "set_text_near_label",
            "label": "Label",
            "index": 1,
            "value": "s3cret",
            "window_id": "w1",
        },
        _OK,
    ),
    (
        "navigate_menu",
        lambda c: c.navigate_menu("File/Close"),
        {"cmd": "navigate_menu", "path": "File/Close"},
        {"ok": True, "clicked": True},
    ),
    (
        "navigate_menu-scoped",
        lambda c: c.navigate_menu("File/Close", "w1"),
        {"cmd": "navigate_menu", "path": "File/Close", "window_id": "w1"},
        {"ok": True, "clicked": True},
    ),
    (
        "expand_tree",
        lambda c: c.expand_tree("API/Settings"),
        {"cmd": "expand_tree", "path": "API/Settings"},
        _OK,
    ),
    (
        "expand_tree-scoped",
        lambda c: c.expand_tree("API/Settings", "w1"),
        {"cmd": "expand_tree", "path": "API/Settings", "window_id": "w1"},
        _OK,
    ),
    (
        "menu_item_exists",
        lambda c: c.menu_item_exists("Help/About", "w1"),
        {"cmd": "menu_item_exists", "path": "Help/About", "window_id": "w1"},
        {"ok": True, "exists": True, "enabled": False},
    ),
]


@pytest.mark.parametrize(
    ("call", "expected", "response"),
    [pytest.param(*case[1:], id=case[0]) for case in _CASES],
)
async def test_command_wire_request(sock_path, call, expected, response):
    seen = []

    def responder(request):
        seen.append(request)
        return response

    async with FakeCommandServer(sock_path, responder):
        async with AgentCommandConnection(sock_path) as conn:
            await call(conn)

    assert seen == [expected]
