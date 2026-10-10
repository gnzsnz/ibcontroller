"""Integration tests: the real agent jar driven through the real `agent_client` over
a Unix socket. The agent runs without an entry point, so no Gateway/TWS is needed.
Skipped when no JDK or built jar is available (`make build`)."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest
import pytest_asyncio

from ibcontroller.agent_client import (
    AgentCommandConnection,
    BadRequestError,
    WindowGoneError,
)

pytestmark = pytest.mark.asyncio

_JAR = (
    Path(__file__).resolve().parent.parent / "ibcontroller" / "ibcontroller-agent.jar"
)
_JAVA = (
    str(Path(os.environ["JAVA_HOME"]) / "bin" / "java")
    if os.environ.get("JAVA_HOME")
    else shutil.which("java")
)

requires_agent = pytest.mark.skipif(
    _JAVA is None or not _JAR.exists(), reason="needs a JDK and `make build`"
)


@pytest_asyncio.fixture
async def agent(sock_path):
    assert _JAVA is not None  # guaranteed by requires_agent
    proc = await asyncio.create_subprocess_exec(
        _JAVA,
        "-Djava.awt.headless=true",
        "-cp",
        str(_JAR),
        "ibcontroller.agent.AgentMain",
        str(sock_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(15):
            while not sock_path.exists():
                await asyncio.sleep(0.05)
        yield sock_path
        assert proc.returncode is None, "agent exited during the test"
    finally:
        if proc.returncode is None:
            proc.terminate()
        await proc.wait()
        Path(f"{sock_path}.events").unlink(missing_ok=True)  # agent's fallback name


@requires_agent
async def test_deep_nesting_is_bad_request_and_agent_keeps_serving(agent):
    nested: list = []
    for _ in range(500):
        nested = [nested]
    async with AgentCommandConnection(agent) as conn:
        response = await conn._request({"cmd": "ping", "x": nested})
        with pytest.raises(BadRequestError):
            conn._raise_for_error(response)
        assert (await conn.ping()).version  # same connection still serves
    async with AgentCommandConnection(agent) as conn:
        assert (await conn.ping()).version  # and so does a new one


@requires_agent
async def test_malformed_line_keeps_connection_and_agent_alive(agent):
    reader, writer = await asyncio.open_unix_connection(str(agent))
    writer.write(b"not json\n")
    await writer.drain()
    assert b"bad_request" in await reader.readline()
    writer.write(b'{"cmd":"ping"}\n')
    await writer.drain()
    assert b'"ok":true' in await reader.readline()
    writer.close()
    await writer.wait_closed()


@requires_agent
async def test_agent_survives_repeated_connect_and_abrupt_close(agent):
    for _ in range(5):
        _, writer = await asyncio.open_unix_connection(str(agent))
        writer.write(b'{"cmd":"ping"')  # no newline, then drop
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    async with AgentCommandConnection(agent) as conn:
        assert (await conn.ping()).version


@requires_agent
async def test_get_text_with_unknown_window_id_is_window_gone(agent):
    async with AgentCommandConnection(agent) as conn:
        with pytest.raises(WindowGoneError):
            await conn.get_text("Username", window_id="nope")
        assert (await conn.ping()).version
