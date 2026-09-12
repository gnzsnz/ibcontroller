"""The shared action vocabulary every decision-maker (built-in recognisers,
config rules, plugins, delegate responses) acts through -- a config rule's
action string, a delegate's JSON, and a plugin's direct Python call all
resolve to the same function.

Each function is `async def foo(dispatcher, ...)` and runs one command
through `dispatcher.send_command`, reached via `dispatcher.command_conn` (an
`AgentCommandConnection`, see `agent_client.py`). Errors are
`agent_client.py`'s typed exceptions (`ElementNotFoundError`,
`CredentialRefusedError`, `BadRequestError`, `UnknownCommandError`,
`AgentError`, `ProtocolError`), propagated unchanged -- this module does not
re-translate them, except that `click`'s candidate-label fallback below
branches on `ElementNotFoundError` specifically.

`ACTIONS` maps action names to these functions, for callers that select an
action by name (config rules, delegate JSON, plugins) rather than calling it
directly. `wait_for_event` is not in `ACTIONS` -- it's a caller-side
primitive, paired directly with `click`/`navigate_menu` by whoever needs to
know an action's effect has happened.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast

from typed_settings.types import Secret

from ibcontroller.agent_client import (
    Component,
    ElementNotFoundError,
    WindowEvent,
)
from ibcontroller.dispatch import Dispatcher

_DEFAULT_DISMISS_LABELS = ("OK", "Close", "Dismiss")


async def _forward(dispatcher: Dispatcher, method: str, *args: object) -> Any:
    """Runs one `AgentCommandConnection` method (named by `method`) through
    the dispatcher's command queue. Shared body for every action below that
    adds no logic beyond that."""
    return await dispatcher.send_command(
        functools.partial(getattr(dispatcher.command_conn, method), *args)
    )


async def type_text(
    dispatcher: Dispatcher,
    field: str,
    value: str | Secret,
    *,
    window_id: str | None = None,
) -> None:
    """Sets `field`'s text to `value`. `value` may be a `Secret`
    (credentials) or a plain string -- only ever unwrapped inside
    `agent_client.set_text`. `window_id`, if given, scopes the action to one
    window."""
    await _forward(dispatcher, "set_text", field, value, window_id)


async def click(
    dispatcher: Dispatcher,
    labels: str | Sequence[str],
    *,
    window_id: str | None = None,
) -> None:
    """Clicks the first of `labels` that resolves. `labels` may be a single
    label or an ordered list of fallback candidates. Only
    `ElementNotFoundError` moves on to the next candidate; any other error
    propagates immediately.

    Fire-and-forget: confirms the agent accepted the command, not that its
    effect has happened yet -- pair with `wait_for_event` when the caller
    needs to know. `window_id`, if given, scopes the action to one window."""
    candidates = [labels] if isinstance(labels, str) else list(labels)
    last_error: ElementNotFoundError | None = None
    for label in candidates:
        try:
            await dispatcher.send_command(
                functools.partial(dispatcher.command_conn.click, label, window_id)
            )
            return
        except ElementNotFoundError as exc:
            last_error = exc
    assert last_error is not None  # candidates is never empty in practice
    raise last_error


async def toggle(
    dispatcher: Dispatcher,
    label: str,
    checked: bool,
    *,
    window_id: str | None = None,
) -> None:
    """Sets `label`'s checkbox to `checked`. Idempotent on the agent side --
    only clicks if the current state differs from `checked`. `window_id`, if
    given, scopes the action to one window."""
    await _forward(dispatcher, "set_checkbox", label, checked, window_id)


async def dismiss(
    dispatcher: Dispatcher,
    labels: str | Sequence[str] | None = None,
    *,
    window_id: str | None = None,
) -> None:
    """Clicks `labels` to dismiss a generic pop-up, defaulting to common
    OK/Close/Dismiss labels when the caller doesn't have a more specific
    one. `window_id`, if given, scopes the action to one window."""
    await click(
        dispatcher,
        labels if labels is not None else _DEFAULT_DISMISS_LABELS,
        window_id=window_id,
    )


async def read_text(dispatcher: Dispatcher, field: str) -> str:
    """Returns the text of `field`. Raises `CredentialRefusedError` if
    `field` is a password field."""
    return await _forward(dispatcher, "get_text", field)


async def type_text_near_label(
    dispatcher: Dispatcher,
    label: str,
    index: int,
    value: str | Secret,
    *,
    window_id: str | None = None,
) -> None:
    """Sets the text of the `index`-th field found near `label`, for fields
    with no accessible name of their own. `value` may be a `Secret` or a
    plain string, same contract as `type_text`. `window_id`, if given,
    scopes the action to one window."""
    await _forward(dispatcher, "set_text_near_label", label, index, value, window_id)


_MENU_RETRY_INTERVAL = 0.25
"""Delay, in seconds, between `navigate_menu` retries while the resolved
menu item is disabled."""


async def navigate_menu(
    dispatcher: Dispatcher,
    path: str,
    *,
    timeout: float = 30.0,
) -> None:
    """Walks menu `path` (e.g. `"File/Close"`) and clicks the item at the
    end. Use this for menu items -- `click` only reaches components in the
    ordinary component tree, not menu dropdowns.

    Retries every `_MENU_RETRY_INTERVAL` seconds while the resolved item is
    disabled (e.g. a blocking dialog is still open), up to `timeout` seconds,
    then raises `TimeoutError`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        clicked = await dispatcher.send_command(
            functools.partial(dispatcher.command_conn.navigate_menu, path)
        )
        if clicked:
            return
        if loop.time() >= deadline:
            raise TimeoutError(f"menu item at {path!r} still disabled after {timeout}s")
        await asyncio.sleep(_MENU_RETRY_INTERVAL)


async def expand_tree(
    dispatcher: Dispatcher,
    path: str,
    *,
    window_id: str | None = None,
) -> None:
    """Selects a section of the Global Configuration dialog's tree (e.g.
    `"API/Settings"`). `window_id`, if given, resolves directly to that
    dialog instead of searching by title."""
    await _forward(dispatcher, "expand_tree", path, window_id)


async def menu_item_exists(
    dispatcher: Dispatcher,
    path: str,
    *,
    window_id: str,
) -> tuple[bool, bool]:
    """Reports whether menu item `path` exists and is enabled under
    `window_id`, without clicking it. Returns `(exists, enabled)`.
    Diagnostic use -- e.g. checking readiness, or researching a new
    declarative recogniser/setting before it's authored."""
    return await _forward(dispatcher, "menu_item_exists", path, window_id)


async def dump(
    dispatcher: Dispatcher,
    *,
    window: str | None = None,
    window_id: str | None = None,
) -> list[Component]:
    """Returns the current component tree, optionally scoped by `window` (a
    title substring) or `window_id` (an exact window, taking precedence when
    both are given). Read-only."""
    return await _forward(dispatcher, "dump", window, window_id)


async def wait_for_event(
    dispatcher: Dispatcher,
    kind: str,
    matches: Callable[[WindowEvent], bool] = lambda _event: True,
    *,
    timeout: float | None,
) -> WindowEvent:
    """Waits for the next `dispatcher.window_events` event of the given
    `kind` for which `matches(event)` is true, up to `timeout` seconds
    (`None` for no timeout). Pairs with `click`/`navigate_menu` where a
    caller needs to know an action's effect has happened, not just that the
    request was sent. Raises `asyncio.TimeoutError` if no matching event
    arrives in time."""
    filtered = dispatcher.window_events.filter(
        lambda event: event.kind == kind and matches(event)
    )
    try:
        # window_events only ever emits WindowEvent (dispatch.py's
        # _event_reader), so this narrows what's already true at runtime.
        return cast(WindowEvent, await asyncio.wait_for(filtered, timeout=timeout))
    finally:
        dispatcher.window_events.disconnect(
            filtered.on_source, filtered.on_source_error, filtered.on_source_done
        )


ACTIONS: dict[str, Callable[..., Awaitable[object]]] = {
    "type_text": type_text,
    "click": click,
    "toggle": toggle,
    "dismiss": dismiss,
    "read_text": read_text,
    "navigate_menu": navigate_menu,
    "expand_tree": expand_tree,
    "type_text_near_label": type_text_near_label,
    "menu_item_exists": menu_item_exists,
    "dump": dump,
}
"""Name -> function table for callers (config rules, delegate JSON, plugins)
that select an action by name rather than calling it directly."""
