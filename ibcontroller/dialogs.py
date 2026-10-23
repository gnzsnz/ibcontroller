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

import tomllib
from collections.abc import Sequence
from importlib import resources
from typing import Any

import attrs
import cattrs

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


@attrs.define(frozen=True)
class DialogCatalog:
    """The `[[dialog]]` entries of one catalog file, in file order. Names are
    unique."""

    dialog: list[DialogEntry] = attrs.field(factory=list)

    def __attrs_post_init__(self) -> None:
        seen: set[str] = set()
        for entry in self.dialog:
            if entry.name in seen:
                raise DialogsError(f"duplicate dialog name {entry.name!r}")
            seen.add(entry.name)


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


def _structure_str(value: Any, _type: Any) -> str:
    if not isinstance(value, str):
        raise DialogsError(f"expected a string, got {type(value).__name__}")
    return value


def _structure_str_list(value: Any, _type: Any) -> list[str]:
    # A bare string is iterable: cattrs would split it into characters.
    if not isinstance(value, list):
        raise DialogsError(f"expected a list of strings, got {type(value).__name__}")
    return [_structure_str(item, str) for item in value]


def _structure_str_or_list(value: Any, _type: Any) -> str | list[str]:
    return value if isinstance(value, str) else _structure_str_list(value, _type)


# Extra keys are an error: the catalog is bundled data, so a stray key is a bug
# to fail on, not a typo to warn about.
_converter = cattrs.Converter(forbid_extra_keys=True)
_converter.register_structure_hook(str, _structure_str)
_converter.register_structure_hook_func(lambda t: t == list[str], _structure_str_list)
_converter.register_structure_hook_func(
    lambda t: t == (str | list[str]), _structure_str_or_list
)


def _describe(exception: BaseException, _type: type | None) -> str:
    if isinstance(exception, cattrs.ForbiddenExtraKeysError):
        keys = ", ".join(repr(key) for key in sorted(exception.extra_fields))
        return f"unknown key {keys}"
    return str(exception) if isinstance(exception, DialogsError) else repr(exception)


def parse_catalog(raw: str, *, source: str) -> DialogCatalog:
    """Parses `raw` TOML into a `DialogCatalog`. Raises `DialogsError` naming
    `source` for invalid TOML, an unknown key, a wrong type or an invalid
    entry."""
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise DialogsError(f"{source}: invalid TOML: {exc}") from exc
    try:
        return _converter.structure(data, DialogCatalog)
    except cattrs.ClassValidationError as exc:
        details = "; ".join(cattrs.transform_error(exc, format_exception=_describe))
        raise DialogsError(f"{source}: {details}") from exc
    except DialogsError as exc:
        raise DialogsError(f"{source}: {exc}") from exc


def load_catalog() -> DialogCatalog:
    """Loads the bundled `data/catalog.toml`. Blocking file read: call it off
    the event loop."""
    raw = (
        resources.files("ibcontroller")
        .joinpath("data", "catalog.toml")
        .read_text(encoding="utf-8")
    )
    return parse_catalog(raw, source="bundled catalog.toml")
