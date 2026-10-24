"""settings.py -- L5's Settings domain, alongside `login.py` (Login) and
`management.py` (Management).

Drives IBKR's Global Configuration dialog: open it, navigate to a section,
set a value, repeat for every setting with a value, then click "OK" once to
commit and close. A plain sequence, not a state machine -- built on the same
`actions.py` primitives (`navigate_menu`, `expand_tree`, `toggle`, `type_text`,
`type_text_near_label`, `click`) every other domain uses.

A setting is a `SettingEntry`, data and not code: a name, where the control
is (`tree_path`, `label` or `label_ref`) and an `action`. The bundled
`data/catalog.toml` holds the entries ibcontroller knows; a user adds more as
`[[setting]]` in `ibcontroller.toml`. The value comes from `[ibkr]` (file) or
`IBKR_<NAME>` (environment); `resolve_settings` checks it against the entry's
action at startup. A setting with no value is left alone -- the deliberate
three-state (set/clear/leave-unchanged) mechanism, matching IBC's own
`config.ini` semantics for yes/no settings.

Five actions are supported: `toggle` (a bool), `type_text` and
`type_text_near_label` (a string), `auto_restart_time` and `auto_logoff_time`
(an `"hh:mm AM/PM"` string; each one drives three writes: the time field, the
AM/PM radio and the "Auto restart"/"Auto logoff" radio, see `_parse_time_ampm`).
Both time settings share the Lock and Exit radio pair, so the catalog lists
`auto_logoff_time` before `auto_restart_time`: choices keep catalog order, the
last one applied wins the radio group, matching IBC's stated precedence.
Button-press entries with no value and dropdown (`JComboBox`) controls are not
supported yet.

`apply_settings` applies at instance startup only, before or around Login.
Each entry's failure is caught and logged independently, so one bad entry
doesn't abort the rest."""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from importlib import resources
from typing import TYPE_CHECKING, Any

import attrs
import cattrs

from ibcontroller.actions import (
    click,
    expand_tree,
    navigate_menu,
    toggle,
    type_text,
    type_text_near_label,
    wait_for_window,
)
from ibcontroller.agent_client import (
    AgentError,
    ElementNotFoundError,
    ProtocolError,
    WindowGoneError,
)
from ibcontroller.config import ENV_FALSE, ENV_TRUE, Config, ConfigError, Program
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import Labels, SettingsLabels

if TYPE_CHECKING:
    from ibcontroller.login import SessionState

logger = logging.getLogger(__name__)

_NOON_HOUR = 12  # datetime.hour is 0-23; below this is AM, at/above is PM
_TIME_ACTIONS = frozenset({"auto_restart_time", "auto_logoff_time"})
_TEXT_ACTIONS = frozenset({"type_text", "type_text_near_label"})
_ACTIONS = _TIME_ACTIONS | _TEXT_ACTIONS | {"toggle"}


class SettingsError(ConfigError):
    """A malformed setting entry or value (unknown `action`, a wrong label
    key, a value of the wrong type), or a failure of this module's own
    sequencing: the Global Configuration dialog never appeared, a wanted
    section/setting couldn't be found, an unresolvable `label_ref`.
    `actions.py`'s own typed exceptions (`TimeoutError`, `ElementNotFoundError`)
    propagate unchanged where relevant."""


@attrs.define(frozen=True)
class SettingEntry:
    """One setting: where its control is and how to write it. The wanted value
    is not part of the entry, it comes from `[ibkr]`/`IBKR_<NAME>`."""

    name: str
    tree_path: str
    action: str
    label: str | None = None
    label_ref: str | None = None
    field_index: int = 0
    """Used only by `"type_text_near_label"` entries, to find a field with
    no accessible name of its own by position within the container below
    the matched label. Ignored by every other action."""

    def __attrs_post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise SettingsError(
                f"setting {self.name!r}: unknown action {self.action!r}; "
                f"actions: {', '.join(sorted(_ACTIONS))}"
            )
        named = (self.label is not None) + (self.label_ref is not None)
        if self.action in _TIME_ACTIONS and named:
            raise SettingsError(
                f"setting {self.name!r}: {self.action!r} takes no 'label' or "
                "'label_ref', it knows its controls"
            )
        if self.action not in _TIME_ACTIONS and named != 1:
            raise SettingsError(
                f"setting {self.name!r}: needs exactly one of 'label'/'label_ref'"
            )


@attrs.define(frozen=True)
class SettingCatalog:
    """The `[[setting]]` entries of one file, in file order. Names are unique."""

    setting: list[SettingEntry] = attrs.field(factory=list)

    def __attrs_post_init__(self) -> None:
        seen: set[str] = set()
        for entry in self.setting:
            if entry.name in seen:
                raise SettingsError(f"duplicate setting name {entry.name!r}")
            seen.add(entry.name)


@attrs.define(frozen=True)
class SettingChoice:
    """An entry with its checked value and where the value came from: `file`
    or `env`."""

    entry: SettingEntry
    value: bool | str
    source: str


def _structure_str(value: Any, _type: Any) -> str:
    if not isinstance(value, str):
        raise SettingsError(f"expected a string, got {type(value).__name__}")
    return value


# Extra keys are an error: a stray key in a custom entry is a typo to fail on.
_converter = cattrs.Converter(forbid_extra_keys=True)
_converter.register_structure_hook(str, _structure_str)


def _describe(exception: BaseException, _type: type | None) -> str:
    if isinstance(exception, cattrs.ForbiddenExtraKeysError):
        keys = ", ".join(repr(key) for key in sorted(exception.extra_fields))
        return f"unknown key {keys}"
    return str(exception) if isinstance(exception, SettingsError) else repr(exception)


def _structure_catalog(data: Mapping[str, Any], source: str) -> SettingCatalog:
    try:
        return _converter.structure(data, SettingCatalog)
    except cattrs.ClassValidationError as exc:
        details = "; ".join(cattrs.transform_error(exc, format_exception=_describe))
        raise SettingsError(f"{source}: {details}") from exc
    except SettingsError as exc:
        raise SettingsError(f"{source}: {exc}") from exc


def parse_custom_settings(
    raw_entries: Sequence[Mapping[str, Any]], *, source: str
) -> list[SettingEntry]:
    """Builds the user's `[[setting]]` entries from plain data, with the same
    checks as the bundled catalog. Raises `SettingsError` naming `source`."""
    return _structure_catalog({"setting": list(raw_entries)}, source).setting


def load_setting_catalog() -> SettingCatalog:
    """Loads the `[[setting]]` entries of the bundled `data/catalog.toml`
    (the `[[dialog]]` entries are `dialogs.py`'s). Blocking file read: call it
    off the event loop."""
    source = "bundled catalog.toml"
    raw = (
        resources.files("ibcontroller")
        .joinpath("data", "catalog.toml")
        .read_text(encoding="utf-8")
    )
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{source}: invalid TOML: {exc}") from exc
    return _structure_catalog({"setting": data.get("setting", [])}, source)


def _value_for(entry: SettingEntry, raw: object, source: str) -> bool | str:
    """Checks `raw` against the entry's action and returns the value to write.
    From the environment `true`/`yes` and `false`/`no` count as booleans.
    Raises `SettingsError` naming the entry."""
    where = f"setting {entry.name!r} ({source})"
    if entry.action == "toggle":
        if source == "env" and isinstance(raw, str):
            word = raw.strip().casefold()
            if word in ENV_TRUE | ENV_FALSE:
                return word in ENV_TRUE
        if not isinstance(raw, bool):
            raise SettingsError(f"{where}: expected true or false, got {raw!r}")
        return raw
    if entry.action in _TIME_ACTIONS:
        if not isinstance(raw, str):
            raise SettingsError(f"{where}: expected a time string, got {raw!r}")
        try:
            _parse_time_ampm(raw, setting_name=entry.name)
        except SettingsError as exc:
            raise SettingsError(f"{where}: {exc}") from exc
        return raw
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise SettingsError(f"{where}: expected text or a number, got {raw!r}")
    return str(raw)


def resolve_settings(
    catalog: SettingCatalog,
    custom: Sequence[SettingEntry],
    ibkr: Mapping[str, Any],
    sources: Mapping[str, str],
) -> list[SettingChoice]:
    """Checks the `[ibkr]` value of every entry that has one (environment merged
    over file; `sources` says which) and returns the choices in apply order:
    catalog entries, then custom ones. An entry with no value is left out, so
    its setting stays untouched. Keys of `ibkr` that name no setting are not
    this module's: dialogs read them. Raises `SettingsError` naming the entry."""
    bundled = {entry.name for entry in catalog.setting}
    for entry in custom:
        if entry.name in bundled:
            raise SettingsError(
                f"custom setting {entry.name!r} has the name of a catalog entry"
            )
    choices: list[SettingChoice] = []
    for entry in [*catalog.setting, *custom]:
        if entry.name in ibkr:
            source = sources.get(entry.name, "file")
            choices.append(
                SettingChoice(
                    entry, _value_for(entry, ibkr[entry.name], source), source
                )
            )
    return choices


def _resolve_control(labels: SettingsLabels, name: str) -> str:
    try:
        return labels.controls[name]
    except KeyError:
        raise SettingsError(f"settings.controls has no entry named {name!r}") from None


def _resolve_label(entry: SettingEntry, labels: SettingsLabels) -> str:
    if entry.label is not None:
        return entry.label
    if entry.label_ref is not None:
        return _resolve_control(labels, entry.label_ref)
    raise SettingsError("a SettingEntry needs either 'label' or 'label_ref'")


def _parse_time_ampm(value: str, *, setting_name: str) -> tuple[str, str]:
    """Parses an `"hh:mm AM/PM"` string (e.g. `"08:00 AM"`) into `(time,
    ampm)`, where `time` is the zero-padded 12-hour `"HH:MM"` string for the
    time field and `ampm` is `"AM"`/`"PM"` for the radio pair. Raises
    `SettingsError` on a malformed value. Shared by both `"auto_restart_time"`
    and `"auto_logoff_time"` -- `setting_name` only names which one in the
    error message."""
    try:
        parsed = datetime.strptime(value, "%I:%M %p")
    except ValueError:
        raise SettingsError(
            f"{setting_name} setting must be hh:mm AM or hh:mm PM, for "
            f'example "09:30 AM" or "04:00 PM", got {value!r}'
        ) from None
    return parsed.strftime("%I:%M"), ("AM" if parsed.hour < _NOON_HOUR else "PM")


async def _await_menu_ready(
    dispatcher: Dispatcher,
    labels: SettingsLabels,
    *,
    timeout: float,
) -> None:
    """Waits for Gateway's own splash frame to close before Configure/
    Settings is touched at all -- Gateway raises an error if that menu is
    accessed before the splash closes, even once the item itself is
    enabled.

    Level wait: returns at once if the splash is not open now, otherwise
    waits for its `window_closed`."""
    await wait_for_window(
        dispatcher,
        lambda w: labels.splash_title_marker in (w.title or ""),
        closed=True,
        timeout=timeout,
    )


async def open_settings_dialog(
    dispatcher: Dispatcher,
    labels: SettingsLabels,
    *,
    program: Program = Program.GATEWAY,
    timeout: float = 180.0,
    main_window_id: str | None = None,
) -> str | None:
    """Opens the Global Configuration dialog and waits for it to actually
    appear (matched by the window title containing `labels.dialog_title_marker`,
    never an exact match -- the real title is account/trading-mode-specific).

    `program` selects `labels.gateway_menu_path` (`"Configure/Settings"`) or,
    for TWS, `labels.tws_menu_path` (`"File/Global Configuration..."`,
    Mosaic layout). TWS has no `Configure` menu at all. Only Mosaic is
    supported: Classic (`labels.tws_menu_path_classic`,
    `"Edit/Global Configuration..."`) has no `Edit` entry under Mosaic, so it
    fails with `ElementNotFoundError`.

    `main_window_id`, when given (`control_loop.py` passes
    `LoginManager.main_window_id`), scopes `navigate_menu` to that exact,
    already-validated main window instead of the agent's own global
    "first displayable frame with a menu bar" guess, which can land on the
    wrong frame. `None` (Gateway's path never captures one -- no ambiguity
    there) uses the global search.

    `timeout` bounds the wait for the splash frame to close
    (`_await_menu_ready`), the menu navigation itself (`navigate_menu`
    retries a disabled item for `timeout` and an unresolvable path briefly,
    e.g. TWS is still populating its menubar right after login), and the wait
    for the dialog to open. Needs to be long enough to span a 2FA wait, since
    Settings may be attempted right after login while Gateway is still gated
    on a human approving 2FA -- callers should pass `config.mfa_timeout`, not
    rely on this function's own default, to stay in sync with `login.py`'s
    own timeout.

    Returns the dialog's own `window_id`, captured from the `window_opened`
    event this function waits for, so every subsequent action against this
    dialog (`apply_settings_from_file`, `close_settings_dialog`) can scope
    to this one window instead of searching every open window. `None` only
    if the recognised event's own title somehow lacked
    `dialog_title_marker` (never happens in practice, since the wait
    predicate already requires it).

    The subscription opens before the menu is touched, so the dialog's
    `window_opened` cannot be missed."""
    await _await_menu_ready(dispatcher, labels, timeout=timeout)
    menu_path = (
        labels.gateway_menu_path if program == Program.GATEWAY else labels.tws_menu_path
    )
    with dispatcher.subscribe() as sub:
        await navigate_menu(
            dispatcher, menu_path, timeout=timeout, window_id=main_window_id
        )
        event = await sub.wait(
            "window_opened",
            lambda e: labels.dialog_title_marker in (e.window.title or ""),
            timeout=timeout,
        )
    return event.window.window_id


async def close_settings_dialog(
    dispatcher: Dispatcher,
    labels: SettingsLabels,
    *,
    window_id: str | None = None,
) -> None:
    """Commits every change made since the dialog was opened and closes it,
    via a click on `labels.ok_button` (`"OK"`, not `"Apply"`, which commits
    without closing).

    `window_id`, if given (normally `open_settings_dialog`'s own return
    value), scopes the click to this one dialog instead of an unscoped
    search that could land on a different, simultaneously-open window's own
    same-labeled button."""
    await click(dispatcher, labels.ok_button, window_id=window_id)


async def _apply_time(
    dispatcher: Dispatcher,
    labels: Labels,
    entry: SettingEntry,
    value: str,
    window_id: str | None,
) -> None:
    """Writes an `auto_restart_time`/`auto_logoff_time` value: the time field,
    the AM/PM radio and the enable radio. Unlike every other action these
    already know all their controls (via `labels.settings.controls`), so the
    entry names none."""
    time_str, ampm = _parse_time_ampm(value, setting_name=entry.name)
    restart_time_label = _resolve_control(labels.settings, "auto_restart_time_label")
    logoff_time_label = _resolve_control(labels.settings, "auto_logoff_time_label")
    ampm_label = _resolve_control(
        labels.settings, "am_radio" if ampm == "AM" else "pm_radio"
    )
    is_restart = entry.action == "auto_restart_time"
    enable_label = _resolve_control(
        labels.settings, "auto_restart_radio" if is_restart else "auto_logoff_radio"
    )
    # One time field, labelled by the selected radio -- try the action's own
    # label first, then the other.
    first_label, second_label = (
        (restart_time_label, logoff_time_label)
        if is_restart
        else (logoff_time_label, restart_time_label)
    )
    try:
        await type_text_near_label(
            dispatcher, first_label, entry.field_index, time_str, window_id=window_id
        )
    except ElementNotFoundError:
        await type_text_near_label(
            dispatcher, second_label, entry.field_index, time_str, window_id=window_id
        )
    await toggle(dispatcher, ampm_label, True, window_id=window_id)
    await toggle(dispatcher, enable_label, True, window_id=window_id)


async def apply_settings(
    dispatcher: Dispatcher,
    labels: Labels,
    choices: Sequence[SettingChoice],
    *,
    window_id: str | None = None,
) -> None:
    """Applies every choice, in order, against an already-open Configuration
    dialog -- callers sequence `open_settings_dialog` -> `apply_settings` ->
    `close_settings_dialog`.

    `window_id`, if given (normally `open_settings_dialog`'s own return
    value), scopes every action below to the one Configuration dialog it
    opened.

    Each entry's own `SettingsError`/`ElementNotFoundError`/`AgentError` is
    caught, logged, and skipped independently, so one bad entry costs exactly
    that entry, not the ones after it. `WindowGoneError` and `ProtocolError`
    (the dialog or the agent is gone) propagate and end the loop."""
    for choice in choices:
        entry, value = choice.entry, choice.value
        try:
            await expand_tree(dispatcher, entry.tree_path, window_id=window_id)
            logger.info(
                "IBController > settings: applying %s = %r (%s)",
                entry.name,
                value,
                choice.source,
            )
            if entry.action in _TIME_ACTIONS:
                await _apply_time(dispatcher, labels, entry, str(value), window_id)
                continue
            label = _resolve_label(entry, labels.settings)
            if entry.action == "toggle":
                await toggle(dispatcher, label, bool(value), window_id=window_id)
            elif entry.action == "type_text":
                await type_text(dispatcher, label, str(value), window_id=window_id)
            else:  # type_text_near_label
                await type_text_near_label(
                    dispatcher,
                    label,
                    entry.field_index,
                    str(value),
                    window_id=window_id,
                )
        except (SettingsError, ElementNotFoundError, AgentError) as exc:
            logger.error(
                "IBController > settings: %s failed -- skipping: %s", entry.name, exc
            )


async def apply_declarative_settings(
    dispatcher: Dispatcher,
    labels: Labels,
    config: Config,
    session: SessionState,
    choices: Sequence[SettingChoice],
    *,
    after_apply: Callable[[str | None], Awaitable[None]] | None = None,
) -> None:
    """Applies `choices` (see `resolve_settings`) in the Global Configuration
    dialog.

    `session.main_window_id` scopes `open_settings_dialog`'s menu navigation
    to the already-validated main window instead of a global guess.

    `window_id` (from `open_settings_dialog`) scopes every subsequent action
    to the dialog that opened, and `close_settings_dialog` always runs in
    `finally`. `after_apply`, if given, is awaited with that `window_id`
    while the dialog is still open, after the choices are applied.

    The "OK" click always runs, committing the entries applied so far, except
    when the dialog or the agent is gone (`WindowGoneError`/`ProtocolError`),
    which is re-raised without it.

    Returns without opening the dialog when there are no choices and no
    `after_apply` hook."""
    if after_apply is None and not choices:
        logger.info("IBController > settings: no settings to apply")
        return
    # open_settings_dialog -> apply_settings -> close_settings_dialog
    window_id = await open_settings_dialog(
        dispatcher,
        labels.settings,
        program=config.program,
        timeout=config.mfa_timeout,
        main_window_id=session.main_window_id,
    )
    dialog_gone = False
    try:
        await apply_settings(dispatcher, labels, choices, window_id=window_id)
        if after_apply is not None:
            await after_apply(window_id)
    except (WindowGoneError, ProtocolError):
        dialog_gone = True  # nothing left to click "OK" on
        raise
    finally:
        if not dialog_gone:
            await close_settings_dialog(
                dispatcher, labels.settings, window_id=window_id
            )
    logger.info("IBController > declarative settings applied (%d)", len(choices))
