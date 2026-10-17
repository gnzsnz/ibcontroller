"""settings.py -- L5's Settings domain, alongside `login.py` (Login) and
`management.py` (Management).

Drives IBKR's Global Configuration dialog: open it, navigate to a section,
set a value, repeat for every declared setting, then click "OK" once to
commit and close. A plain sequence, not a state machine -- built on the same
`actions.py` primitives (`navigate_menu`, `expand_tree`, `toggle`, `type_text`,
`type_text_near_label`, `click`) every other domain uses.

Settings are declared in TOML, not code (`SettingEntry`/`SettingsFile`, loaded
via `load_settings_file`/`load_builtin_settings_file`). Two tiers:

- **Built-in** (`data/builtin_settings.toml`, always loaded): `read_only_api`
  and `auto_restart_time`, the load-bearing settings this project's own
  "Scope, deliberately" section calls out. Sourced from `Config` fields, so
  they apply from `ibcontroller.toml` alone, no extra file needed.
- **User** (`ibkr_settings.toml`, via `Config.settings_file`, opt-in): anything
  else a deployment wants to set, e.g. the `api_precautions_*` controls.

`merge_settings_files` combines them: a user entry whose `value_from_config`
matches a built-in entry replaces it (e.g. to correct a control that moved in
a newer TWS/Gateway release); everything else is unioned in, built-ins first.

Each `SettingEntry` names its target via `label` (a literal widget label) or
`label_ref` (resolved through `labels.settings.controls`, version-proofed),
and its value via a literal `value` or `value_from_config` (an attribute
looked up on `Config` at apply time). A value of `None`, from either source,
means "leave this setting alone" -- the deliberate three-state (set/clear/
leave-unchanged) enable/disable mechanism, matching IBC's own `config.ini`
semantics for yes/no settings. `_SETTINGS_ELIGIBLE_FIELDS` is the closed set
of `Config` fields a `value_from_config` may name; adding a new built-in
setting means extending this set and adding the `Config` field together.

Five actions are supported: `toggle`, `type_text`, `type_text_near_label`,
`auto_restart_time`, and `auto_logoff_time` (each of the last two, one config
string driving three underlying writes: the time field, the AM/PM radio, and
the "Auto restart"/"Auto logoff" radio -- see `_parse_time_ampm`; both share
the same Lock and Exit radio-button pair, so `builtin_settings.toml` applies
`auto_logoff_time` before `auto_restart_time` so that, if both are set, the
last-applied wins the shared radio group, matching IBC's own stated
precedence). Button-press entries with no value (e.g. IBC's "Reset API order
ID sequence") and dropdown (`JComboBox`) controls aren't supported yet --
nothing declared so far needs them.

`apply_settings_from_file` applies at instance startup only, before or
around Login (not from the future control API/L9, which is a separate
runtime trigger into the same `SettingEntry` mechanism). Each entry's
failure is caught and logged independently, so one bad entry doesn't abort
the rest of the file."""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Awaitable, Callable
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

import attrs
import cattrs
from anyio import Path as AsyncPath
from anyio.to_thread import run_sync

from ibcontroller.actions import (
    click,
    expand_tree,
    navigate_menu,
    toggle,
    type_text,
    type_text_near_label,
    wait_for_window,
)
from ibcontroller.agent_client import ElementNotFoundError
from ibcontroller.config import Config, Program
from ibcontroller.dispatch import Dispatcher
from ibcontroller.errors import IBControllerError
from ibcontroller.labels import Labels, SettingsLabels

if TYPE_CHECKING:
    from ibcontroller.login import SessionState

logger = logging.getLogger(__name__)

converter = cattrs.Converter()
_NOON_HOUR = 12  # datetime.hour is 0-23; below this is AM, at/above is PM
# Single source of truth for which Config fields a "value_from_config" may
# name -- both _resolve_value's allow-list and load_settings_file's eager
# warning read this same set. hasattr(config, ...) alone would let an entry
# reach any Config attribute, including Secret-wrapped credentials
# (userid/password). Extend this set (and config.py) when a new built-in
# setting is added.
_SETTINGS_ELIGIBLE_FIELDS = frozenset(
    {"read_only_api", "auto_restart_time", "auto_logoff_time"}
)
# SettingEntry.value (bool | str | None) has no discriminator cattrs can use
# on its own -- but tomllib.load() already parses TOML's own real
# bool/string/absent types into native Python values before cattrs ever sees
# them, so a plain passthrough is the correct hook.
converter.register_structure_hook(bool | str | None, lambda v, _: v)


class SettingsError(IBControllerError):
    """Raised for conditions specific to this module's own sequencing and
    entry resolution: the Global Configuration dialog never appeared, a
    wanted section/setting couldn't be found, or a `SettingEntry` is
    malformed (unknown `action`, neither/both of `label`/`label_ref` given,
    an unresolvable `label_ref`/`value_from_config`). `actions.py`'s own
    typed exceptions (`TimeoutError`, `ElementNotFoundError`) propagate
    unchanged where relevant."""


@attrs.define(frozen=True)
class SettingEntry:
    """One row of `ibkr_settings.toml` -- see the module docstring for the
    `label`/`label_ref`/`value`/`value_from_config` resolution rules."""

    tree_path: str
    action: str
    label: str | None = None
    label_ref: str | None = None
    value: bool | str | None = None
    value_from_config: str | None = None
    field_index: int = 0
    """Used only by `"type_text_near_label"` entries, to find a field with
    no accessible name of its own by position within the container below
    the matched label. Ignored by every other action."""


@attrs.define(frozen=True)
class SettingsFile:
    settings: list[SettingEntry] = attrs.field(factory=list)


async def load_settings_file(path: str | Path) -> SettingsFile:
    """Loads `path` as TOML into a `SettingsFile`. Used for a user's own
    `ibkr_settings.toml` (`Config.settings_file`, `None` unless a caller
    explicitly points at a real file) -- see `load_builtin_settings_file`
    for the always-loaded built-in counterpart.

    Warns (without raising, and without dropping the entry) for any
    `value_from_config` naming a field outside `_SETTINGS_ELIGIBLE_FIELDS`,
    since such an entry can never resolve to a real setting and will always
    be skipped at apply time.

    Read via `anyio.Path`, off the event loop -- called once per launch/
    restart from `apply_declarative_settings`, concurrently
    with the Dispatcher's own event/command tasks while the Configuration
    dialog is open on the live Gateway.

    Raises `SettingsError` (never a raw `OSError`/`tomllib.TOMLDecodeError`/
    cattrs error) for a missing/unreadable file, invalid TOML, or a
    structure that doesn't match `SettingsFile` -- `control_loop` catches
    this specifically to fall back to built-in-only settings rather than
    losing the whole settings step to one bad user file."""
    try:
        raw = await AsyncPath(Path(path).expanduser()).read_bytes()
    except OSError as exc:
        raise SettingsError(f"could not read settings file {path}: {exc}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"settings file {path} is not valid TOML: {exc}") from exc
    try:
        settings_file = converter.structure(data, SettingsFile)
    except cattrs.errors.ClassValidationError as exc:
        raise SettingsError(
            f"settings file {path} has an invalid structure: {exc}"
        ) from exc
    for entry in settings_file.settings:
        if (
            entry.value_from_config is not None
            and entry.value_from_config not in _SETTINGS_ELIGIBLE_FIELDS
        ):
            logger.warning(
                "IBController > settings: %s entry %s (%s) has "
                "value_from_config=%r, which is not a real ibcontroller.toml "
                "field -- this entry will never apply. Use a literal 'value' "
                "instead, or check the README's Settings table.",
                path,
                entry.tree_path,
                entry.label or entry.label_ref,
                entry.value_from_config,
            )
    return settings_file


async def load_builtin_settings_file() -> SettingsFile:
    """Loads the bundled `data/builtin_settings.toml` -- `read_only_api`/
    `auto_restart_time`, always parsed and always applied regardless of
    whether `Config.settings_file` is set.

    `resources.files(...)` returns an `importlib.resources` `Traversable`,
    not necessarily a real `pathlib.Path` (e.g. inside a zipped wheel), so
    this can't use `anyio.Path` directly -- `anyio.to_thread.run_sync` wraps
    the whole `read_bytes()` call off the event loop instead, same reasoning
    as `launcher.build_launch_plan`'s own wrap."""
    resource = resources.files("ibcontroller").joinpath("data", "builtin_settings.toml")
    raw = await run_sync(resource.read_bytes)
    data = tomllib.loads(raw.decode("utf-8"))
    return converter.structure(data, SettingsFile)


def merge_settings_files(
    builtin: SettingsFile, user: SettingsFile | None
) -> list[SettingEntry]:
    """Combines `builtin`'s always-on entries with `user`'s optional
    entries. A user entry whose `value_from_config` matches a built-in
    entry's *replaces* it (the override case); every other entry (an
    un-overridden built-in, or any other user entry, including one with no
    `value_from_config` set at all) is unioned in, built-ins first in their
    own order, then the user's remaining entries in theirs."""
    user_entries = user.settings if user is not None else []
    user_by_key = {
        entry.value_from_config: entry
        for entry in user_entries
        if entry.value_from_config is not None
    }
    merged: list[SettingEntry] = []
    overridden_keys: set[str] = set()
    for entry in builtin.settings:
        key = entry.value_from_config
        if key is not None and key in user_by_key:
            merged.append(user_by_key[key])
            overridden_keys.add(key)
        else:
            merged.append(entry)
    for entry in user_entries:
        if entry.value_from_config in overridden_keys:
            continue  # already merged in above, in the built-in's own slot
        merged.append(entry)
    return merged


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


def _resolve_value(entry: SettingEntry, config: Config) -> bool | str | None:
    if entry.value_from_config is not None:
        if entry.value_from_config not in _SETTINGS_ELIGIBLE_FIELDS:
            raise SettingsError(
                f"Config has no field named {entry.value_from_config!r}"
            )
        return getattr(config, entry.value_from_config)
    return entry.value


def _any_value_to_apply(entries: list[SettingEntry], config: Config) -> bool:
    """True if any entry resolves to a value. An entry whose value cannot be
    resolved counts as one, so the per-entry error handling reports it."""
    for entry in entries:
        try:
            if _resolve_value(entry, config) is not None:
                return True
        except SettingsError:
            return True
    return False


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


async def apply_settings_from_file(  # noqa: PLR0912 -- per-entry try/except (below)
    dispatcher: Dispatcher,
    labels: Labels,
    config: Config,
    settings_file: SettingsFile,
    *,
    window_id: str | None = None,
) -> None:
    """Applies every entry in `settings_file`, in order, against an already-
    open Configuration dialog -- callers sequence `open_settings_dialog` ->
    `apply_settings_from_file` -> `close_settings_dialog`. An entry whose
    resolved value is `None` (either a literal `value = nothing` or a
    `value_from_config` field that's currently `None`) is skipped entirely
    -- leave-unchanged.

    `window_id`, if given (normally `open_settings_dialog`'s own return
    value), scopes every action below to the one Configuration dialog it
    opened.

    Each entry's own `SettingsError`/`ElementNotFoundError` is caught,
    logged, and skipped independently, so one bad entry costs exactly that
    entry, not the ones after it."""
    for entry in settings_file.settings:
        try:
            value = _resolve_value(entry, config)
            if value is None:
                logger.info(
                    "IBController > settings: skipping %s (%s) -- value "
                    "resolved to None",
                    entry.tree_path,
                    entry.label or entry.label_ref,
                )
                continue
            await expand_tree(dispatcher, entry.tree_path, window_id=window_id)
            if entry.action in ("auto_restart_time", "auto_logoff_time"):
                # No `label`/`label_ref` to resolve -- unlike every other action,
                # these already know all three underlying controls (the time
                # field, the AM/PM radio pair, the enable radio) via
                # labels.settings.controls, so there's nothing for the entry
                # itself to name.
                if not isinstance(value, str):
                    raise SettingsError(
                        f"'{entry.action}' entries need a string value, got {value!r}"
                    )
                time_str, ampm = _parse_time_ampm(value, setting_name=entry.action)
                logger.info(
                    "IBController > settings: applying %s (%s) = %s %s",
                    entry.tree_path,
                    entry.action,
                    time_str,
                    ampm,
                )
                restart_time_label = _resolve_control(
                    labels.settings, "auto_restart_time_label"
                )
                logoff_time_label = _resolve_control(
                    labels.settings, "auto_logoff_time_label"
                )
                ampm_label = _resolve_control(
                    labels.settings, "am_radio" if ampm == "AM" else "pm_radio"
                )
                enable_control = (
                    "auto_restart_radio"
                    if entry.action == "auto_restart_time"
                    else "auto_logoff_radio"
                )
                enable_label = _resolve_control(labels.settings, enable_control)
                # One time field, labelled by the selected radio -- try the
                # action's own label first, then the other.
                first_label, second_label = (
                    (restart_time_label, logoff_time_label)
                    if entry.action == "auto_restart_time"
                    else (logoff_time_label, restart_time_label)
                )
                try:
                    await type_text_near_label(
                        dispatcher,
                        first_label,
                        entry.field_index,
                        time_str,
                        window_id=window_id,
                    )
                except ElementNotFoundError:
                    await type_text_near_label(
                        dispatcher,
                        second_label,
                        entry.field_index,
                        time_str,
                        window_id=window_id,
                    )
                await toggle(dispatcher, ampm_label, True, window_id=window_id)
                await toggle(dispatcher, enable_label, True, window_id=window_id)
                continue
            label = _resolve_label(entry, labels.settings)
            logger.info(
                "IBController > settings: applying %s (%s) %s = %r",
                entry.tree_path,
                label,
                entry.action,
                value,
            )
            if entry.action == "toggle":
                if not isinstance(value, bool):
                    raise SettingsError(
                        f"'toggle' entries need a bool value, got {value!r}"
                    )
                await toggle(dispatcher, label, value, window_id=window_id)
            elif entry.action == "type_text":
                if not isinstance(value, str):
                    raise SettingsError(
                        f"'type_text' entries need a string value, got {value!r}"
                    )
                await type_text(dispatcher, label, value, window_id=window_id)
            elif entry.action == "type_text_near_label":
                if not isinstance(value, str):
                    raise SettingsError(
                        "'type_text_near_label' entries need a string "
                        f"value, got {value!r}"
                    )
                await type_text_near_label(
                    dispatcher,
                    label,
                    entry.field_index,
                    value,
                    window_id=window_id,
                )
            else:
                raise SettingsError(
                    f"unsupported settings action {entry.action!r} -- only "
                    "'toggle'/'type_text'/'type_text_near_label'/"
                    "'auto_restart_time'/'auto_logoff_time' are supported"
                )
        except (SettingsError, ElementNotFoundError) as exc:
            logger.error(
                "IBController > settings: entry %s (%s) failed -- skipping: %s",
                entry.tree_path,
                entry.label or entry.label_ref,
                exc,
            )
            continue


async def apply_declarative_settings(
    dispatcher: Dispatcher,
    labels: Labels,
    config: Config,
    session: SessionState,
    *,
    after_apply: Callable[[str | None], Awaitable[None]] | None = None,
) -> None:
    """Applies the bundled built-in entries (`read_only_api`/
    `auto_restart_time`, from `Config` alone -- see "Two tiers, not one"
    above), merged with a user's own `Config.settings_file` when set.

    `session.main_window_id` scopes `open_settings_dialog`'s menu navigation
    to the already-validated main window instead of a global guess.

    `window_id` (from `open_settings_dialog`) scopes every subsequent action
    to the dialog that opened, and `close_settings_dialog` always runs in
    `finally`. `after_apply`, if given, is awaited with that `window_id`
    while the dialog is still open, after the entries are applied.

    A `config.settings_file` that fails to load (`SettingsError`) is logged
    and treated as absent, so a typo'd path cannot take the load-bearing
    built-in entries down with it.

    Returns without opening the dialog when no entry resolves to a value and
    there is no `after_apply` hook."""
    builtin_settings = await load_builtin_settings_file()
    user_settings = None
    if config.settings_file is not None:
        try:
            user_settings = await load_settings_file(config.settings_file)
        except SettingsError as exc:
            logger.error(
                "IBController > settings: could not load %s -- continuing "
                "with built-in settings only: %s",
                config.settings_file,
                exc,
            )
    merged = merge_settings_files(builtin_settings, user_settings)
    if after_apply is None and not _any_value_to_apply(merged, config):
        logger.info("IBController > settings: no settings to apply")
        return
    # open_settings_dialog -> apply_settings_from_file -> close_settings_dialog
    window_id = await open_settings_dialog(
        dispatcher,
        labels.settings,
        program=config.program,
        timeout=config.mfa_timeout,
        main_window_id=session.main_window_id,
    )
    try:
        await apply_settings_from_file(
            dispatcher,
            labels,
            config,
            SettingsFile(settings=merged),
            window_id=window_id,
        )
        if after_apply is not None:
            await after_apply(window_id)
    finally:
        await close_settings_dialog(dispatcher, labels.settings, window_id=window_id)
    if user_settings is not None:
        logger.info(
            "IBController > declarative settings applied (built-in + %s)",
            config.settings_file,
        )
    else:
        logger.info("IBController > declarative settings applied (built-in only)")
