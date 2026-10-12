"""Unit tests for actions.py (L7) -- no live Gateway needed, same fake-socket-server
pattern dispatch.py's own tests use (CLAUDE.md's "Working method": unit tests after
the layer is validated -- these follow a real live-validation run against a real
paper Gateway 10.50 instance, not instead of it)."""

from __future__ import annotations

import asyncio

import pytest
from typed_settings.types import Secret

from ibcontroller.actions import (
    ACTIONS,
    click,
    dismiss,
    expand_tree,
    navigate_menu,
    read_text,
    toggle,
    type_text,
    type_text_near_label,
    wait_for_window,
)
from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    ElementNotFoundError,
    WindowEvent,
    WindowInfo,
)
from ibcontroller.dispatch import Dispatcher
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio


async def _start(cmd_sock, event_sock) -> Dispatcher:
    dispatcher = Dispatcher(
        AgentCommandConnection(cmd_sock), AgentEventConnection(event_sock)
    )
    await dispatcher.start()
    return dispatcher


async def test_type_text_sends_set_text(sock_path, event_sock_path):
    def responder(request):
        assert request == {"cmd": "set_text", "target": "Username", "value": "alice"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await type_text(dispatcher, "Username", "alice")
        finally:
            await dispatcher.stop()


async def test_type_text_accepts_secret_and_never_sends_plaintext_repr(
    sock_path, event_sock_path
):
    seen = {}

    def responder(request):
        seen["value"] = request["value"]
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await type_text(dispatcher, "Password", Secret("hunter2"))
        finally:
            await dispatcher.stop()

    # the wire value is unwrapped correctly (agent_client.py's own job) -- but
    # nothing about type_text ever holds or logs the bare string
    # outside that one call.
    assert seen["value"] == "hunter2"


async def test_click_tries_candidates_in_order_until_one_is_found(
    sock_path, event_sock_path
):
    attempts = []

    def responder(request):
        attempts.append(request["target"])
        if request["target"] == "Log In":
            return {"ok": False, "error": "not_found", "detail": "no such button"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await click(dispatcher, ["Log In", "Paper Log In"])
        finally:
            await dispatcher.stop()

    assert attempts == ["Log In", "Paper Log In"]


async def test_click_single_label_still_works(sock_path, event_sock_path):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await click(dispatcher, "IB API")
        finally:
            await dispatcher.stop()


async def test_click_propagates_non_not_found_error_without_trying_more_candidates(
    sock_path, event_sock_path
):
    attempts = []

    def responder(request):
        attempts.append(request["target"])
        return {"ok": False, "error": "refused_credential_field", "detail": "nope"}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(Exception, match="nope"):
                await click(dispatcher, ["Password", "Username"])
        finally:
            await dispatcher.stop()

    assert attempts == ["Password"]  # never tried the second candidate


async def test_click_raises_after_exhausting_all_candidates(sock_path, event_sock_path):
    async with (
        FakeCommandServer(
            sock_path,
            lambda _req: {"ok": False, "error": "not_found", "detail": "gone"},
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(ElementNotFoundError):
                await click(dispatcher, ["a", "b", "c"])
        finally:
            await dispatcher.stop()


async def test_toggle_sends_set_checkbox(sock_path, event_sock_path):
    def responder(request):
        assert request == {
            "cmd": "set_checkbox",
            "target": "IB API",
            "checked": True,
        }
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await toggle(dispatcher, "IB API", True)
        finally:
            await dispatcher.stop()


async def test_dismiss_clicks_the_labels_it_is_given(sock_path, event_sock_path):
    attempts = []

    def responder(request):
        attempts.append(request["target"])
        return {"ok": False, "error": "not_found", "detail": "no"}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(ElementNotFoundError):
                await dismiss(dispatcher, ["OK", "Close"])
        finally:
            await dispatcher.stop()

    assert attempts == ["OK", "Close"]


async def test_read_text_returns_value(sock_path, event_sock_path):
    async with (
        FakeCommandServer(
            sock_path, lambda _req: {"ok": True, "value": "existing text"}
        ),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            result = await read_text(dispatcher, "Username")
        finally:
            await dispatcher.stop()

    assert result == "existing text"


async def test_read_text_forwards_window_id(sock_path, event_sock_path):
    seen = []

    def responder(request):
        seen.append(request)
        return {"ok": True, "value": "x"}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await read_text(dispatcher, "Username", window_id="w7")
        finally:
            await dispatcher.stop()

    assert seen == [{"cmd": "get_text", "target": "Username", "window_id": "w7"}]


def test_actions_table_contains_the_real_vocabulary_only():
    """key_combo is deliberately absent -- the Java agent doesn't implement that
    command yet (Protocol.java's dispatch recognises ping/dump/get_text/set_text/
    set_checkbox/click/navigate_menu/expand_tree/menu_item_exists); wrapping a
    command that doesn't exist would be building ahead of an unvalidated layer.
    expand_tree joined the real vocabulary 2026-09-05 once
    WriteOps.selectConfigSection was built and live-validated (settings.py's own
    docstring has the full story). menu_item_exists joined 2026-09-09 -- built
    2026-09-06 as a diagnostic-only agent_client primitive, only now exposed at
    this layer once login.py's TWS main-window detection became its first real
    caller (ported from IBC's own MainWindowFrameHandler.recogniseWindow). dump
    joined 2026-09-11, promoted from a direct, unqueued cmd_conn.dump() call in
    settings.py -- routing it through Dispatcher.send like every other action
    closed a real serialization/tracing gap."""
    assert set(ACTIONS) == {
        "type_text",
        "click",
        "toggle",
        "dismiss",
        "read_text",
        "navigate_menu",
        "expand_tree",
        "type_text_near_label",
        "menu_item_exists",
        "dump",
    }
    assert "wait_for_event" not in ACTIONS


async def test_navigate_menu_sends_path(sock_path, event_sock_path):
    def responder(request):
        assert request == {"cmd": "navigate_menu", "path": "File/Close"}
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await navigate_menu(dispatcher, "File/Close")
        finally:
            await dispatcher.stop()


async def test_navigate_menu_forwards_window_id(sock_path, event_sock_path):
    """#40: `window_id`, when given, scopes `navigate_menu` to that exact
    window instead of the agent's global "first displayable frame" search --
    `open_settings_dialog` passes `LoginManager.main_window_id` for this."""

    def responder(request):
        assert request == {
            "cmd": "navigate_menu",
            "path": "File/Global Configuration...",
            "window_id": "w2",
        }
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await navigate_menu(
                dispatcher, "File/Global Configuration...", window_id="w2"
            )
        finally:
            await dispatcher.stop()


async def test_navigate_menu_retries_while_disabled_then_succeeds(
    sock_path, event_sock_path
):
    """Ports IBC's own `Utils.invokeMenuItem` shape (check `isEnabled()`,
    retry, don't click-once-and-hope). Confirms `navigate_menu` keeps retrying
    on `clicked: False` -- e.g. Gateway still has a startup dialog blocking the
    menu path -- and returns as soon as a later attempt reports `clicked: True`,
    without raising."""
    attempts = []

    def responder(request):
        attempts.append(request)
        return {"ok": True, "clicked": len(attempts) >= 3}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await asyncio.wait_for(
                navigate_menu(dispatcher, "Configure/Settings", timeout=5.0),
                timeout=5.0,
            )
        finally:
            await dispatcher.stop()

    assert len(attempts) == 3
    assert all(
        r == {"cmd": "navigate_menu", "path": "Configure/Settings"} for r in attempts
    )


async def test_navigate_menu_times_out_if_never_enabled(sock_path, event_sock_path):
    def responder(request):
        return {"ok": True, "clicked": False}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(TimeoutError):
                await navigate_menu(dispatcher, "Configure/Settings", timeout=0.3)
        finally:
            await dispatcher.stop()


async def test_navigate_menu_not_found_fails_after_not_found_timeout(
    sock_path, event_sock_path
):
    attempts = []

    def responder(request):
        attempts.append(request)
        return {"ok": False, "error": "not_found", "detail": "no such menu"}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            loop = asyncio.get_running_loop()
            start = loop.time()
            with pytest.raises(ElementNotFoundError):
                await navigate_menu(
                    dispatcher, "File/Nope", timeout=30.0, not_found_timeout=0.6
                )
            elapsed = loop.time() - start
        finally:
            await dispatcher.stop()

    assert 0.5 <= elapsed < 5.0  # not the 30s `timeout`
    assert len(attempts) >= 2


async def test_navigate_menu_not_found_then_resolves_succeeds(
    sock_path, event_sock_path
):
    attempts = []

    def responder(request):
        attempts.append(request)
        if len(attempts) < 3:
            return {"ok": False, "error": "not_found", "detail": "not yet"}
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await navigate_menu(dispatcher, "File/Close", not_found_timeout=5.0)
        finally:
            await dispatcher.stop()

    assert len(attempts) == 3


async def test_navigate_menu_disabled_is_not_cut_by_not_found_timeout(
    sock_path, event_sock_path
):
    attempts = []

    def responder(request):
        attempts.append(request)
        return {"ok": True, "clicked": len(attempts) >= 4}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await navigate_menu(
                dispatcher, "File/Close", timeout=5.0, not_found_timeout=0.1
            )
        finally:
            await dispatcher.stop()

    assert len(attempts) == 4


async def test_navigate_menu_not_found_clock_restarts_after_path_resolves(
    sock_path, event_sock_path
):
    """not_found, disabled (path resolved), then not_found again: the second
    not_found gets a fresh window instead of failing at once."""
    replies = [
        {"ok": False, "error": "not_found", "detail": "x"},
        {"ok": True, "clicked": False},
        {"ok": True, "clicked": False},
        {"ok": True, "clicked": False},
        {"ok": False, "error": "not_found", "detail": "x"},
        {"ok": True, "clicked": True},
    ]
    attempts = []

    def responder(request):
        attempts.append(request)
        return replies[len(attempts) - 1]

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await navigate_menu(
                dispatcher, "File/Close", timeout=10.0, not_found_timeout=0.6
            )
        finally:
            await dispatcher.stop()

    assert len(attempts) == 6


async def test_expand_tree_sends_path(sock_path, event_sock_path):
    def responder(request):
        assert request == {"cmd": "expand_tree", "path": "API/Settings"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await expand_tree(dispatcher, "API/Settings")
        finally:
            await dispatcher.stop()


async def test_type_text_near_label_sends_label_index_and_value(
    sock_path, event_sock_path
):
    def responder(request):
        assert request == {
            "cmd": "set_text_near_label",
            "label": "Set Auto Restart Time (HH:MM)",
            "index": 0,
            "value": "09:30",
        }
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await type_text_near_label(
                dispatcher, "Set Auto Restart Time (HH:MM)", 0, "09:30"
            )
        finally:
            await dispatcher.stop()


def _idle_dispatcher() -> Dispatcher:
    """Unstarted Dispatcher: registry and `_window_events` driven by hand."""
    return Dispatcher(AgentCommandConnection("unused"), AgentEventConnection("unused"))


def _win_event(kind: str, window_id: str, title: str) -> WindowEvent:
    return WindowEvent(
        seq=1,
        kind=kind,
        window=WindowInfo(class_="c", title=title, window_id=window_id),
    )


async def test_wait_for_window_open_already_open_returns_at_once():
    dispatcher = _idle_dispatcher()
    dispatcher._windows["w1"] = WindowInfo(class_="c", title="Login", window_id="w1")
    win = await wait_for_window(dispatcher, lambda w: w.title == "Login", timeout=0.2)
    assert win is not None
    assert win.window_id == "w1"


async def test_wait_for_window_open_waits_for_event():
    dispatcher = _idle_dispatcher()
    task = asyncio.ensure_future(
        wait_for_window(dispatcher, lambda w: w.title == "Login", timeout=2.0)
    )
    await asyncio.sleep(0)
    dispatcher._window_events.emit(_win_event("window_opened", "w2", "Other"))
    dispatcher._window_events.emit(_win_event("window_opened", "w3", "Login"))
    win = await task
    assert win is not None
    assert win.window_id == "w3"
    assert len(dispatcher._window_events) == 0


async def test_wait_for_window_closed_already_closed_returns_at_once():
    dispatcher = _idle_dispatcher()
    dispatcher._windows["w1"] = WindowInfo(class_="c", title="Other", window_id="w1")
    result = await wait_for_window(
        dispatcher, lambda w: w.title == "Splash", closed=True, timeout=0.2
    )
    assert result is None


async def test_wait_for_window_closed_waits_for_close_event():
    dispatcher = _idle_dispatcher()
    dispatcher._windows["w1"] = WindowInfo(class_="c", title="Splash", window_id="w1")
    task = asyncio.ensure_future(
        wait_for_window(
            dispatcher, lambda w: w.title == "Splash", closed=True, timeout=2.0
        )
    )
    await asyncio.sleep(0)
    assert not task.done()
    dispatcher._window_events.emit(_win_event("window_closed", "w1", "Splash"))
    assert await task is None
    assert len(dispatcher._window_events) == 0


async def test_wait_for_window_times_out_and_disconnects():
    dispatcher = _idle_dispatcher()
    with pytest.raises(TimeoutError):
        await wait_for_window(dispatcher, lambda w: True, timeout=0.1)
    assert len(dispatcher._window_events) == 0


async def test_wait_for_window_predicate_error_propagates():
    dispatcher = _idle_dispatcher()

    def boom(_w: WindowInfo) -> bool:
        raise ValueError("bad predicate")

    task = asyncio.ensure_future(wait_for_window(dispatcher, boom, timeout=2.0))
    await asyncio.sleep(0)
    dispatcher._window_events.emit(_win_event("window_opened", "w1", "x"))
    with pytest.raises(ValueError, match="bad predicate"):
        await task


async def test_click_with_no_candidates_raises_value_error():
    with pytest.raises(ValueError, match="at least one"):
        await click(_idle_dispatcher(), [])
