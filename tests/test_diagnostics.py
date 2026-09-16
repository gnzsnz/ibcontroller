"""Unit tests for diagnostics.py -- same fake-socket-server pattern
test_recognisers.py's own `watch_for_unprompted_windows` tests use."""

from __future__ import annotations

import asyncio

import pytest

from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    Component,
    WindowEvent,
    WindowInfo,
)
from ibcontroller.config import DiagnosticScope, DiagnosticWhen
from ibcontroller.diagnostics import format_dump, watch_for_diagnostics
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import DismissRule
from ibcontroller.recognisers import DeclarativeDismissRecognizer, RecognizerRegistry
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio

_RULE = DismissRule(name="test_rule", click="OK", match_title="Warning")


async def _start(cmd_sock, event_sock) -> Dispatcher:
    dispatcher = Dispatcher(
        AgentCommandConnection(cmd_sock), AgentEventConnection(event_sock)
    )
    await dispatcher.start()
    return dispatcher


def _event(kind: str, title: str, window_id: str = "w1") -> WindowEvent:
    return WindowEvent(
        seq=1,
        kind=kind,
        window=WindowInfo(class_="x", title=title, window_id=window_id),
    )


def test_format_dump_includes_only_present_optional_fields():
    event = _event("window_opened", "Warning")
    components = [
        Component(
            class_="javax.swing.JLabel", text="hello", enabled=True, visible=True
        ),
        Component(class_="javax.swing.JButton", name="ok_button"),
    ]
    text = format_dump(event, components)
    assert "window_opened x 'Warning' window_id=w1" in text
    assert "javax.swing.JLabel text='hello' enabled=True visible=True" in text
    assert "javax.swing.JButton name='ok_button' enabled=False visible=False" in text


async def test_watch_for_diagnostics_is_a_noop_when_when_is_never(
    sock_path, event_sock_path
):
    async with (
        FakeCommandServer(sock_path, lambda request: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry([])
            calls = []
            await asyncio.wait_for(
                watch_for_diagnostics(
                    registry,
                    dispatcher,
                    DiagnosticScope.ALL,
                    DiagnosticWhen.NEVER,
                    sink=lambda event, text: calls.append((event, text)),
                ),
                timeout=1.0,
            )
        finally:
            await dispatcher.stop()

    assert calls == []


async def test_watch_for_diagnostics_dumps_a_known_window_when_scope_is_known(
    sock_path, event_sock_path
):
    def responder(request):
        if request.get("cmd") == "dump":
            return {
                "ok": True,
                "components": [
                    {"class": "javax.swing.JLabel", "accessible_name": "Warning"}
                ],
            }
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry([DeclarativeDismissRecognizer(_RULE)])
            calls = []
            watcher = asyncio.ensure_future(
                watch_for_diagnostics(
                    registry,
                    dispatcher,
                    DiagnosticScope.KNOWN,
                    DiagnosticWhen.OPEN,
                    sink=lambda event, text: calls.append((event, text)),
                )
            )
            await asyncio.sleep(0.05)
            dispatcher.window_events.emit(_event("window_opened", "Warning"))
            await asyncio.sleep(0.2)
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
        finally:
            await dispatcher.stop()

    assert len(calls) == 1
    assert "javax.swing.JLabel" in calls[0][1]


async def test_watch_for_diagnostics_skips_an_unknown_window_when_scope_is_known(
    sock_path, event_sock_path
):
    def responder(request):
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry([DeclarativeDismissRecognizer(_RULE)])
            calls = []
            watcher = asyncio.ensure_future(
                watch_for_diagnostics(
                    registry,
                    dispatcher,
                    DiagnosticScope.KNOWN,
                    DiagnosticWhen.OPEN,
                    sink=lambda event, text: calls.append((event, text)),
                )
            )
            await asyncio.sleep(0.05)
            dispatcher.window_events.emit(_event("window_opened", "Unrelated"))
            await asyncio.sleep(0.2)
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
        finally:
            await dispatcher.stop()

    assert calls == []


async def test_watch_for_diagnostics_falls_back_on_a_vanished_window(
    sock_path, event_sock_path
):
    """`window_closed` can't be dumped -- the window is already gone by the
    time this task reacts to the push over the socket."""

    def responder(request):
        if request.get("cmd") == "dump":
            return {"ok": False, "error": "window_gone", "detail": "gone"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            registry = RecognizerRegistry([])
            calls = []
            watcher = asyncio.ensure_future(
                watch_for_diagnostics(
                    registry,
                    dispatcher,
                    DiagnosticScope.ALL,
                    DiagnosticWhen.OPENCLOSE,
                    sink=lambda event, text: calls.append((event, text)),
                )
            )
            await asyncio.sleep(0.05)
            dispatcher.window_events.emit(_event("window_closed", "Warning"))
            await asyncio.sleep(0.2)
            watcher.cancel()
            with pytest.raises(asyncio.CancelledError):
                await watcher
        finally:
            await dispatcher.stop()

    assert len(calls) == 1
    assert "window already gone" in calls[0][1]
