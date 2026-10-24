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
from collections.abc import Mapping, Sequence
from importlib import resources
from typing import Any

import attrs
import cattrs

from ibcontroller.actions import click
from ibcontroller.config import ENV_FALSE, ENV_TRUE, ConfigError
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


def _structure_catalog(data: Mapping[str, Any], source: str) -> DialogCatalog:
    try:
        return _converter.structure(data, DialogCatalog)
    except cattrs.ClassValidationError as exc:
        details = "; ".join(cattrs.transform_error(exc, format_exception=_describe))
        raise DialogsError(f"{source}: {details}") from exc
    except DialogsError as exc:
        raise DialogsError(f"{source}: {exc}") from exc


def parse_catalog(raw: str, *, source: str) -> DialogCatalog:
    """Parses `raw` TOML into a `DialogCatalog`. Raises `DialogsError` naming
    `source` for invalid TOML, an unknown key, a wrong type or an invalid
    entry."""
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise DialogsError(f"{source}: invalid TOML: {exc}") from exc
    data.pop("setting", None)  # same file also holds the settings entries
    return _structure_catalog(data, source)


def parse_custom_dialogs(
    raw_entries: Sequence[Mapping[str, Any]], *, source: str
) -> list[DialogEntry]:
    """Builds the user's `[[dialog]]` entries from plain data, with the same
    checks as the bundled catalog. Raises `DialogsError` naming `source`."""
    return _structure_catalog({"dialog": list(raw_entries)}, source).dialog


def load_catalog() -> DialogCatalog:
    """Loads the bundled `data/catalog.toml`. Blocking file read: call it off
    the event loop."""
    raw = (
        resources.files("ibcontroller")
        .joinpath("data", "catalog.toml")
        .read_text(encoding="utf-8")
    )
    return parse_catalog(raw, source="bundled catalog.toml")


@attrs.define(frozen=True)
class DialogChoice:
    """An entry with the mode it runs in and where that mode came from:
    `default`, `file` or `env`."""

    entry: DialogEntry
    mode: str
    source: str


def _mode_for(entry: DialogEntry, value: object, source: str) -> str:
    """Maps a configured value to one of the entry's modes: a boolean is `on` or
    `off`, a string is a mode name, ignoring case. From the environment
    `true`/`yes` and `false`/`no` count as booleans."""
    if source == "env" and isinstance(value, str):
        word = value.strip().casefold()
        if word in ENV_TRUE | ENV_FALSE:
            value = word in ENV_TRUE
    where = f"dialog {entry.name!r} ({source})"
    if isinstance(value, bool):
        mode = "on" if value else "off"
        if not {"on", "off"} <= entry.modes.keys():
            raise DialogsError(
                f"{where}: a boolean needs both 'on' and 'off' modes; "
                f"modes: {', '.join(entry.modes)}"
            )
        return mode
    if not isinstance(value, str):
        raise DialogsError(
            f"{where}: expected a mode name or a boolean, got {type(value).__name__}"
        )
    for name in entry.modes:
        if name.casefold() == value.strip().casefold():
            return name
    raise DialogsError(
        f"{where}: unknown mode {value!r}; modes: {', '.join(entry.modes)}"
    )


def resolve_dialogs(
    catalog: DialogCatalog,
    custom: Sequence[DialogEntry],
    ibkr: Mapping[str, Any],
    sources: Mapping[str, str],
) -> list[DialogChoice]:
    """Picks a mode per entry from `ibkr` (the `[ibkr]` values, environment
    merged over file; `sources` says which) or the entry's default. Custom
    entries come first, so they win over catalog entries. Keys of `ibkr` that
    name no dialog are not this module's: settings read them. Raises
    `DialogsError` naming the entry."""
    bundled = {entry.name for entry in catalog.dialog}
    for entry in custom:
        if entry.name in bundled:
            raise DialogsError(
                f"custom dialog {entry.name!r} has the name of a catalog entry"
            )
    choices: list[DialogChoice] = []
    for entry in [*custom, *catalog.dialog]:
        if entry.name in ibkr:
            source = sources.get(entry.name, "file")
            mode = _mode_for(entry, ibkr[entry.name], source)
            choices.append(DialogChoice(entry, mode, source))
        else:
            choices.append(DialogChoice(entry, entry.default, "default"))
    return choices
