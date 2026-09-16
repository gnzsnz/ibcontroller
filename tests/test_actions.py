"""Unit tests for actions.py (L7) -- no live Gateway needed, same fake-socket-server
pattern dispatch.py's own tests use (CLAUDE.md's "Working method": unit tests after
the layer is validated -- these follow a real live-validation run against a real
paper Gateway 10.50 instance, not instead of it)."""

from __future__ import annotations

import asyncio

import eventkit
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
    wait_for_event,
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
    # nothing about wait_for_event/type_text ever holds or logs the bare string
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


async def test_dismiss_uses_default_labels_when_none_given(sock_path, event_sock_path):
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
                await dismiss(dispatcher)
        finally:
            await dispatcher.stop()

    assert attempts == ["OK", "Close", "Dismiss"]


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


async def test_wait_for_event_returns_matching_event(sock_path, event_sock_path):
    scripted = [
        {
            "type": "event",
            "seq": 1,
            "kind": "window_opened",
            "window": {"class": "some.Other", "title": "Not it"},
        },
        {
            "type": "event",
            "seq": 2,
            "kind": "window_opened",
            "window": {"class": "ibgateway.ax", "title": "IBKR Gateway"},
        },
    ]

    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, scripted),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            event = await wait_for_event(
                dispatcher,
                "window_opened",
                lambda e: e.window.title == "IBKR Gateway",
                timeout=2.0,
            )
        finally:
            await dispatcher.stop()

    assert event.seq == 2
    assert event.window.title == "IBKR Gateway"


async def test_wait_for_event_times_out_cleanly(sock_path, event_sock_path):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(TimeoutError):
                await wait_for_event(dispatcher, "window_opened", timeout=0.2)
        finally:
            await dispatcher.stop()


async def test_wait_for_event_disconnects_listener_after_returning(
    sock_path, event_sock_path
):
    """A leaked listener would keep firing on every future event for this
    Dispatcher's lifetime -- confirm cleanup actually happens, not just assume the
    `finally` block runs."""
    scripted = [
        {
            "type": "event",
            "seq": 1,
            "kind": "window_opened",
            "window": {"class": "x", "title": "t"},
        }
    ]

    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, scripted),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await wait_for_event(dispatcher, "window_opened", timeout=2.0)
            assert len(dispatcher.window_events._slots.slots) == 0
        finally:
            await dispatcher.stop()


async def test_wait_for_event_survives_synchronous_back_to_back_emits(
    sock_path, event_sock_path
):
    """The stricter regression case, emitting directly on `window_events` (no
    socket I/O in between, so truly zero event-loop turn between the two emits) --
    confirms `wait_for_event` doesn't lose the second (matching) emit to the
    reconnect race a naive `while True: await window_events` loop has (see
    `test_eventkit_await_loop_has_a_reconnect_race_between_iterations` below,
    which reproduces exactly that loss against raw `eventkit.Event`)."""
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            waiter = asyncio.ensure_future(
                wait_for_event(dispatcher, "window_opened", timeout=2.0)
            )
            await asyncio.sleep(0)  # let waiter's .filter() connect
            dispatcher.window_events.emit(
                WindowEvent(seq=1, kind="window_closed", window=WindowInfo(class_="x"))
            )
            dispatcher.window_events.emit(
                WindowEvent(seq=2, kind="window_opened", window=WindowInfo(class_="y"))
            )
            event = await asyncio.wait_for(waiter, timeout=2.0)
        finally:
            await dispatcher.stop()

    assert event.seq == 2


async def test_wait_for_event_disconnects_listener_after_timeout(
    sock_path, event_sock_path
):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(TimeoutError):
                await wait_for_event(dispatcher, "window_opened", timeout=0.2)
            assert len(dispatcher.window_events._slots.slots) == 0
        finally:
            await dispatcher.stop()


async def test_eventkit_aiter_is_lazy_not_synchronous():
    """Confirmed directly against `eventkit.Event`, independent of any
    socket/Dispatcher plumbing, not just asserted from reading its source:
    `.aiter()` connects its listener lazily, only at the first `__anext__()`
    call (it's an async generator; Python doesn't run any of its body until
    then), so an `emit()` between calling `.aiter()` and that first `await` is
    silently lost. This is why `wait_for_event` doesn't use `.aiter()`."""
    event = eventkit.Event()
    it = event.aiter()
    event.emit("should be lost")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(it.__anext__(), timeout=0.2)


async def test_eventkit_await_loop_has_a_reconnect_race_between_iterations():
    """Confirmed directly, not assumed: a bare `while True: await event` loop
    has the *same class* of race as `.aiter()`, for a different reason --
    `Event.__await__` disconnects its listener via a Future done-callback
    (deferred through `call_soon`), so a second, matching emit arriving
    back-to-back with a non-matching one (no real event-loop turn between them)
    lands in the gap between one iteration's disconnect and the next's
    reconnect, and is lost. This is why `wait_for_event` uses `.filter()`
    (which stays permanently connected) instead of looping over bare `await`."""
    event = eventkit.Event()

    async def consumer():
        while True:
            value = await event
            if value == "match":
                return value

    task = asyncio.ensure_future(consumer())
    await asyncio.sleep(0)  # let the task start and connect its first await
    event.emit("no match")
    event.emit("match")  # back-to-back, no await in between -- lost in the gap
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, timeout=0.2)
    task.cancel()


async def test_eventkit_filter_leaks_a_permanent_subscriber_unless_disconnected():
    """Confirmed directly: `.filter()`'s connection to its source is eager
    (race-free, unlike the two cases above) but permanent -- `Op._connect_from`
    connects with `keep_ref=True`, so the source keeps a *strong* reference to
    the filter object forever unless explicitly disconnected. This is why
    `wait_for_event` disconnects in a `finally`, using `Op`'s own public
    `on_source`/`on_source_error`/`on_source_done` methods."""
    source = eventkit.Event()
    filtered = source.filter(lambda v: v == "match")

    async def waiter():
        return await filtered

    task = asyncio.ensure_future(waiter())
    await asyncio.sleep(0)
    source.emit("match")
    await asyncio.wait_for(task, timeout=1.0)

    assert len(source._slots.slots) == 1  # the leak, if left alone

    source.disconnect(
        filtered.on_source, filtered.on_source_error, filtered.on_source_done
    )
    assert len(source._slots.slots) == 0


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
    settings.py -- routing it through send_command like every other action
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
