"""Window/button labels the built-in recognisers (`recognisers.py`, L5) match
against -- externalized to JSON rather than hardcoded. If IBKR renames a
button in a future release, a user patches this file instead of waiting for a
new ibcontroller release or forking Python code.

A bundled default (`data/labels.json`, shipped with the package so
ibcontroller works out of the box) is layered under an optional
`labels.json` in `app_dirs.resolve_app_dirs()`'s `config_dir` -- merged one
field at a time (`{"login": {"gateway_titles": [...]}}` overrides just that
one field, leaving every other label and domain untouched), not a full
replacement. `dismiss_rules` (below) is a list, not a nested object, so an
override replaces the whole list, not one entry within it -- a user
extending it copies the bundled entries into their own override file rather
than appending.

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
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import attrs
import cattrs

converter = cattrs.Converter()


@attrs.define(frozen=True)
class LoginLabels:
    gateway_titles: list[str]
    tws_titles: list[str]
    login_buttons: list[str]
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
class SecondFactorAuthLabels:
    title: str


@attrs.define(frozen=True)
class ExistingSessionLabels:
    title: str
    continue_buttons: list[str]
    cancel_buttons: list[str]


@attrs.define(frozen=True)
class AcceptIncomingConnectionLabels:
    """`accept_buttons` tries "OK" then "Yes" in order; `reject_buttons` is
    "No"."""

    title: str
    accept_buttons: list[str]
    reject_buttons: list[str]


@attrs.define(frozen=True)
class LoginFailedLabels:
    title: str
    dismiss_button: str


class LabelsError(Exception):
    """A malformed entry in `labels.json` (bundled or override) -- e.g. a
    `DismissRule` with neither `match_title` nor `match_text` set."""


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
    `Configure` menu at all; `tws_menu_path` uses the Mosaic-layout path
    (`File/Global Configuration...`) since that's TWS's modern default. A
    Classic-layout path (`Edit/Global Configuration...`) exists but isn't
    supported here -- `navigate_menu` takes one path, not a fallback list.

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
    explicitly."""

    gateway_menu_path: str
    tws_menu_path: str
    dialog_title_marker: str
    apply_button: str
    cancel_button: str
    ok_button: str
    splash_title_marker: str
    controls: dict[str, str]


@attrs.define(frozen=True)
class ShutdownLabels:
    """The graceful shutdown menu item: Gateway's `File > Close` or TWS's
    `File > Exit`. See `launcher.clean_shutdown`."""

    gateway_menu_path: str
    tws_menu_path: str


@attrs.define(frozen=True)
class Labels:
    login: LoginLabels
    second_factor_auth: SecondFactorAuthLabels
    existing_session: ExistingSessionLabels
    accept_incoming_connection: AcceptIncomingConnectionLabels
    login_failed: LoginFailedLabels
    too_many_failed_login_attempts: TooManyFailedLoginAttemptsLabels
    settings: SettingsLabels
    shutdown: ShutdownLabels
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


def _load_bundled_default() -> dict[str, Any]:
    raw = (
        resources.files("ibcontroller")
        .joinpath("data", "labels.json")
        .read_text(encoding="utf-8")
    )
    return json.loads(raw)


def load_labels(config_dir: str | Path | None = None) -> Labels:
    """Loads the bundled default, then merges an optional `{config_dir}/labels.json`
    override on top if one exists. `config_dir` is typically
    `app_dirs.resolve_app_dirs()`'s first element -- passed explicitly rather than
    resolved here, matching
    `config.py`'s own convention of callers resolving app dirs once and threading the
    result through."""
    data = _load_bundled_default()
    if config_dir is not None:
        override_path = Path(config_dir) / "labels.json"
        if override_path.is_file():
            override_data = json.loads(override_path.read_text(encoding="utf-8"))
            data = _deep_merge(data, override_data)
    return converter.structure(data, Labels)
