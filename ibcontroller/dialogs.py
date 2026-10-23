"""Declarative dialog entries: how a dialog is recognised and what is done to
it, as data.

A `DialogEntry` has a name, a default mode, a list of `MatchGroup`s (any group
matching is enough) and a map from mode name to the ordered steps run on the
matched window. A mode with no steps leaves the dialog to the user.
`recognisers.DialogRecognizer` applies one entry in one chosen mode;
`run_steps` runs the steps. Malformed entries fail at construction with a
`DialogsError`, so a bad catalog is a startup error and not a surprise at the
first dispatch.
"""

from __future__ import annotations

from collections.abc import Sequence

import attrs

from ibcontroller.actions import click
from ibcontroller.config import ConfigError
from ibcontroller.dispatch import Dispatcher


class DialogsError(ConfigError):
    """A malformed dialog entry -- no match group, a group without a title or
    text, an empty `click`, no modes, or a default that is not one of the
    modes. A `ConfigError`, so `cli.py` exits 78."""


@attrs.define(frozen=True)
class MatchGroup:
    """One way to recognise a dialog. Every field given must match (AND);
    `text` and `text_also` are two separate text conditions, each satisfied by
    any component. At least one of `title`/`text` is required."""

    title: str | None = None
    text: str | None = None
    text_also: str | None = None

    def __attrs_post_init__(self) -> None:
        if self.title is None and self.text is None:
            raise DialogsError("a match group needs at least one of 'title'/'text'")


@attrs.define(frozen=True)
class ClickStep:
    """Click the first of `click` that is present: one label, or an ordered
    list of candidates."""

    click: str | list[str]

    def __attrs_post_init__(self) -> None:
        if not self.click:
            raise DialogsError("a click step needs a label or a non-empty list")


Step = ClickStep


@attrs.define(frozen=True)
class DialogEntry:
    name: str
    default: str
    match: list[MatchGroup]
    modes: dict[str, list[Step]]

    def __attrs_post_init__(self) -> None:
        if not self.match:
            raise DialogsError(f"dialog {self.name!r}: needs at least one match group")
        if not self.modes:
            raise DialogsError(
                f"dialog {self.name!r}: needs at least one entry in modes"
            )
        if self.default not in self.modes:
            known = ", ".join(self.modes)
            raise DialogsError(
                f"dialog {self.name!r}: default {self.default!r} is not one of: {known}"
            )


async def run_steps(
    dispatcher: Dispatcher, steps: Sequence[Step], *, window_id: str | None
) -> None:
    """Runs `steps` in order against the window `window_id`. The first failing
    step stops the sequence and its exception propagates: later steps usually
    depend on the earlier ones."""
    for step in steps:
        if isinstance(step, ClickStep):
            await click(dispatcher, step.click, window_id=window_id)
        else:
            raise TypeError(f"unknown dialog step {step!r}")
