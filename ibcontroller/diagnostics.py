"""Built-in replacement for poking the agent socket by hand (`socat`/`nc`)
during layer-by-layer development: `watch_for_diagnostics` runs for the
agent's whole lifetime, dumping component structure for windows matching
`Config.diagnostic_scope`/`diagnostic_when` and handing the formatted text
to a `Sink`.

Deliberately split into capture (`format_dump`, built on the existing
`actions.dump`), trigger (`watch_for_diagnostics`, modeled directly on
`recognisers.watch_for_unprompted_windows`), and sink (a plain callable) --
the log sink below (`log_sink`) is the only one shipped now, but an
interactive `diagnose` CLI or a control-API handler can pass a different
sink later without touching capture or trigger logic.

Off by default (`Config.diagnostic_when` defaults to `NEVER`)."""

from __future__ import annotations

import logging
from collections.abc import Callable

from ibcontroller.actions import dump, expand_tree
from ibcontroller.agent_client import (
    AgentClientError,
    Component,
    ElementNotFoundError,
    WindowEvent,
    WindowGoneError,
)
from ibcontroller.config import DiagnosticScope, DiagnosticWhen
from ibcontroller.dispatch import Dispatcher
from ibcontroller.recognisers import RecognizerRegistry

logger = logging.getLogger(__name__)

Sink = Callable[[WindowEvent, str], None]
"""Receives the triggering event and the formatted dump text (or a
one-line fallback, see `watch_for_diagnostics`). `log_sink` is the only
implementation shipped now."""


def log_sink(event: WindowEvent, text: str) -> None:
    logger.info("diagnostic dump (%s):\n%s", event.kind, text)


def format_dump(event: WindowEvent, components: list[Component]) -> str:
    """One line per component: class, name/accessible_name/text (when
    present -- `dump` already omits `text` for credential fields, nothing
    extra to redact here), enabled/visible."""
    header = (
        f"{event.kind} {event.window.class_} {event.window.title!r} "
        f"window_id={event.window.window_id}"
    )
    return "\n".join([header, *_component_lines(components)])


def _component_lines(components: list[Component]) -> list[str]:
    lines = []
    for component in components:
        parts = [component.class_]
        if component.name:
            parts.append(f"name={component.name!r}")
        if component.accessible_name:
            parts.append(f"accessible_name={component.accessible_name!r}")
        if component.text:
            parts.append(f"text={component.text!r}")
        parts.append(f"enabled={component.enabled}")
        parts.append(f"visible={component.visible}")
        lines.append("  " + " ".join(parts))
    return lines


async def watch_for_diagnostics(
    registry: RecognizerRegistry,
    dispatcher: Dispatcher,
    scope: DiagnosticScope,
    when: DiagnosticWhen,
    sink: Sink = log_sink,
) -> None:
    """Runs for the agent's whole lifetime (meant to be started via
    `asyncio.ensure_future`, like `watch_for_unprompted_windows`; never
    returns on its own). A no-op if `when` is `NEVER`, so scheduling this
    unconditionally is safe.

    `scope` is checked against `registry.recognises()` -- the same
    recognisers `watch_for_unprompted_windows` dispatches through, not a
    separate classification. A `window_closed` event can't be dumped (the
    window is already gone by the time this task reacts to the push over
    the socket -- no in-process synchronous hook the way IBC's own
    same-JVM listener has); `sink` still gets called, with a one-line
    fallback noting the window's identity instead of its structure."""
    if when is DiagnosticWhen.NEVER:
        return

    wanted_kinds = (
        {"window_opened"}
        if when is DiagnosticWhen.OPEN
        else {"window_opened", "window_closed"}
    )

    with dispatcher.subscribe() as sub:
        async for event in sub:
            if event.kind not in wanted_kinds:
                continue
            try:
                components = await dump(
                    dispatcher,
                    window=event.window.title,
                    window_id=event.window.window_id,
                )
            except WindowGoneError:
                sink(
                    event,
                    f"{event.kind} {event.window.class_} {event.window.title!r} "
                    "(window already gone, no structure)",
                )
                continue
            except AgentClientError as exc:  # diagnostics must not end the cycle
                logger.warning(
                    "diagnostic dump failed for %r: %s", event.window.title, exc
                )
                continue
            if scope is not DiagnosticScope.ALL:
                known = registry.recognises(event, components)
                if (scope is DiagnosticScope.KNOWN) != known:
                    continue
            sink(event, format_dump(event, components))


def sweep_sink(text: str) -> None:
    logger.info("settings sweep:\n%s", text)


async def sweep_settings_tree(
    dispatcher: Dispatcher,
    window_id: str | None,
    paths: list[str],
    sink: Callable[[str], None] = sweep_sink,
) -> None:
    """Selects each tree node in `paths` of the open Global Configuration
    dialog and hands its panel dump to `sink`. Read-only. A path the running
    build lacks (`ElementNotFoundError`) is reported and skipped; the tree
    cannot be enumerated, so `paths` is a probe list."""
    for path in paths:
        try:
            await expand_tree(dispatcher, path, window_id=window_id)
        except ElementNotFoundError:
            sink(f"{path!r} not found")
            continue
        components = await dump(dispatcher, window_id=window_id)
        sink(
            "\n".join(
                [f"{path!r} window_id={window_id}", *_component_lines(components)]
            )
        )
