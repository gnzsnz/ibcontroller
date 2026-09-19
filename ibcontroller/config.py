"""
ibcontroller config

Configuration settings hierarchy, precedence, and validation.
Credentials (userid/password) are deliberately environment-variable-only, never accepted
from a file. This is a security measure to avoid accidentally committing secrets to
source control, and to support Docker secrets and other secret-management patterns.
The config file (ibcontroller.toml) is intentionally flat (no [section] headers) to
simplify parsing and avoid confusion. The config file is optional; if it doesn't exist,
the defaults are used, and environment variables can override them.

The precedence is: defaults < config file < environment variables.
"""

import logging
import os
import sys
import tomllib
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

import typed_settings as ts
from dotenv import load_dotenv
from typed_settings.converters import Converter
from typed_settings.dict_utils import set_path
from typed_settings.exceptions import TsError
from typed_settings.loaders import (
    DictLoader,
    EnvLoader,
    FileLoader,
    LoadedSettings,
    LoaderMeta,
    TomlFormat,
)
from typed_settings.processors import FormatProcessor
from typed_settings.types import Secret

from ibcontroller.app_dirs import resolve_app_dirs

ENV_PREFIX = "IBCONTROLLER_"
ENV_SENSITIVE: list[str] = ["IBCONTROLLER_USERID", "IBCONTROLLER_PASSWORD"]

# Field/TOML key names that must never appear in the config file -- credentials are
# environment-variable-only (see module docstring). Mirrors config_old.py's own
# _CREDENTIAL_KEY_NAMES, updated for this module's `userid` field name.
_CREDENTIAL_KEY_NAMES = frozenset({"userid", "password", "tws_userid", "tws_password"})

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class ConfigError(Exception):
    """Bad or unsafe configuration -- an invalid value, a missing required setting.
    The one exception type `load_config` raises; wraps typed-settings' own errors
    (`TsError` subclasses) as well as the hand-written checks below (e.g. missing
    credentials) so callers (`cli.py`) only ever need to catch one type."""


def _find_credential_keys(data: Mapping[str, Any], prefix: str = "") -> list[str]:
    """Recursively searches a nested mapping for any keys that look like credentials.
    Returns the full dotted paths to those keys. Direct port of config_old.py's own
    function of the same name."""
    found = []
    for key, value in data.items():
        path = f"{prefix}{key}"
        if key in _CREDENTIAL_KEY_NAMES:
            found.append(path)
        if isinstance(value, Mapping):
            found.extend(_find_credential_keys(value, prefix=f"{path}."))
    return found


def _reject_credentials_in_file(data: Mapping[str, Any], path: Path) -> None:
    """Raises ConfigError if any credential-shaped keys are found in the parsed TOML
    data -- credentials are environment-variable-only, never accepted from a file
    (direct port of config_old.py's function of the same name)."""
    found = _find_credential_keys(data)
    if found:
        raise ConfigError(
            f"{path}: credentials must not be set in the config file "
            f"({', '.join(found)} found) -- use environment variables instead "
            "(IBCONTROLLER_USERID/IBCONTROLLER_PASSWORD, each also accepting a "
            "_FILE-suffixed variant)"
        )


class _FileBackedEnvLoader(EnvLoader):
    """Docker-secrets pattern: for each env var, `{var}_FILE` wins if set (trimmed
    file contents); otherwise falls back to the plain var, same as `EnvLoader`.
    Direct port of config_old.py's own `_env_or_file`, adapted to the `Loader` protocol
    (`__call__(settings_cls, options) -> LoadedSettings`, see loaders.py) rather than
    a plain function -- typed-settings has no `_FILE` convention of its own."""

    def __call__(self, settings_cls: object, options: object) -> LoadedSettings:
        env = os.environ
        values: dict[str, Any] = {}
        for option in options:  # type: ignore[attr-defined]
            varname = self.get_envvar(option)
            file_varname = f"{varname}_FILE"
            if file_varname in env:
                secret_path = Path(env[file_varname])
                try:
                    value = secret_path.read_text(encoding="utf-8").strip()
                except OSError as exc:
                    raise ConfigError(f"{file_varname}={secret_path}: {exc}") from exc
                set_path(values, option.path, value)  # type: ignore[attr-defined]
            elif varname in env:
                set_path(values, option.path, env[varname])  # type: ignore[attr-defined]
        return LoadedSettings(values, LoaderMeta(self))


def _log_level_converter(raw: object) -> int:
    """
    Accept the declared default (already a real `int`, e.g. `logging.INFO`) as well as a
    raw name string from the environment/file layers.
    """
    if isinstance(raw, int):
        return raw
    name = str(raw).strip().lower()
    if name not in _LOG_LEVELS:
        raise ValueError(
            f"log_level: expected one of {', '.join(sorted(_LOG_LEVELS))}, got {raw!r}"
        )
    return _LOG_LEVELS[name]


class TradingMode(StrEnum):
    LIVE = "live"
    PAPER = "paper"


class ExistingSessionAction(StrEnum):
    """ExistingSessionDetectedAction is the user's choice for what to do when IBC
    detects an existing TWS/Gateway session (see `ExistingSessionRecognizer` in
    recognisers.py). The default is MANUAL, which leaves the user to decide what to do
    in the GUI. PRIMARY and SECONDARY are the two automatic options, and
    PRIMARY_OVERRIDE is a special case that forces the primary session to close even if
    it is logged in as a different user (see the `ExistingSessionRecognizer` docstring
    for why this is a real risk).
    """

    MANUAL = "manual"
    PRIMARY = "primary"
    PRIMARY_OVERRIDE = "primaryoverride"
    SECONDARY = "secondary"


class LogSink(StrEnum):
    """Where `configure_logging`/`configure_gateway_stdout` send their output --
    exclusive, not additive: FILE is file-only (no console), STD is console-only
    (no file). Deliberately excludes `configure_trace`'s NDJSON wire trace, which
    stays file-only regardless (meant to be tailed, not mixed into a formatted
    stdout stream). Default is STD (see `log_sink` field below for why)."""

    FILE = "file"
    STD = "std"


class DiagnosticScope(StrEnum):
    """Which windows `diagnostics.watch_for_diagnostics` dumps -- input from
    IBC's `LogStructureScope`, not a straight copy (IBC also has `untitled`,
    dropped here: `WindowInfo.title` already distinguishes a real untitled
    window from one merely unmatched by any recogniser, so `UNKNOWN` alone
    covers it)."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    ALL = "all"


class DiagnosticWhen(StrEnum):
    """When `diagnostics.watch_for_diagnostics` dumps a window -- input from
    IBC's `LogStructureWhen`, minus `activate` (no matching `WindowEvent`
    kind exists today; only `window_opened`/`window_closed`). NEVER is the
    default -- diagnostics ship inert until explicitly opted into."""

    OPEN = "open"
    OPENCLOSE = "openclose"
    NEVER = "never"


class AcceptIncomingConnections(StrEnum):
    """AcceptIncomingConnections is the user's choice for what to do when ibcontroller
    detects incoming API connections (see `AcceptIncomingConnectionsRecognizer` in
    recognisers.py). The default is MANUAL, which leaves the user to decide what to do
    in the GUI. ACCEPT and REJECT are the two automatic options, and MANUAL is a special
    case that forces the user to decide what to do in the GUI.
    """

    ACCEPT = "accept"
    REJECT = "reject"
    MANUAL = "manual"


@ts.settings
class Config:
    """
    IBController's own config file, `ibcontroller.toml`.
    """

    # The IBKR settings file to load (if any) -- see `ibkr_settings.toml.example`.
    # Defaults to `None` (inert) until a user points `[settings] file` at a renamed
    # copy of the example file. See module docstring for why this is deliberate.
    settings_file: str | None = None
    # Matches config_old.py's own default: f"{program}-{trading_mode.value}", not
    # tws_channel -- confirmed against config_old.py:510.
    instance: str = "{program}-{trading_mode}"
    program: str = "gateway"  # "gateway" or "tws"
    # None -> auto-detected from the real install directory (launcher.
    # _detect_tws_version), channel-aware via tws_channel below.
    tws_version: str | None = None
    # Filters auto-detection to installs carrying this channel (see
    # launcher._detect_tws_version); "stable"/"latest" as directly observed on real
    # installs (not a strict enum). Defaults to "stable", the safer pick when several
    # channels are installed side by side -- confirmed 2026-09-11, see TODO.md.
    tws_channel: str = "stable"
    tws_path: str | None = None
    # Where TWS/Gateway itself stores its settings (IBC's own TWS_SETTINGS_PATH) --
    # deliberately NOT the same as tws_path (the install location). None defaults,
    # per instance, in launcher.py's resolve_tws_settings_path.
    tws_settings_path: str | None = None
    # IBC-key-compatible settings (file or env, never a secret).
    trading_mode: TradingMode = TradingMode.PAPER
    # IBC: ReadOnlyLogin -- loaded but not yet wired to any behavior, see TODO.md.
    read_only_login: bool = False
    read_only_api: bool | None = None  # None = leave the existing setting unchanged
    accept_incoming_connections: AcceptIncomingConnections = (
        AcceptIncomingConnections.MANUAL
    )
    existing_session_action: ExistingSessionAction = ExistingSessionAction.MANUAL

    # Login/2FA timeout and retry settings
    # IBC: LoginDialogDisplayTimeout
    login_dialog_display_timeout: float = 60.0
    # IBC: SecondFactorAuthenticationTimeout
    second_factor_authentication_timeout: float = 180.0
    # IBC: ReloginAfterSecondFactorAuthenticationTimeout
    relogin_after_2fa_timeout: bool = False
    # IBC: SecondFactorAuthenticationExitInterval
    second_factor_authentication_exit_interval: float = 60.0

    # AutoRestartTime "hh:mm AM/PM" format (e.g. "08:00 AM")
    auto_restart_time: str | None = None  # None = leave the existing setting unchanged
    # AutoLogoffTime "hh:mm AM/PM" format (e.g. "08:00 AM"); shares one Lock and
    # Exit radio-button pair with auto_restart_time -- if both are set, the latter
    # wins (applied last, matching builtin_settings.toml's order and IBC itself).
    auto_logoff_time: str | None = None  # None = leave the existing setting unchanged

    # Self-scheduled shutdown actions (applies to TWS and Gateway alike), consumed
    # by schedule.py/control_loop.py -- not GUI settings written to TWS/Gateway.
    # ColdRestartTime "HH:MM" 24-hour local time; every Sunday, close the instance
    # tidily and relaunch with a full fresh login (weekly reauth, Sunday 01:00
    # US/Eastern token invalidation).
    cold_restart_time: str | None = None  # None = disable
    # ClosedownAt "HH:MM" (daily) or "<Weekday> HH:MM" (weekly); close the instance
    # tidily, no relaunch. If both cold_restart_time and closedown_at are set,
    # whichever occurs first wins.
    closedown_at: str | None = None  # None = disable

    # JVM heap for TWS/Gateway at launch, e.g. "1024m"/"4g". Overrides the
    # -Xmx line in the installed .vmoptions file (ibgateway.vmoptions/tws.
    # vmoptions); None = leave the file's value untouched.
    java_heap_size: str | None = None

    # Credentials -- environment-variable-only. Field name matches the real env var
    # (IBCONTROLLER_USERID) directly -- no alias/mapping needed.
    userid: Secret = ts.secret(default=None)
    password: Secret = ts.secret(default=None)

    # Logging and tracing -- log_dir is the one path ibcontroller's own log/trace files
    # land in, always resolved: never None. Defaults to the platformdirs log dir
    # (app_dirs.resolve_app_dirs, honoring IBCONTROLLER_APP_DIR's docker mode); a
    # caller-supplied `load_config(log_dir=...)` is only a lower-priority default
    # (DictLoader), so the config file's `log_dir` and the IBCONTROLLER_LOG_DIR env var
    # genuinely override it. (2026-09-12: this field used to be `str | None` with
    # `load_config`'s platform-dirs parameter force-overwriting whatever TOML set --
    # pyrefly correctly refused `Path(config.log_dir)` in launcher.py:602, and a config
    # file `log_dir` silently never took effect.) See load_config for the loader order.
    trace_enabled: bool = False
    log_dir: str = ts.option(factory=lambda: str(resolve_app_dirs()[1]))
    # Logging level for ibcontroller's own log file (not the raw wire trace).
    # Was previously unannotated (`log_level = logging.INFO`), which meant attrs
    # never turned it into a real field at all -- not configurable, silently fixed
    # at INFO, confirmed live via attrs.fields(Config).
    log_level: int = ts.option(default=logging.INFO, converter=_log_level_converter)
    # Where ibcontroller's own log (configure_logging) and Gateway/TWS's raw console
    # output (configure_gateway_stdout) go -- "std" (console only) or "file" (only
    # gateway-{instance}.log/ibcontroller-{instance}.log under log_dir), never both.
    # Default is "std", not "file": this project's live-testing workflow relies on
    # seeing logger output in the terminal during real login/restart sessions, and
    # "std" is also what a Docker deployment wants with zero configuration (`docker
    # logs` captures stdout/stderr, not files written inside the container). "file"
    # is the explicit opt-in for a headless/service deployment that wants a
    # persistent log file instead. Does not affect configure_trace's NDJSON wire
    # trace, which stays file-only regardless (gitea #26).
    log_sink: LogSink = LogSink.STD

    # Diagnostics -- see diagnostics.py. Off by default (diagnostic_when=never);
    # an opted-in deployment gets a structure dump logged for each matching
    # window event, the replacement for socket-poking the agent by hand.
    diagnostic_scope: DiagnosticScope = DiagnosticScope.KNOWN
    diagnostic_when: DiagnosticWhen = DiagnosticWhen.NEVER


def load_config(
    config_dir: str | Path,
    log_dir: str | Path | None = None,
    toml_path: str | Path | None = None,
    dotenv_path: str | Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> Config:

    load_dotenv(dotenv_path, override=False)
    sys.stdout.write("IBController > loaded from dotenv (override=False)\n")
    env = os.environ

    # display the env vars we actually read, but never print credentials in plaintext
    sys.stdout.write("IBController > Environment variable: \n")
    for key, val in env.items():
        if key in ENV_SENSITIVE:
            sys.stdout.write(f"IBController > env {key}=******\n")
        elif key.startswith(ENV_PREFIX) and key not in ENV_SENSITIVE:
            sys.stdout.write(f"IBController > env {key}={val}\n")

    _config_file = (
        Path(toml_path) if toml_path else Path(config_dir) / "ibcontroller.toml"
    )
    # Credentials are environment-variable-only -- reject them here, against the raw
    # parsed TOML, before ts.load_settings ever sees the file (typed-settings has no
    # per-key allow/deny-list hook of its own; userid/password are real Config fields,
    # so InvalidOptionsError would not catch this). Silently skipped if the file
    # doesn't exist, matching FileLoader's own optional-file handling.
    try:
        with _config_file.open("rb") as f:
            _raw_toml = tomllib.load(f)
    except OSError:
        pass
    else:
        _reject_credentials_in_file(_raw_toml, _config_file)

    # section=None -- the TOML file is flat, no [section] headers (see
    # config_old.py's docstring). TomlFormat("") looks up settings[""] instead of
    # returning the top-level dict, so it silently discards the whole file --
    # confirmed live.
    CONF_FORMATS = {"*.toml": TomlFormat(None)}
    # A caller-supplied log_dir (platform dirs in production, a tmp dir in tests) is
    # a low-priority DEFAULT -- first in the loader list, so every later loader wins
    # over it and `log_dir` in the config file (or IBCONTROLLER_LOG_DIR) genuinely
    # takes effect. The Config field's own factory default (resolve_app_dirs) is the
    # even-lower built-in base when neither param nor file/env set it.
    _log_dir_default: dict[str, str] = {}
    if log_dir is not None:
        _log_dir_default["log_dir"] = str(log_dir)
    CONF_LOADERS: list[FileLoader | EnvLoader | DictLoader] = [
        DictLoader(_log_dir_default),
        ts.loaders.FileLoader(
            files=[_config_file],
            # pyrefly: ignore [bad-argument-type]
            formats=CONF_FORMATS,
        ),
        # Must come after FileLoader: later loaders win, so this reproduces
        # defaults < file < env (config_old.py's own precedence). _FileBackedEnvLoader
        # adds the _FILE-suffix Docker-secrets indirection on top of plain EnvLoader.
        _FileBackedEnvLoader(prefix=ENV_PREFIX),
    ]
    # CLI flags (cli.py) are the most explicit, per-invocation expression of intent --
    # last loader wins, so these override file/env unconditionally. Only added when
    # the caller actually set something, same reasoning as _log_dir_default above.
    if cli_overrides:
        CONF_LOADERS.append(DictLoader(dict(cli_overrides)))
    CONF_PROCESSORS: list[FormatProcessor] = [FormatProcessor()]
    CONF_CONVERTER: Converter = ts.converters.default_converter()

    try:
        _config: Config = ts.load_settings(
            Config,
            loaders=CONF_LOADERS,
            processors=CONF_PROCESSORS,
            converter=CONF_CONVERTER,
        )
    except TsError as exc:
        raise ConfigError(str(exc)) from exc

    if (
        _config.userid.get_secret_value() is None
        or _config.password.get_secret_value() is None
    ):
        raise ConfigError(
            "credentials not found -- set IBCONTROLLER_USERID and "
            "IBCONTROLLER_PASSWORD in the environment"
        )

    return _config
