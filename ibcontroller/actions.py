"""The shared action vocabulary every decision-maker (built-in recognisers,
config rules, plugins, delegate responses) acts through -- a config rule's
action string, a delegate's JSON, and a plugin's direct Python call all
resolve to the same function.

Each function is `async def foo(dispatcher, ...)` and runs one command by
sending its `agent_client.Request` through `dispatcher.send`. Errors are
`agent_client.py`'s typed exceptions (`ElementNotFoundError`,
`CredentialRefusedError`, `BadRequestError`, `UnknownCommandError`,
`AgentError`, `ProtocolError`), propagated unchanged -- this module does not
re-translate them, except that `click`'s candidate-label fallback below
branches on `ElementNotFoundError` specifically.

`ACTIONS` maps action names to these functions, for callers that select an
action by name (config rules, delegate JSON, plugins) rather than calling it
directly. `wait_for_window` is not in `ACTIONS` -- it's a caller-side
primitive, paired directly with `click`/`navigate_menu` by whoever needs to
know an action's effect has happened.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence

from typed_settings.types import Secret

from ibcontroller.agent_client import (
    Click,
    Component,
    Dump,
    ElementNotFoundError,
    ExpandTree,
    GetText,
    MenuItemExists,
    NavigateMenu,
    SetCheckbox,
    SetText,
    SetTextNearLabel,
    WindowInfo,
)
from ibcontroller.dispatch import Dispatcher

logger = logging.getLogger(__name__)

_MENU_RETRY_INTERVAL = 0.25
"""Delay, in seconds, between `navigate_menu` retries while the resolved
menu item is disabled."""

_MENU_NOT_FOUND_TIMEOUT = 5.0
"""Default seconds `navigate_menu` keeps retrying a path that does not resolve:
covers TWS populating its menu bar after login (#37, measured 0.3-0.6s)."""


async def type_text(
    dispatcher: Dispatcher,
    field: str,
    value: str | Secret,
    *,
    window_id: str | None = None,
) -> None:
    """Sets `field`'s text to `value`. `value` may be a `Secret`
    (credentials) or a plain string -- only ever unwrapped inside
    `Request.wire`. `window_id`, if given, scopes the action to one
    window."""
    await dispatcher.send(SetText(field, value, window_id))


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
    effect has happened yet -- pair with `wait_for_window` when the caller
    needs to know. `window_id`, if given, scopes the action to one window."""
    candidates = [labels] if isinstance(labels, str) else list(labels)
    last_error: ElementNotFoundError | None = None
    for label in candidates:
        try:
            await dispatcher.send(Click(label, window_id))
            return
        except ElementNotFoundError as exc:
            last_error = exc
    if last_error is None:
        raise ValueError("click needs at least one candidate label")
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
    await dispatcher.send(SetCheckbox(label, checked, window_id))


async def dismiss(
    dispatcher: Dispatcher,
    labels: str | Sequence[str],
    *,
    window_id: str | None = None,
) -> None:
    """Clicks `labels` (the first candidate found) to dismiss a generic
    pop-up; callers pass `Labels.generic.dismiss_buttons` when they have no
    more specific one. `window_id`, if given, scopes the action to one window."""
    await click(dispatcher, labels, window_id=window_id)


async def read_text(
    dispatcher: Dispatcher, field: str, *, window_id: str | None = None
) -> str:
    """Returns the text of `field`; needed to read a settings field before
    writing it, so an unchanged value is left alone and a change that raises a
    confirmation dialog can be anticipated. `window_id`, if given, scopes the
    read to one window. Raises `CredentialRefusedError` if `field` is a password
    field."""
    return await dispatcher.send(GetText(field, window_id))


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
    await dispatcher.send(SetTextNearLabel(label, index, value, window_id))


async def navigate_menu(
    dispatcher: Dispatcher,
    path: str,
    *,
    timeout: float = 30.0,
    not_found_timeout: float = _MENU_NOT_FOUND_TIMEOUT,
    window_id: str | None = None,
) -> None:
    """Walks menu `path` (e.g. `"File/Close"`) and clicks the item at the
    end. Use this for menu items -- `click` only reaches components in the
    ordinary component tree, not menu dropdowns.

    `window_id`, when given, scopes the lookup to that window's own menu
    bar (matching `menu_item_exists`'s scoping) instead of the agent's
    global "first displayable frame with a menu bar" search -- the wrong
    frame can otherwise be picked when more than one displayable frame
    carries a menu bar at once (#40; `open_settings_dialog` passes
    `LoginManager.main_window_id`, once `login.py` has validated it, for
    exactly this reason). `None` (the default) keeps the old global search.

    Retries every `_MENU_RETRY_INTERVAL` seconds while the resolved item is
    disabled (e.g. a blocking dialog is still open), up to `timeout` seconds,
    then raises `TimeoutError`. A path that does not resolve (TWS still
    populating its menubar right after login, #37) is retried for only
    `not_found_timeout` seconds from its first failure, then
    `ElementNotFoundError` is re-raised: a wrong path fails fast instead of
    spending `timeout`. The not-found clock restarts if the path resolves and
    the item is then disabled. Both are capped by `timeout`. IBC's
    `Utils.invokeMenuItem` is the model; this project's equivalent of its
    `MainWindowManager`/`SessionManager.awaitReady()` gate is
    `LoginManager.main_window_id`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    not_found_since: float | None = None
    while True:
        try:
            clicked = await dispatcher.send(NavigateMenu(path, window_id))
        except ElementNotFoundError:
            now = loop.time()
            if not_found_since is None:
                not_found_since = now
                logger.info(
                    "IBController > menu path %r not found (window_id=%s), "
                    "retrying for up to %ss",
                    path,
                    window_id,
                    not_found_timeout,
                )
            if now >= deadline or now - not_found_since >= not_found_timeout:
                raise
            await asyncio.sleep(_MENU_RETRY_INTERVAL)
            continue
        not_found_since = None
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
    await dispatcher.send(ExpandTree(path, window_id))


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
    return await dispatcher.send(MenuItemExists(path, window_id))


async def dump(
    dispatcher: Dispatcher,
    *,
    window: str | None = None,
    window_id: str | None = None,
) -> list[Component]:
    """Returns the current component tree, optionally scoped by `window` (a
    title substring) or `window_id` (an exact window, taking precedence when
    both are given). Read-only."""
    return await dispatcher.send(Dump(window, window_id))


async def wait_for_window(
    dispatcher: Dispatcher,
    matches: Callable[[WindowInfo], bool],
    *,
    closed: bool = False,
    timeout: float,
) -> WindowInfo | None:
    """Level wait: returns once a window matching `matches` is open (or, with
    `closed`, once none is), checking `dispatcher.windows` first and then
    waiting for the event. Returns the matching `WindowInfo`, or `None` when
    waiting for close. Raises `TimeoutError` after `timeout` seconds; an
    exception in `matches` propagates."""
    # Subscribe before reading the registry so no event lands in between.
    with dispatcher.subscribe() as sub:
        current = next((w for w in dispatcher.windows.values() if matches(w)), None)
        if closed:
            if current is None:
                return None
            await sub.wait(
                "window_closed", lambda e: matches(e.window), timeout=timeout
            )
            return None
        if current is not None:
            return current
        event = await sub.wait(
            "window_opened", lambda e: matches(e.window), timeout=timeout
        )
        return event.window


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
that select an action by name rather than calling it directly. Reserved API:
no production caller selects by name yet."""
