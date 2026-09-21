"""Integration test: config.py's `log_dir` (env -> app_dirs.py) actually reaches a
real `Dispatcher` and real trace files land where it resolved to, when
`trace_enabled` is on. The three modules involved
(config.py, app_dirs.py, dispatch.py) each have their own unit tests already, but
nothing connected them end to end until this -- a real gap, caught by the user asking
"where are we setting app dir for our tests?" and finding the answer was "nowhere, not
together." No live Gateway needed: runs against the same fake Unix-socket server
pattern the other test files use."""

from __future__ import annotations

import functools
import json

import pytest

from ibcontroller.agent_client import AgentCommandConnection, AgentEventConnection
from ibcontroller.app_dirs import resolve_app_dirs
from ibcontroller.config import load_config
from ibcontroller.dispatch import Dispatcher
from ibcontroller.logging_setup import configure_trace, stop_logging
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio

_BASE_ENV = {
    "IBC_USERID": "papuser",
    "IBC_PASSWORD": "pappass",  # nosec B105
    "IBC_TRADING_MODE": "paper",
    "IBC_TWS_VERSION": "10.50",
}


async def test_config_log_dir_reaches_a_real_dispatcher(
    monkeypatch, sock_path, event_sock_path, tmp_path
):
    app_dir = tmp_path / "app"
    env = {
        **_BASE_ENV,
        "IBC_APP_DIR": str(app_dir),
        "IBC_INSTANCE": "paper",
        "IBC_TRACE_ENABLED": "yes",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config_dir, log_dir = resolve_app_dirs(env)
    config = load_config(
        config_dir=config_dir, log_dir=log_dir, dotenv_path=tmp_path / "nonexistent.env"
    )
    assert config.log_dir == str(app_dir / "log")

    def responder(request):
        assert request == {"cmd": "click", "target": "IB API"}
        return {"ok": True}

    async with FakeCommandServer(sock_path, responder):
        async with FakeEventServer(event_sock_path, []):
            # exactly the wiring launcher.launch_instance uses (2026-09-08):
            # configure_trace owns the files, the Dispatcher emits into the
            # instance-scoped loggers
            configure_trace(
                instance=config.instance,
                enabled=config.trace_enabled,
                trace_dir=config.log_dir,
            )
            cmd_conn = AgentCommandConnection(sock_path)
            dispatcher = Dispatcher(
                cmd_conn,
                AgentEventConnection(event_sock_path),
                instance=config.instance,
            )
            await dispatcher.start()
            try:
                await dispatcher.send_command(
                    functools.partial(cmd_conn.click, "IB API")
                )
            finally:
                await dispatcher.stop()

    stop_logging()  # drain the listener thread before reading the file
    cmd_log = app_dir / "log" / "cmd-paper.jsonl"
    assert cmd_log.exists()
    lines = [json.loads(line) for line in cmd_log.read_text().splitlines()]
    assert [entry["direction"] for entry in lines] == ["sent", "result"]
    assert lines[0]["cmd"] == "click"


async def test_config_password_never_reaches_trace_file_as_plaintext(
    monkeypatch, sock_path, event_sock_path, tmp_path
):
    """The real incident this project hit (2026-09-05): a plaintext password
    landed in cmd-paper.jsonl via set_text's own value argument. Runs the exact real
    path -- a real Config's Secret-wrapped password, through a real Dispatcher,
    into a real set_text call -- and asserts the actual bytes on disk."""
    app_dir = tmp_path / "app"
    env = {
        **_BASE_ENV,
        "IBC_APP_DIR": str(app_dir),
        "IBC_INSTANCE": "paper",
        "IBC_TRACE_ENABLED": "yes",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config_dir, log_dir = resolve_app_dirs(env)
    config = load_config(
        config_dir=config_dir, log_dir=log_dir, dotenv_path=tmp_path / "nonexistent.env"
    )

    async with FakeCommandServer(sock_path, lambda _req: {"ok": True}):
        async with FakeEventServer(event_sock_path, []):
            configure_trace(
                instance=config.instance,
                enabled=config.trace_enabled,
                trace_dir=config.log_dir,
            )
            cmd_conn = AgentCommandConnection(sock_path)
            dispatcher = Dispatcher(
                cmd_conn,
                AgentEventConnection(event_sock_path),
                instance=config.instance,
            )
            await dispatcher.start()
            try:
                await dispatcher.send_command(
                    functools.partial(cmd_conn.set_text, "Password", config.password)
                )
            finally:
                await dispatcher.stop()

    stop_logging()  # drain the listener thread before reading the file
    cmd_log = app_dir / "log" / "cmd-paper.jsonl"
    raw_text = cmd_log.read_text()
    assert "pappass" not in raw_text  # the real credential from _BASE_ENV
    lines = [json.loads(line) for line in raw_text.splitlines()]
    sent = next(entry for entry in lines if entry["direction"] == "sent")
    assert sent["args"] == ["Password", "Secret('*******')"]


# Deliberately no "defaults to the real platformdirs path" variant here that actually
# runs a Dispatcher against it -- that would write real files into
# ~/Library/Logs/ibcontroller (or the Linux/Windows equivalent) on every test run,
# polluting real user state. config.py's own test_log_dir_defaults_via_app_dirs
# already covers "log_dir resolves to the real platformdirs path" at the
# pure-resolution level, with no file I/O; this file only integration-tests the path
# that's actually safe to run for real, i.e. with IBC_APP_DIR pointed at a
# tmp_path.
