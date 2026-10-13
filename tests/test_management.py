"""Unit tests for management.py -- `clean_shutdown` against a fake agent
socket and a fake process, no Gateway."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from tempfile import gettempdir

import pytest

from ibcontroller import launcher
from ibcontroller.agent_client import AgentCommandConnection, AgentEventConnection
from ibcontroller.config import Program
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import load_labels
from ibcontroller.launcher import LaunchedInstance
from ibcontroller.management import clean_shutdown
from tests.fakes import FakeCommandServer, FakeEventServer, FakeProcess

pytestmark = pytest.mark.asyncio

LABELS = load_labels()


async def _start_dispatcher(cmd_sock, event_sock) -> Dispatcher:
    dispatcher = Dispatcher(
        AgentCommandConnection(cmd_sock), AgentEventConnection(event_sock)
    )
    await dispatcher.start()
    return dispatcher


def _launched(process, dispatcher, sock_path, event_sock_path) -> LaunchedInstance:
    return LaunchedInstance(
        process=process,
        dispatcher=dispatcher,
        command_socket_path=str(sock_path),
        event_socket_path=str(event_sock_path),
        settings_dir=f"{gettempdir()}/settings",
        stdout_drain_task=asyncio.ensure_future(asyncio.sleep(0)),
    )


async def test_clean_shutdown_terminates_directly_when_process_already_exited(
    sock_path, event_sock_path
):
    """Regression test for two real, live-caught bugs, both hit back to back
    on 2026-09-06 testing the exact same scenario (the user closing Gateway
    manually before `clean_shutdown` ever ran): first, a raw
    `ConnectionResetError` from attempting `navigate_menu` over a dead
    connection (the old `except AgentClientError` never caught it); then,
    once that was fixed to check `returncode` first, a `ProcessLookupError`
    from this same branch still calling `terminate()` on a process that no
    longer has an OS PID to signal. Both are asserted here: no command sent,
    and `terminate()` never called (the fake raises `ProcessLookupError` if
    it is, matching real `asyncio.subprocess.Process` behaviour)."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start_dispatcher(sock_path, event_sock_path)
        process = FakeProcess(returncode=0)
        launched = _launched(process, dispatcher, sock_path, event_sock_path)
        await clean_shutdown(
            launched, program=Program.GATEWAY, logged_in=True, labels=LABELS.shutdown
        )

    assert not process.terminated  # never called -- would have raised otherwise
    assert calls == []  # never attempted navigate_menu against the dead process


async def test_clean_shutdown_terminates_when_never_logged_in_and_still_running(
    sock_path, event_sock_path
):
    """The other branch that calls `terminate()` -- login never completed, but
    the process is genuinely still alive, so `terminate()` must actually be
    called here (unlike the already-exited case above)."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start_dispatcher(sock_path, event_sock_path)
        process = FakeProcess(returncode=None)
        launched = _launched(process, dispatcher, sock_path, event_sock_path)
        await clean_shutdown(
            launched, program=Program.GATEWAY, logged_in=False, labels=LABELS.shutdown
        )

    assert process.terminated
    assert calls == []  # never logged in -- no graceful menu attempt at all


async def test_clean_shutdown_uses_graceful_navigate_menu_when_process_alive(
    sock_path, event_sock_path
):
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start_dispatcher(sock_path, event_sock_path)
        process = FakeProcess(returncode=None)
        launched = _launched(process, dispatcher, sock_path, event_sock_path)
        # A short timeout: this fake process never exits on its own after the
        # click (no real Gateway behind it), so the fallback terminate() path
        # fires -- exercised deliberately, not the point of this test.
        await clean_shutdown(
            launched,
            program=Program.GATEWAY,
            logged_in=True,
            labels=LABELS.shutdown,
            timeout=0.1,
        )

    assert {"cmd": "navigate_menu", "path": "File/Close"} in calls


async def test_clean_shutdown_terminates_when_the_agent_dies_mid_navigate_menu(
    sock_path, event_sock_path, monkeypatch
):
    """The process dying between the `returncode` check and the command being
    sent surfaces from the connection as `ProtocolError`, not a raw
    `ConnectionResetError`, so the fallback `terminate()` still runs."""
    async with (
        FakeCommandServer(sock_path, lambda _request: {"ok": True, "clicked": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start_dispatcher(sock_path, event_sock_path)

        async def drain():
            raise ConnectionResetError("reset by peer")

        monkeypatch.setattr(dispatcher._command_conn._writer, "drain", drain)
        process = FakeProcess(returncode=None)
        launched = _launched(process, dispatcher, sock_path, event_sock_path)
        await clean_shutdown(
            launched,
            program=Program.GATEWAY,
            logged_in=True,
            labels=LABELS.shutdown,
            timeout=0.1,
        )

    assert process.terminated


async def test_clean_shutdown_terminates_when_the_agent_never_answers(
    sock_path, event_sock_path
):
    """A `navigate_menu` the agent never answers is bounded by the request
    timeout, so the fallback `terminate()` is reached instead of hanging."""

    async def handle(reader, writer):
        await reader.readline()  # accepts, never answers
        await asyncio.sleep(5)
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(sock_path))
    try:
        async with FakeEventServer(event_sock_path, []):
            dispatcher = Dispatcher(
                AgentCommandConnection(sock_path, request_timeout=0.1),
                AgentEventConnection(event_sock_path),
            )
            await dispatcher.start()
            process = FakeProcess(returncode=None)
            launched = _launched(process, dispatcher, sock_path, event_sock_path)
            await asyncio.wait_for(
                clean_shutdown(
                    launched,
                    program=Program.GATEWAY,
                    logged_in=True,
                    labels=LABELS.shutdown,
                    timeout=0.1,
                ),
                timeout=3,
            )
    finally:
        server.close()
        await server.wait_closed()

    assert process.terminated


async def test_clean_shutdown_uses_tws_menu_path_for_tws(sock_path, event_sock_path):
    """Real, live-caught bug (2026-09-09, an audit prompted by two other
    hardcoded-label bugs the same day): the graceful-shutdown menu path used
    to be a hardcoded `"File/Close"`/`"File/Exit"` ternary directly in
    `clean_shutdown`, not sourced from `labels.json` like every other
    user-facing string this project matches or clicks against. Confirms
    `program=Program.TWS` selects `labels.shutdown.tws_menu_path`, not Gateway's."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start_dispatcher(sock_path, event_sock_path)
        process = FakeProcess(returncode=None)
        launched = _launched(process, dispatcher, sock_path, event_sock_path)
        await clean_shutdown(
            launched,
            program=Program.TWS,
            logged_in=True,
            labels=LABELS.shutdown,
            timeout=0.1,
        )

    assert {"cmd": "navigate_menu", "path": "File/Exit"} in calls
    assert {"cmd": "navigate_menu", "path": "File/Close"} not in calls


async def test_clean_shutdown_scopes_navigate_menu_to_main_window(
    sock_path, event_sock_path
):
    """The unscoped menu-bar search can resolve a frame other than TWS's main
    window, whose menu has no `File/Exit` (#76) -- `main_window_id` must reach
    the agent."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start_dispatcher(sock_path, event_sock_path)
        process = FakeProcess(returncode=None)
        launched = _launched(process, dispatcher, sock_path, event_sock_path)
        await clean_shutdown(
            launched,
            program=Program.TWS,
            logged_in=True,
            labels=LABELS.shutdown,
            main_window_id="w2",
            timeout=0.1,
        )

    assert {"cmd": "navigate_menu", "path": "File/Exit", "window_id": "w2"} in calls


def test_launcher_no_longer_imports_ui_layers():
    """L1 stays below L5-1: `clean_shutdown` moved to Management so `launcher`
    no longer needs `actions` or `labels` (#79)."""
    tree = ast.parse(Path(launcher.__file__).read_text())
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert "ibcontroller.actions" not in imported
    assert "ibcontroller.labels" not in imported
