"""Window/button labels the built-in recognisers (`recognisers.py`, L5) match
against -- externalized to JSON rather than hardcoded. If IBKR renames a
button in a future release, a user patches this file instead of waiting for a
new ibcontroller release or forking Python code.

A bundled default (`data/labels.json`, shipped with the package so
ibcontroller works out of the box) is layered under an optional
`labels.json` in `app_dirs.resolve_app_dirs()`'s `config_dir` -- merged one
field at a time (`{"login": {"gateway_titles": [...]}}` overrides just that
one field, leaving every other label and domain untouched), not a full
replacement. `dismiss_rules` (below) is a list, merged by rule `name`: an
override entry with a bundled rule's name replaces that rule, an entry with a
new name is appended, and bundled rules the override doesn't name are kept.
Removing a bundled rule is not supported.

`dismiss_rules` is a real exception to "this file is just labels, not config
rules": each entry is a full declarative `Recognizer` (see `DismissRule`'s
own docstring), not just a name-to-text mapping the way every other group
here is. It stays here to reuse this module's bundled-default-plus-override
loader, rather than a second loader elsewhere. A *config rule* in the wider
sense (arbitrary match -> arbitrary `ACTIONS` sequence, user-authored,
per-deployment) is still a different, larger thing than a `DismissRule`
(fixed shape: one match, one click)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import attrs
import cattrs

from ibcontroller.config import ConfigError

logger = logging.getLogger(__name__)

converter = cattrs.Converter()


class LabelsError(ConfigError):
    """A malformed `labels.json` (bundled or override) -- invalid JSON, a wrong
    type, an empty candidate list, or a `DismissRule` with neither
    `match_title` nor `match_text` set. A `ConfigError`, so `cli.py` exits 78."""


def _structure_str(value: Any, _type: Any) -> str:
    if not isinstance(value, str):
        raise LabelsError(f"expected a string, got {type(value).__name__}")
    return value


def _structure_str_list(value: Any, _type: Any) -> list[str]:
    # A bare string is iterable: cattrs would split it into characters.
    if not isinstance(value, list):
        raise LabelsError(f"expected a list of strings, got {type(value).__name__}")
    return [_structure_str(item, str) for item in value]


converter.register_structure_hook(str, _structure_str)
converter.register_structure_hook_func(lambda t: t == list[str], _structure_str_list)


def _non_empty(instance: Any, attribute: attrs.Attribute[list[str]], value: list[str]):
    """Validator for candidate lists: an empty one can never match or click."""
    if not value:
        raise LabelsError(
            f"{type(instance).__name__}.{attribute.name} must not be empty"
        )


@attrs.define(frozen=True)
class LoginLabels:
    gateway_titles: list[str] = attrs.field(validator=_non_empty)
    tws_titles: list[str] = attrs.field(validator=_non_empty)
    login_buttons: list[str] = attrs.field(validator=_non_empty)
    api_type_ib_api: str
    api_type_fix: str
    trading_mode_live: str
    trading_mode_paper: str
    username_field: str
    password_field: str
    starting_application_title: str
    """Gateway only. The splash/"Authenticating..." frame's *final* title, read
    at `WINDOW_CLOSED` time, is the Gateway login-completion signal --
    the main window opening is not a valid signal, since it renders fully
    before authentication (including 2FA) actually finishes. See
    `login.py`'s `_wait_for_outcome_gateway`."""
    tws_main_window_menu_item: str
    """TWS only. A `File > Lock Application` menu item existing on a
    newly-opened top-level window is the TWS login-completion signal -- TWS's
    real main window title is account-specific, so a title match can't be
    used here the way it can for Gateway. See `login.py`'s
    `_wait_for_outcome_tws`."""


@attrs.define(frozen=True)
class MfaLabels:
    title: str


@attrs.define(frozen=True)
class ExistingSessionLabels:
    title: str
    continue_buttons: list[str] = attrs.field(validator=_non_empty)
    cancel_buttons: list[str] = attrs.field(validator=_non_empty)


@attrs.define(frozen=True)
class ReloginLabels:
    """The "Re-login is required" dialog, shown after the other session took
    over. `relogin_buttons` reconnects, `cancel_buttons` ends this session."""

    title: str
    relogin_buttons: list[str] = attrs.field(validator=_non_empty)
    cancel_buttons: list[str] = attrs.field(validator=_non_empty)


@attrs.define(frozen=True)
class TradingLoginHandoffLabels:
    """The "Trading Login Handoff" dialog (accounts with 2FA), from IBC; not
    yet seen live. `disconnect_buttons` ends the other session."""

    title: str
    disconnect_buttons: list[str] = attrs.field(validator=_non_empty)


@attrs.define(frozen=True)
class AcceptIncomingConnectionLabels:
    """`accept_buttons` tries "OK" then "Yes" in order; `reject_buttons` is
    "No"."""

    title: str
    accept_buttons: list[str] = attrs.field(validator=_non_empty)
    reject_buttons: list[str] = attrs.field(validator=_non_empty)


@attrs.define(frozen=True)
class LoginFailedLabels:
    """The "Login failed" dialog -- despite the wording, IB reuses this
    title for a "server disconnected" condition, not rejected credentials
    (IBC's own commit history, gitea #62). See
    `recognisers.LoginFailedRecognizer`."""

    title: str
    dismiss_button: str


@attrs.define(frozen=True)
class LoginErrorLabels:
    """The "Login Error" dialog (ported from IBC's `LoginErrorDialogHandler`)
    -- a transient server condition, never a credential rejection. See
    `recognisers.LoginErrorRecognizer`."""

    title: str
    dismiss_button: str


@attrs.define(frozen=True)
class UnrecognizedCredentialsLabels:
    """The "Unrecognized Username or Password" dialog -- a real rejected
    login, distinct from `LoginFailedLabels`'s dialog despite the similar
    name. See `recognisers.UnrecognizedCredentialsRecognizer`."""

    title: str
    dismiss_button: str


@attrs.define(frozen=True)
class GatewayConnectionFailedLabels:
    """The generic `JDialog` titled "Gateway" IBKR reuses for several
    unrelated purposes (ported from IBC's `GatewayDialogHandler`); only the
    ones whose text starts with `message_prefix` are a login outcome.
    `known_reasons` lists the reason substrings (checked against the
    HTML-stripped text) that mean a credential/account problem -- anything
    else is transient (e.g. a stale restart token). See
    `recognisers.GatewayConnectionFailedRecognizer`."""

    title: str
    message_prefix: str
    known_reasons: list[str]
    dismiss_button: str


@attrs.define(frozen=True)
class DismissRule:
    """One declarative "just dismiss this" recogniser: a dialog that's always
    recognised the same simple way (title-contains and/or text-contains) and
    always dismissed the same simple way (click one button).

    `recognisers.DeclarativeDismissRecognizer` implements the `Recognizer`
    Protocol generically for every rule here.

    At least one of `match_title`/`match_text` is required (checked in
    `__attrs_post_init__`, so a malformed rule fails at `load_labels()` time,
    not silently at first dispatch). If both are given, **both** must match
    (AND, not OR), to avoid over-matching an unrelated dialog that happens to
    share only one of the two conditions. `match_text` checks every
    component's `text` *and* `accessible_name`, since which field a label's
    text lands in depends on the real Swing component type -- `JLabel` text
    only ever surfaces via `accessible_name`."""

    name: str
    click: str
    match_title: str | None = None
    match_text: str | None = None

    def __attrs_post_init__(self) -> None:
        if self.match_title is None and self.match_text is None:
            raise LabelsError(
                f"dismiss_rules entry {self.name!r} needs at least one of "
                "'match_title'/'match_text'"
            )


@attrs.define(frozen=True)
class TooManyFailedLoginAttemptsLabels:
    """`message_prefix` matches the start of the dialog text ("Too many
    failed login attempts. Please wait N minute(s) & M second(s) before
    attempting to re-login again."), not the window title."""

    message_prefix: str
    dismiss_button: str


@attrs.define(frozen=True)
class SettingsLabels:
    """Labels for the Global Configuration dialog (see `settings.py`).

    `gateway_menu_path` is Gateway's `Configure/Settings`. TWS has no
    `Configure` menu at all; `open_settings_dialog` uses `tws_menu_path`
    (`File/Global Configuration...`, Mosaic layout) -- live-confirmed
    2026-09-16 as the real path, after `tws_menu_path_classic`
    (`Edit/Global Configuration...`) produced a clean `ElementNotFoundError`
    every run (Mosaic has no `Edit/Global Configuration...` item; earlier
    mistaken for #40's wrong-frame race). `tws_menu_path_classic` is defined
    but unused -- a real Classic/Mosaic fallback matching IBC's own
    `GetConfigDialogTask` order is tracked separately as #39.

    `controls` is a flat name -> widget-label map, deliberately decoupled
    from tree paths (which stay literal on each `settings.SettingEntry` in
    `ibkr_settings.toml`): this dict exists to version-proof against IBKR
    renaming a *label*, not to describe a setting's navigation.

    `api_precautions_*` covers the API/Precautions panel. The control set is
    account/permission dependent (e.g. market-data entitlements), so a given
    deployment may not have every entry here, and a future account could
    expose controls not listed here.

    `splash_title_marker` matches the splash/"Starting application..." frame
    that must close before `Configure/Settings` is safely reachable --
    opening the menu too early can silently fail to open the dialog.
    `settings.py`'s `open_settings_dialog` waits for this signal first.

    `ok_button` is what `close_settings_dialog` clicks to commit and close
    the dialog -- `"OK"` has Swing's standard apply-and-close semantics,
    unlike `"Apply"`, which commits without closing. `apply_button`/
    `cancel_button` are kept for callers that need those semantics
    explicitly.

    `sweep_paths` are the tree nodes `diagnostics.sweep_settings_tree` probes;
    a node absent from the running build is reported, not an error."""

    gateway_menu_path: str
    tws_menu_path: str
    tws_menu_path_classic: str
    dialog_title_marker: str
    apply_button: str
    cancel_button: str
    ok_button: str
    splash_title_marker: str
    controls: dict[str, str]
    sweep_paths: list[str]


@attrs.define(frozen=True)
class GenericLabels:
    """Buttons for a pop-up no specific handler recognises. See `actions.dismiss`."""

    dismiss_buttons: list[str] = attrs.field(validator=_non_empty)


@attrs.define(frozen=True)
class ShutdownLabels:
    """The graceful shutdown menu item: Gateway's `File > Close` or TWS's
    `File > Exit`. See `management.clean_shutdown`."""

    gateway_menu_path: str
    tws_menu_path: str


@attrs.define(frozen=True)
class Labels:
    login: LoginLabels
    mfa: MfaLabels
    existing_session: ExistingSessionLabels
    relogin: ReloginLabels
    trading_login_handoff: TradingLoginHandoffLabels
    accept_incoming_connection: AcceptIncomingConnectionLabels
    login_failed: LoginFailedLabels
    login_error: LoginErrorLabels
    unrecognized_credentials: UnrecognizedCredentialsLabels
    gateway_connection_failed: GatewayConnectionFailedLabels
    too_many_failed_login_attempts: TooManyFailedLoginAttemptsLabels
    settings: SettingsLabels
    shutdown: ShutdownLabels
    generic: GenericLabels
    dismiss_rules: list[DismissRule] = attrs.field(factory=list)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """`override` wins per-field, not per-domain -- a user's file only needs to name
    the one label that changed, not repeat every sibling field or every other domain
    untouched."""
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _merge_dismiss_rules(base: Any, override: Any) -> Any:
    """Merges `dismiss_rules` lists by rule `name`; a same-name override entry replaces
    the bundled one in place, new names are appended. Malformed input is returned as
    the override for the structuring step to reject."""
    if not (isinstance(base, list) and isinstance(override, list)):
        return override
    if not all(isinstance(rule, Mapping) and "name" in rule for rule in override):
        return override
    by_name = {rule["name"]: rule for rule in base}
    by_name.update((rule["name"], rule) for rule in override)
    return list(by_name.values())


def _load_bundled_default() -> dict[str, Any]:
    raw = (
        resources.files("ibcontroller")
        .joinpath("data", "labels.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(raw)


def _warn_unknown_keys(data: Mapping[str, Any], cls: type, path: str) -> None:
    """Logs each key of `data` that `cls` (an attrs class) has no field for;
    recurses into nested attrs classes and lists of them."""
    fields = attrs.fields_dict(attrs.resolve_types(cls))
    for key, value in data.items():
        dotted = f"{path}.{key}" if path else key
        field = fields.get(key)
        if field is None:
            logger.warning("labels.json: unknown key %r ignored", dotted)
            continue
        field_type = field.type
        if field_type is None:
            continue
        if attrs.has(field_type) and isinstance(value, Mapping):
            _warn_unknown_keys(value, field_type, dotted)
        elif getattr(field_type, "__origin__", None) is list and isinstance(
            value, list
        ):
            (item_type,) = getattr(field_type, "__args__")  # noqa: B009
            if attrs.has(item_type):
                for index, item in enumerate(value):
                    if isinstance(item, Mapping):
                        _warn_unknown_keys(item, item_type, f"{dotted}[{index}]")


def _describe(exception: BaseException, _type: type | None) -> str:
    return str(exception) if isinstance(exception, LabelsError) else repr(exception)


def load_labels(config_dir: str | Path | None = None) -> Labels:
    """Loads the bundled default, then merges an optional `{config_dir}/labels.json`
    override on top if one exists. `config_dir` is typically
    `app_dirs.resolve_app_dirs()`'s first element -- passed explicitly rather than
    resolved here, matching
    `config.py`'s own convention of callers resolving app dirs once and threading the
    result through.

    Raises `LabelsError` (a `ConfigError`) naming the file and the field for invalid
    JSON, a wrong type (e.g. a string where a list is expected), an empty candidate
    list or a malformed `dismiss_rules` entry. Unknown keys are logged and ignored."""
    data = _load_bundled_default()
    source = "bundled labels.json"
    if config_dir is not None:
        override_path = Path(config_dir) / "labels.json"
        if override_path.is_file():
            source = str(override_path)
            try:
                override_data = json.loads(override_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise LabelsError(f"{source}: invalid JSON: {exc}") from exc
            if not isinstance(override_data, dict):
                raise LabelsError(f"{source}: expected a JSON object at the top level")
            _warn_unknown_keys(override_data, Labels, "")
            merged = _deep_merge(data, override_data)
            if "dismiss_rules" in override_data:
                merged["dismiss_rules"] = _merge_dismiss_rules(
                    data.get("dismiss_rules"), override_data["dismiss_rules"]
                )
            data = merged
    try:
        return converter.structure(data, Labels)
    except cattrs.ClassValidationError as exc:
        details = "; ".join(cattrs.transform_error(exc, format_exception=_describe))
        raise LabelsError(f"{source}: {details}") from exc
    except LabelsError as exc:
        raise LabelsError(f"{source}: {exc}") from exc
