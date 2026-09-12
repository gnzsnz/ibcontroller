"""Window/button labels the built-in recognisers (`recognisers.py`, L5) match against --
externalized to JSON rather than hardcoded, unlike IBC (CLAUDE.md, 2026-09-05:
confirmed by a direct scan of IBC's own source that every title/label it matches is an
inline Java string literal, scattered across its handler classes, with zero
externalized resource file anywhere in its repo -- a real, confirmed gap, not a
hypothetical one). If IBKR renames a button in a future release, a user patches this
file instead of waiting for a new ibcontroller release or forking Python code.

A bundled default (`data/labels.json`, shipped with the package so ibcontroller works
out of the box) is layered under an optional `labels.json` in
`app_dirs.resolve_app_dirs()`'s `config_dir` -- merged one field at a time (`{"login":
{"gateway_titles": [...]}}` overrides just that one field, leaving every other label,
and every other domain, untouched), not a full replacement. Note `dismiss_rules`
(below) is a list, not a nested object, so an override replaces the whole list, not
one entry within it -- a user extending it copies the bundled entries into their own
override file rather than appending.

**`dismiss_rules` (2026-09-06) is a real exception to "this file is just labels, not
config rules"** -- each entry is a full declarative `Recognizer` (see `DismissRule`'s
own docstring), not just a name-to-text mapping the way every other group here is.
Still lives here rather than in a separate file: the user's own steer was to reuse
this module's existing bundled-default-plus-override loader as-is, not build a second
one. A *config rule* in the Phase 4 sense (arbitrary match -> arbitrary `ACTIONS`
sequence, user-authored, per-deployment) is still a different, larger thing than a
`DismissRule` (fixed shape: one match, one click) -- this is that tier's simplest
case arriving early, not the whole tier."""

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
    """Gateway only (2026-09-06/07) -- ported from IBC's own `SplashFrameHandler`:
    the splash/"Authenticating..." frame's *final* title, read at `WINDOW_CLOSED`
    time, is IBC's real Gateway login-completion signal (`login.py`'s
    `_wait_for_outcome_gateway`'s own docstring has the full story -- confirmed
    live that the main window opening is *not* a valid signal for Gateway, since
    it renders fully before authentication, including 2FA, actually finishes)."""
    tws_main_window_menu_item: str
    """TWS only (2026-09-09) -- ported from IBC's own `MainWindowFrameHandler.
    recogniseWindow`: a `File > Lock Application` menu item existing on a
    newly-opened top-level window is IBC's real TWS login-completion signal
    (`login.py`'s `_wait_for_outcome_tws`'s own docstring has the full story --
    TWS's real main window title is account-specific, so a title match can
    never work here the way it does for Gateway)."""


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
    """Ported from IBC's own `AcceptIncomingConnectionDialogHandler` --
    `accept_buttons` tries "OK" then "Yes" (`SwingUtils.clickButton`'s own
    fallback order), `reject_buttons` is "No"."""

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
    """One declarative "just dismiss this" recogniser
    A dialog that's always recognised the same simple way (title-contains and/or
    text-contains) and always dismissed the same simple way (click one button)

    `recognisers.DeclarativeDismissRecognizer` is the one class that implements
    the `Recognizer` Protocol generically for every rule here.

    At least one of `match_title`/`match_text` is required (checked in
    `__attrs_post_init__`, so a malformed rule fails at `load_labels()` time,
    not silently at first dispatch). If both are given, **both** must match
    (AND, not OR) -- the stricter default, since a loose OR risks over-matching
    an unrelated dialog that happens to share only one of the two conditions.
    `match_text` checks every component's `text` *and* `accessible_name` (the
    same double-check idiom the two migrated recognisers already used, since
    which field a label's text lands in depends on the real Swing component
    type -- `JLabel` text only ever surfaces via `accessible_name`, confirmed
    live, 2026-09-05)."""

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
    """Ported from IBC's `TooManyFailedLoginAttemptsDialogHandler.java` --
    `message_prefix` matches the start of the real dialog text ("Too many
    failed login attempts. Please wait N minute(s) & M second(s) before
    attempting to re-login again."), not the window title (IBC doesn't check
    the title for this dialog either)."""

    message_prefix: str
    dismiss_button: str


@attrs.define(frozen=True)
class SettingsLabels:
    """Ported from IBC's own `GetConfigDialogTask`/`GlobalConfigurationDialogHandler`
    (`settings.py`'s own docstring has the full grounding) -- `gateway_menu_path`
    is confirmed live (2026-09-05) against a real Gateway 10.50.

    **`tws_menu_path` (2026-09-09, a real live-caught gap's fix):** confirmed
    live against a real TWS 10.45 that `navigate_menu("Configure/Settings")`
    (Gateway's own path) fails with `not_found` -- TWS has no `Configure` menu
    at all. `GetConfigDialogTask.java` shows IBC's own real TWS handling tries
    **two** candidate paths depending on layout: `"Edit/Global
    Configuration..."` (Classic) or `"File/Global Configuration..."` (Mosaic).
    Only the Mosaic path is used here -- confirmed live to be the one that
    actually works on this install (modern TWS defaults to Mosaic) -- matching
    this project's own "don't guess-fix speculatively" discipline: `navigate_
    menu` takes one path, not IBC's own two-candidate fallback list, so adding
    the untested Classic-layout path now would be exactly the kind of
    unverified guess the original note above already warned against. Revisit
    if a real Classic-layout deployment is ever actually hit.

    **`controls` is a flat name -> real widget label map (2026-09-05), not nested
    per-setting objects** -- deliberately decoupled from tree paths (which stay
    literal on each `settings.SettingEntry` in `ibkr_settings.toml`, not
    indirected here): this dict exists specifically to version-proof against
    IBKR renaming a *label*, the same concern every other labels group in this
    file exists for, not to describe a setting's full navigation. `read_only_api`
    and the "Lock and Exit" controls (`auto_restart_time_label`/
    `auto_logoff_time_label`/`am_radio`/`pm_radio`/`auto_restart_radio`) are all
    confirmed live end to end (2026-09-06).

    **`api_precautions_*` -- live-checked 2026-09-06, and the real panel does
    NOT match IBC's own `ConfigureApiPrecautionsTask.java` list exactly.**
    Ported verbatim from IBC originally, on the assumption its list was
    complete -- confirmed wrong by a real live dump of a paper account's
    API/Precautions panel:
    `api_precautions_bypass_us_stocks_market_data_in_shares` is **not a real
    control on this account/Gateway 10.50 build at all** (applying it raises
    `ElementNotFoundError`) -- likely account/permission-gated (market-data
    entitlement dependent), not simply a stale label. Conversely, the real
    panel has one control IBC's list doesn't mention:
    `api_precautions_bypass_route_marketable_to_bbo` ("Bypass Route Marketable
    to BBO warning for API orders."), added here from the live text. Every
    other `api_precautions_*` entry here was confirmed present and settable
    live. Since the precaution set may be account-dependent, a future account
    could still see a different real panel than this one -- don't assume this
    list is exhaustive or universal just because it's now live-confirmed for
    one paper account.

    **`splash_title_marker` (2026-09-05, a real live bug's fix):** IBC's own
    `SessionManager.awaitReady()` blocks `GetConfigDialogTask` on a splash
    frame (title containing this marker) closing before ever touching
    Configure/Settings -- its own comment: "the main form is loaded right at
    the start, and long before the menu items become responsive: any attempt
    to access the Configure > Settings menu item (even after it has been
    enabled) results in an exception being logged by Gateway." Confirmed live:
    calling `navigate_menu("Configure/Settings")` right after reaching
    `LOGGED_IN` returned successfully (no exception) but never actually opened
    the dialog -- `settings.py`'s `open_settings_dialog` now waits for this
    exact signal first, matching `SplashFrameHandler.recogniseWindow`'s own
    case-insensitive title-contains match ("Starting application...").

    **`ok_button` (2026-09-06, a real live-caught gap's fix):** `close_settings_dialog`
    used to click `apply_button` only -- confirmed live (2026-09-05) that this commits
    changes but genuinely does *not* close the dialog, then live-caught again
    (2026-09-06, by the user's own direct observation) as a real missing step: settings
    applied, dialog left open, "full login process" not actually complete. Checked IBC's
    own source directly before guessing a fix: grepped every `ConfigureXXXTask`/
    `EnableApiTask`/`DefaultConfigDialogManager` in IBC's repo for how *any* of them
    close the Global Configuration dialog -- every single one clicks `"OK"`, and IBC's
    source contains zero references to an `"Apply"` button anywhere at all. `"OK"` is
    Swing's standard apply-and-close semantics (unlike `"Apply"`, which commits without
    closing) -- exactly the missing step. `close_settings_dialog` now clicks this
    instead of `apply_button`. `apply_button`/`cancel_button` are kept (not proven
    unnecessary, just not currently used by this module) rather than removed
    speculatively."""

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
    """Ported from IBC's own `StopTask` (`launcher.clean_shutdown`'s own
    docstring has the full grounding): the graceful shutdown menu item is
    Gateway's `File > Close` or TWS's `File > Exit`, never the same one --
    added here 2026-09-09 after an audit found this exact program-branching
    menu path hardcoded directly in `launcher.py` instead of externalized
    like every other user-facing string this project matches or clicks
    against (CLAUDE.md's own "Working method" now names this rule)."""

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
