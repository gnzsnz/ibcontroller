"""Wire client for the Java agent's two Unix sockets: a command socket
(request/response) and an event socket (server push). This module owns the
transport and the typed (`cattrs`/`attrs`) message schemas only -- reading
one JSON line, building one, and turning the result into a typed object or
a typed exception.

Command scheduling, event fan-out, and tracing are `dispatch.py`'s job, one
layer up: it calls the typed methods below and consumes
`AgentEventConnection.messages()`, without needing to know anything about
JSON shapes or error codes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import attrs
import cattrs
from cattrs.gen import make_dict_structure_fn, override
from typed_settings.types import Secret

converter = cattrs.Converter()


class AgentClientError(Exception):
    """Base class for every error this module raises."""


class ElementNotFoundError(AgentClientError):
    """The target component could not be found (agent error code `not_found`)."""


class CredentialRefusedError(AgentClientError):
    """The agent refused to return a credential field's value (error code
    `refused_credential_field`), e.g. `get_text` on a password field."""


class BadRequestError(AgentClientError):
    """The agent reported a malformed request (error code `bad_request`)."""


class UnknownCommandError(AgentClientError):
    """The agent does not recognise the command (error code `unknown_command`)."""


class AgentError(AgentClientError):
    """A generic agent-side error (error code `agent_error`), or any error code
    this client doesn't recognise."""


class WindowGoneError(AgentClientError):
    """The window referenced by `window_id` no longer exists -- it closed
    between the caller learning about it and the command reaching the agent.
    Distinct from `ElementNotFoundError`, which means the target label itself
    doesn't exist within a (still open) window."""


class ProtocolError(AgentClientError):
    """A transport-level failure: a failed or closed connection, a malformed
    line, or a response that doesn't have the expected shape -- not an error
    the agent itself reported."""


# Large enough for a dump() response from a window with many components; a
# dump() response is JSON, not raw binary, so this bounds well above what any
# realistic component tree would produce.
_STREAM_READ_LIMIT = 8 * 1024 * 1024

# Wire round-trip bounds, from the longest command durations seen in trace files
# (5.3 s for a dump, under 2.5 s for anything else). A dump is slower under EDT load.
_REQUEST_TIMEOUT = 30.0
_DUMP_TIMEOUT = 60.0

_EXCERPT_LIMIT = 300  # raw wire data embedded in an error message

_ERROR_CODES: dict[str, type[AgentClientError]] = {
    "not_found": ElementNotFoundError,
    "refused_credential_field": CredentialRefusedError,
    "bad_request": BadRequestError,
    "unknown_command": UnknownCommandError,
    "agent_error": AgentError,
    "window_gone": WindowGoneError,
}


@attrs.define(frozen=True)
class PingResult:
    version: str
    uptime_s: int


@attrs.define(frozen=True)
class Component:
    """One entry from `dump`'s `components` list. Every field but `class_` is
    optional -- the agent only includes a field when it has something to
    report. `text_truncated`/`text_length` indicate `text` was cut to a
    maximum length, rather than silently returning a truncated value."""

    class_: str
    name: str | None = None
    accessible_name: str | None = None
    credential_field: bool = False
    enabled: bool = False
    showing: bool = False
    visible: bool = False
    selected: bool | None = None
    parent_class: str | None = None
    text: str | None = None
    text_truncated: bool = False
    text_length: int | None = None


@attrs.define(frozen=True)
class WindowInfo:
    """Identifies one window. `window_id` is the agent's opaque, per-window ID
    -- pass it back on any command that takes `window_id` to scope that
    command to this exact window instead of an agent-side search across every
    open window."""

    class_: str
    title: str | None = None
    window_id: str | None = None


converter.register_structure_hook(
    Component,
    make_dict_structure_fn(Component, converter, class_=override(rename="class")),
)
converter.register_structure_hook(
    WindowInfo,
    make_dict_structure_fn(WindowInfo, converter, class_=override(rename="class")),
)


@attrs.define(frozen=True)
class Hello:
    protocol_version: int


@attrs.define(frozen=True)
class Snapshot:
    windows: list[WindowInfo]


@attrs.define(frozen=True)
class WindowEvent:
    """One event from the event socket. `kind` is `window_opened` or
    `window_closed`."""

    seq: int
    kind: str
    window: WindowInfo


@attrs.define(frozen=True)
class Overflow:
    from_seq: int


@attrs.define(frozen=True)
class Keepalive:
    ts: int


AgentEventMessage = Hello | Snapshot | WindowEvent | Overflow | Keepalive

_EVENT_MESSAGE_TYPES: dict[str, type[AgentEventMessage]] = {
    "hello": Hello,
    "snapshot": Snapshot,
    "event": WindowEvent,
    "overflow": Overflow,
    "keepalive": Keepalive,
}


def parse_event_message(raw: Mapping[str, Any]) -> AgentEventMessage:
    """Structures a raw event message dict into its typed `AgentEventMessage`
    subclass, chosen by its `type` field. Raises `ProtocolError` if `type` is
    missing or unrecognised, or the message doesn't have that type's shape."""
    msg_type = raw.get("type")
    cls = _EVENT_MESSAGE_TYPES.get(msg_type) if isinstance(msg_type, str) else None
    if cls is None:
        raise ProtocolError(f"unknown event message type: {msg_type!r}")
    return _structure(raw, cls, "event")


def _structure(data: object, cls: Any, what: str) -> Any:
    """`converter.structure(data, cls)`, with a shape mismatch raised as
    `ProtocolError` instead of a `cattrs` error."""
    try:
        return converter.structure(data, cls)
    except (cattrs.BaseValidationError, KeyError, TypeError) as exc:
        raise ProtocolError(f"unexpected {what} shape: {_excerpt(data)}") from exc


def _excerpt(value: object) -> str:
    """`repr(value)`, cut to `_EXCERPT_LIMIT` characters plus the full length."""
    text = repr(value)
    if len(text) <= _EXCERPT_LIMIT:
        return text
    return f"{text[:_EXCERPT_LIMIT]}... ({len(text)} chars)"


def _field(response: Mapping[str, Any], key: str) -> Any:
    """`response[key]`, with a missing key raised as `ProtocolError`."""
    try:
        return response[key]
    except KeyError:
        raise ProtocolError(f"unexpected response shape: no {key!r}") from None


def derive_event_socket_path(command_socket_path: str | Path) -> Path:
    """Derives the event socket path from `command_socket_path`:
    `<prefix>-cmd.sock` -> `<prefix>-events.sock`, or `<path>.events` if the
    command socket path doesn't follow that naming convention."""
    path = Path(command_socket_path)
    suffix = "-cmd.sock"
    name = path.name
    if name.endswith(suffix):
        new_name = name[: -len(suffix)] + "-events.sock"
        return path.with_name(new_name)
    return path.with_name(name + ".events")


class AgentCommandConnection:
    """One persistent connection to the agent's command socket: send one JSON
    line, read one JSON line back, per command. The wire protocol has no
    request IDs, so requests are serialized with a lock -- one write/read
    pair completes before the next one starts. A request that fails between
    the write and a parsed response leaves the stream at an unknown position,
    so the connection is marked broken and every later request raises
    `ProtocolError` (reconnecting needs a new `connect()`).

    `request_timeout` bounds each request's wire round trip (not the wait for
    the lock); `dump_timeout` replaces it for `dump`. A request that times out
    breaks the connection."""

    def __init__(
        self,
        path: str | Path,
        *,
        request_timeout: float = _REQUEST_TIMEOUT,
        dump_timeout: float = _DUMP_TIMEOUT,
    ) -> None:
        self._path = str(path)
        self._request_timeout = request_timeout
        self._dump_timeout = dump_timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._broken = False

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(
            path=self._path, limit=_STREAM_READ_LIMIT
        )
        self._broken = False

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def __aenter__(self) -> AgentCommandConnection:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def broken(self) -> bool:
        """True once a request failure left the stream at an unknown position;
        every later request raises `ProtocolError` until `connect()`."""
        return self._broken

    def _mark_broken(self) -> None:
        self._broken = True
        if self._writer is not None:
            self._writer.close()  # not awaited (may run while cancelled), close() does

    async def _request(
        self, payload: Mapping[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        """Sends `payload` as one JSON line and returns the parsed response
        dict. Raises `ProtocolError` for transport-level failures (not
        connected, broken or closed connection, no response within `timeout`
        (default `request_timeout`), malformed line, a response missing the
        `ok` field). Any failure between the write and a parsed response,
        cancellation and timeout included, marks the connection broken."""
        timeout = self._request_timeout if timeout is None else timeout
        line = json.dumps(payload) + "\n"
        async with self._lock:
            reader, writer = self._reader, self._writer
            if self._broken:
                raise ProtocolError("connection broken")
            if reader is None or writer is None:
                raise ProtocolError("not connected")
            try:
                async with asyncio.timeout(timeout):
                    writer.write(line.encode("utf-8"))
                    await writer.drain()
                    raw = await reader.readline()
            except asyncio.CancelledError:
                self._mark_broken()
                raise
            except TimeoutError:  # before OSError: it is a subclass
                self._mark_broken()
                raise ProtocolError(f"no response within {timeout}s") from None
            except (OSError, ValueError) as exc:  # ValueError: line over the limit
                self._mark_broken()
                raise ProtocolError(f"command connection failed: {exc}") from exc
            try:
                if not raw:
                    raise ProtocolError("agent closed the command connection")
                try:
                    response = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ProtocolError(
                        f"malformed response line: {_excerpt(raw)}"
                    ) from exc
                if not isinstance(response, dict) or "ok" not in response:
                    raise ProtocolError(
                        f"unexpected response shape: {_excerpt(response)}"
                    )
            except ProtocolError:
                self._mark_broken()
                raise
        return response

    @staticmethod
    def _raise_for_error(response: Mapping[str, Any]) -> None:
        """Raises the typed `AgentClientError` subclass matching `response`'s
        `error` code (`AgentError` if the code isn't recognised)."""
        code = response.get("error", "agent_error")
        detail = str(response.get("detail", ""))
        raise _ERROR_CODES.get(code, AgentError)(detail)

    async def ping(self) -> PingResult:
        """Pings the agent. Returns its version and uptime."""
        response = await self._request({"cmd": "ping"})
        if not response["ok"]:
            self._raise_for_error(response)
        return _structure(response, PingResult, "response")

    async def dump(
        self, window: str | None = None, window_id: str | None = None
    ) -> list[Component]:
        """Returns the current component tree, optionally scoped by `window`
        (a title substring) or `window_id` (an exact, opaque ID, taking
        precedence when both are given). Raises `WindowGoneError` if
        `window_id` no longer resolves to an open window."""
        payload: dict[str, Any] = {"cmd": "dump"}
        if window is not None:
            payload["window"] = window
        if window_id is not None:
            payload["window_id"] = window_id
        response = await self._request(payload, self._dump_timeout)
        if not response["ok"]:
            self._raise_for_error(response)
        return _structure(_field(response, "components"), list[Component], "response")

    async def get_text(self, target: str, window_id: str | None = None) -> str:
        """Returns the text of the component identified by `target`.
        `window_id`, if given, scopes the search to one window. Raises
        `CredentialRefusedError` if `target` is a password field, and
        `WindowGoneError` if `window_id` no longer resolves to an open window."""
        payload: dict[str, Any] = {"cmd": "get_text", "target": target}
        if window_id is not None:
            payload["window_id"] = window_id
        response = await self._request(payload)
        if not response["ok"]:
            self._raise_for_error(response)
        return str(_field(response, "value"))

    async def set_text(
        self, target: str, value: str | Secret, window_id: str | None = None
    ) -> None:
        """Sets the text of the component identified by `target`. `value` may
        be a `Secret` (credentials) or a plain string -- a `Secret` is
        unwrapped here, immediately before building the request, the one
        point where a credential becomes a bare string. `window_id`, if
        given, scopes the search to one window. Raises `WindowGoneError` if
        `window_id` no longer resolves to an open window."""
        real_value = value.get_secret_value() if isinstance(value, Secret) else value
        payload: dict[str, Any] = {
            "cmd": "set_text",
            "target": target,
            "value": real_value,
        }
        if window_id is not None:
            payload["window_id"] = window_id
        response = await self._request(payload)
        if not response["ok"]:
            self._raise_for_error(response)

    async def set_checkbox(
        self, target: str, checked: bool, window_id: str | None = None
    ) -> None:
        """Sets the checked state of the component identified by `target`.
        `window_id`, if given, scopes the search to one window."""
        payload: dict[str, Any] = {
            "cmd": "set_checkbox",
            "target": target,
            "checked": checked,
        }
        if window_id is not None:
            payload["window_id"] = window_id
        response = await self._request(payload)
        if not response["ok"]:
            self._raise_for_error(response)

    async def click(self, target: str, window_id: str | None = None) -> None:
        """Clicks the component identified by `target`. Fire-and-forget on
        the agent side: this only confirms the agent accepted the command,
        not that the click's effect has landed. `window_id`, if given, scopes
        the search to one window."""
        payload: dict[str, Any] = {"cmd": "click", "target": target}
        if window_id is not None:
            payload["window_id"] = window_id
        response = await self._request(payload)
        if not response["ok"]:
            self._raise_for_error(response)

    async def set_text_near_label(
        self,
        label: str,
        index: int,
        value: str | Secret,
        window_id: str | None = None,
    ) -> None:
        """Sets the text of the `index`-th text field found within the
        container of the component labelled `label` -- for fields with no
        accessible name of their own. `value` may be a `Secret` or a plain
        string, same unwrap contract as `set_text`. `window_id`, if given,
        scopes the search to one window."""
        real_value = value.get_secret_value() if isinstance(value, Secret) else value
        payload: dict[str, Any] = {
            "cmd": "set_text_near_label",
            "label": label,
            "index": index,
            "value": real_value,
        }
        if window_id is not None:
            payload["window_id"] = window_id
        response = await self._request(payload)
        if not response["ok"]:
            self._raise_for_error(response)

    async def navigate_menu(self, path: str, window_id: str | None = None) -> bool:
        """Walks a menu path (e.g. `"File/Close"`) and clicks the item at the
        end. `click` cannot reach menu items -- a menu's dropdown lives
        outside the ordinary component tree while closed. Fire-and-forget
        once a click is actually queued.

        `window_id`, if given, resolves directly to that window's own menu
        bar (matching `menu_item_exists`'s scoping) instead of the agent's
        own global "first displayable frame with a menu bar" search (#40) --
        `None` (the default) keeps that global search, for callers that
        don't have a validated main-window id to give (e.g. Gateway, whose
        `login.py` path never captures one, see `LoginManager.main_window_id`).

        Returns whether the item was actually clicked: `False` means the
        resolved item exists but is currently disabled (a retriable
        condition, e.g. a blocking dialog still open), distinct from
        `ElementNotFoundError` (the path itself doesn't resolve to anything).
        `actions.navigate_menu` owns the retry loop this return value is
        for."""
        payload: dict[str, Any] = {"cmd": "navigate_menu", "path": path}
        if window_id is not None:
            payload["window_id"] = window_id
        response = await self._request(payload)
        if not response["ok"]:
            self._raise_for_error(response)
        return bool(_field(response, "clicked"))

    async def expand_tree(self, path: str, window_id: str | None = None) -> None:
        """Selects a section of the Global Configuration dialog's tree (e.g.
        `"API/Settings"`). Unlike `navigate_menu`/`click`, this can be
        trusted immediately -- selecting a tree node only swaps an
        already-visible panel, it can't open a new modal. `window_id`, if
        given, resolves directly to that dialog instead of searching by
        title."""
        payload: dict[str, Any] = {"cmd": "expand_tree", "path": path}
        if window_id is not None:
            payload["window_id"] = window_id
        response = await self._request(payload)
        if not response["ok"]:
            self._raise_for_error(response)

    async def menu_item_exists(self, path: str, window_id: str) -> tuple[bool, bool]:
        """Reports whether `path` (e.g. `"Help/About Gateway"`) resolves to a
        real menu item under `window_id`'s menu bar, and whether it's
        currently enabled, without clicking it. Returns `(exists, enabled)`.
        `window_id` is required -- diagnostic use, not general scoping."""
        response = await self._request(
            {"cmd": "menu_item_exists", "path": path, "window_id": window_id}
        )
        if not response["ok"]:
            self._raise_for_error(response)
        return bool(_field(response, "exists")), bool(_field(response, "enabled"))


class AgentEventConnection:
    """One connection to the agent's event socket. Pure server push --
    `messages()` is a plain async iterator over whatever the agent sends, in
    order. Fan-out and filtering are `dispatch.py`'s job, not this class's."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(
            path=self._path, limit=_STREAM_READ_LIMIT
        )

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def __aenter__(self) -> AgentEventConnection:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def messages(self) -> AsyncIterator[AgentEventMessage]:
        """Yields each event message from the agent, in order, until the
        connection closes. Raises `ProtocolError` if not connected, or on a
        malformed line."""
        if self._reader is None:
            raise ProtocolError("not connected")
        while True:
            try:
                raw = await self._reader.readline()
            except (OSError, ValueError) as exc:  # ValueError: line over the limit
                raise ProtocolError(f"event connection failed: {exc}") from exc
            if not raw:
                return
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ProtocolError(f"malformed event line: {_excerpt(raw)}") from exc
            if not isinstance(data, dict):
                raise ProtocolError(f"unexpected event shape: {_excerpt(data)}")
            yield parse_event_message(data)
