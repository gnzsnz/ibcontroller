"""Shared test fakes -- a small in-process Unix-socket server standing in for the
real Java agent, so client-side logic (agent_client.py, dispatch.py) gets real
socket-level coverage without a live Gateway. Extracted here once a second test file
needed the identical pattern test_agent_client.py already established. The
`sock_path` fixture these servers bind to lives in conftest.py, not here -- a
fixture shared across test modules is auto-discovered from there, no import needed
(and no F811 false-positive from ruff treating an imported fixture's use as a
parameter-name "redefinition")."""

from __future__ import annotations

import asyncio
import json


class FakeCommandServer:
    """Reads one JSON line, hands it to `responder(request) -> dict`, writes the
    result back -- repeat until the client disconnects. Mirrors Protocol.java's own
    read-a-line/write-a-line loop closely enough to exercise real client logic."""

    def __init__(self, path, responder):
        self._path = str(path)
        self._responder = responder
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> FakeCommandServer:
        self._server = await asyncio.start_unix_server(self._handle, path=self._path)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while line := await reader.readline():
                request = json.loads(line)
                response = self._responder(request)
                writer.write((json.dumps(response) + "\n").encode("utf-8"))
                await writer.drain()
        finally:
            writer.close()


class FakeEventServer:
    """Writes each of `messages` as its own line as soon as a client connects, then
    closes -- enough to exercise the event-socket parsing/fan-out path."""

    def __init__(self, path, messages):
        self._path = str(path)
        self._messages = messages
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> FakeEventServer:
        self._server = await asyncio.start_unix_server(self._handle, path=self._path)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        for message in self._messages:
            line = message if isinstance(message, str) else json.dumps(message)
            writer.write((line + "\n").encode("utf-8"))
            await writer.drain()
        writer.close()
