"""Regression tests for config.py's `load_config` (the typed-settings-based rewrite,
see the plan discussed with the user 2026-09-11). Covers the two bugs found live during
that review: `TomlFormat("")` silently discarding the whole TOML file, and no
`EnvLoader` being wired in at all -- both fixed by pointing directly at the real
loader, not a mock."""

from __future__ import annotations

import logging
from pathlib import Path

import attrs
import pytest

from ibcontroller.config import (
    ConfigError,
    MissingCredentialsError,
    TradingMode,
    _default_time_zone,
    load_config,
)


def _write_toml(tmp_path, text):
    path = tmp_path / "ibcontroller.toml"
    path.write_text(text)
    return path


def _set_credentials(monkeypatch):
    monkeypatch.setenv("IBC_USERID", "user")
    monkeypatch.setenv("IBC_PASSWORD", "pass")


def test_toml_values_are_actually_applied(monkeypatch, tmp_path):
    """`TomlFormat("")` used to look up settings[""] instead of the top-level dict,
    silently discarding the whole file -- confirmed live before the fix."""
    _set_credentials(monkeypatch)
    toml_path = _write_toml(tmp_path, 'trading_mode = "live"\nread_only_api = true\n')
    config = load_config(config_dir=tmp_path, log_dir=tmp_path, toml_path=toml_path)
    assert config.trading_mode is TradingMode.LIVE
    assert config.read_only_api is True


def test_env_var_overrides_toml_value(monkeypatch, tmp_path):
    """No `EnvLoader` was wired into `load_config`'s loader list at all -- env vars
    had zero effect on the loaded Config, confirmed live before the fix."""
    _set_credentials(monkeypatch)
    toml_path = _write_toml(tmp_path, 'trading_mode = "paper"\n')
    monkeypatch.setenv("IBC_TRADING_MODE", "live")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path, toml_path=toml_path)
    assert config.trading_mode is TradingMode.LIVE


def test_log_dir_tilde_is_expanded(monkeypatch, tmp_path):
    """A literal `~` in `log_dir` (TOML) must be expanded to the home directory here --
    downstream `Path(log_dir)` calls (logging_setup.py, launcher.py) don't expanduser()
    themselves, so an unexpanded `~` becomes a literal `~` directory, confirmed live."""
    _set_credentials(monkeypatch)
    toml_path = _write_toml(tmp_path, 'log_dir = "~/some/sub/dir"\n')
    config = load_config(config_dir=tmp_path, log_dir=tmp_path, toml_path=toml_path)
    assert "~" not in config.log_dir
    assert config.log_dir == str(Path("~/some/sub/dir").expanduser())


@pytest.mark.parametrize("field", ["tws_path", "tws_settings_path", "settings_file"])
def test_optional_path_fields_tilde_is_expanded(monkeypatch, tmp_path, field):
    """`tws_path`/`tws_settings_path`/`settings_file` share `log_dir`'s converter
    (gitea #57, consolidating what used to be inconsistent per-field handling) --
    a literal `~` must be expanded here too, not left for a consumption site to
    forget."""
    _set_credentials(monkeypatch)
    toml_path = _write_toml(tmp_path, f'{field} = "~/some/sub/path"\n')
    config = load_config(config_dir=tmp_path, log_dir=tmp_path, toml_path=toml_path)
    value = getattr(config, field)
    assert value is not None
    assert "~" not in value
    assert value == str(Path("~/some/sub/path").expanduser())


def test_optional_path_fields_default_none_is_preserved(monkeypatch, tmp_path):
    """The path converter must pass `None` through unchanged -- these fields are
    optional and their absence carries meaning (auto-detect/no-op), not an empty
    string or expanded cwd."""
    _set_credentials(monkeypatch)
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.tws_path is None
    assert config.tws_settings_path is None
    assert config.settings_file is None


def test_missing_credentials_raise_config_error(monkeypatch, tmp_path):
    # dotenv_path="" points load_dotenv() at a nonexistent file instead of letting
    # it search upward and pick up the repo's real, gitignored .env.
    monkeypatch.delenv("IBC_USERID", raising=False)
    monkeypatch.delenv("IBC_PASSWORD", raising=False)
    with pytest.raises(MissingCredentialsError, match="credentials not found"):
        load_config(
            config_dir=tmp_path,
            log_dir=tmp_path,
            dotenv_path=tmp_path / "nonexistent.env",
        )


def test_invalid_value_from_env_is_not_missing_credentials_error(monkeypatch, tmp_path):
    """Gitea #63: `cli.py` must only show the credentials hint for a real
    missing-credentials failure -- any other bad value is a plain
    `ConfigError`, not `MissingCredentialsError`."""
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_TRADING_MODE", "xyz")
    with pytest.raises(ConfigError) as exc_info:
        load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert not isinstance(exc_info.value, MissingCredentialsError)


def test_invalid_enum_value_from_env_names_the_field(monkeypatch, tmp_path):
    """Gitea #63: the field name must survive, not just the group summary
    ("N errors occured ... (N sub-exception)")."""
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_TRADING_MODE", "xyz")
    with pytest.raises(ConfigError, match="trading_mode") as exc_info:
        load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert "sub-exception" not in str(exc_info.value)


def test_invalid_enum_value_from_toml_names_the_field(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    _write_toml(tmp_path, 'trading_mode = "xyz"\n')
    with pytest.raises(ConfigError, match="trading_mode") as exc_info:
        load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert "sub-exception" not in str(exc_info.value)


def test_log_level_converts_name_to_constant(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_LOG_LEVEL", "debug")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.log_level == logging.DEBUG


def test_log_level_rejects_bad_name(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_LOG_LEVEL", "bogus")
    with pytest.raises(ConfigError, match="log_level"):
        load_config(config_dir=tmp_path, log_dir=tmp_path)


def test_log_rotation_fields_default(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.log_max_bytes == 10_485_760
    assert config.log_backup_count == 5


def test_log_rotation_fields_env_override(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_LOG_MAX_BYTES", "1048576")
    monkeypatch.setenv("IBC_LOG_BACKUP_COUNT", "3")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.log_max_bytes == 1_048_576
    assert config.log_backup_count == 3


def test_tws_settings_path_field(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_TWS_SETTINGS_PATH", "/some/path")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.tws_settings_path == "/some/path"


def test_java_heap_size_field(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.java_heap_size is None
    monkeypatch.setenv("IBC_JAVA_HEAP_SIZE", "2g")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.java_heap_size == "2g"


def test_instance_default_uses_trading_mode(monkeypatch, tmp_path):
    """config.py's own default is f"{program}-{trading_mode.value}" -- confirmed
    against config.py:510, not tws_channel."""
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_TRADING_MODE", "live")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.instance == "gateway-live"


def test_password_file_suffix_reads_file_contents(monkeypatch, tmp_path):
    monkeypatch.setenv("IBC_USERID", "user")
    monkeypatch.delenv("IBC_PASSWORD", raising=False)
    secret_path = tmp_path / "password.txt"
    secret_path.write_text("from-file-pass\n")
    monkeypatch.setenv("IBC_PASSWORD_FILE", str(secret_path))
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.password.get_secret_value() == "from-file-pass"


def test_password_file_suffix_wins_over_plain_var(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    secret_path = tmp_path / "password.txt"
    secret_path.write_text("file-wins\n")
    monkeypatch.setenv("IBC_PASSWORD_FILE", str(secret_path))
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.password.get_secret_value() == "file-wins"


def test_credentials_in_toml_are_rejected(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    toml_path = _write_toml(tmp_path, 'userid = "sneaky"\n')
    with pytest.raises(ConfigError, match="credentials must not be set"):
        load_config(config_dir=tmp_path, log_dir=tmp_path, toml_path=toml_path)


def test_cli_overrides_win_over_env_and_toml(monkeypatch, tmp_path):
    """`cli_overrides` is the last loader -- must win over both TOML and env, the
    whole point of letting `cli.py`'s `run` pick trading_mode/etc. per invocation
    regardless of what a shared ibcontroller.toml or .env file says."""
    _set_credentials(monkeypatch)
    toml_path = _write_toml(tmp_path, 'trading_mode = "paper"\n')
    monkeypatch.setenv("IBC_TRADING_MODE", "paper")
    config = load_config(
        config_dir=tmp_path,
        log_dir=tmp_path,
        toml_path=toml_path,
        cli_overrides={"trading_mode": "live"},
    )
    assert config.trading_mode is TradingMode.LIVE


def test_cli_overrides_feed_instance_template(monkeypatch, tmp_path):
    """A `trading_mode` cli_override is resolved before FormatProcessor runs, so
    `instance`'s "{program}-{trading_mode}" default picks it up too -- confirming
    --trading-mode alone is enough to separate paper/live instances."""
    _set_credentials(monkeypatch)
    config = load_config(
        config_dir=tmp_path,
        log_dir=tmp_path,
        cli_overrides={"trading_mode": "live"},
    )
    assert config.instance == "gateway-live"


def test_empty_cli_overrides_do_not_affect_config(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    toml_path = _write_toml(tmp_path, 'trading_mode = "paper"\n')
    config = load_config(
        config_dir=tmp_path, log_dir=tmp_path, toml_path=toml_path, cli_overrides={}
    )
    assert config.trading_mode is TradingMode.PAPER


def test_load_config_prints_one_line_per_key_credentials_masked(
    monkeypatch, tmp_path, capsys
):
    """gitea #45: once loaded, Config is printed to stdout, one `key=value` line per
    field, with userid/password masked (Secret.__str__ already returns '*******')."""
    monkeypatch.setenv("IBC_USERID", "secret-user")
    monkeypatch.setenv("IBC_PASSWORD", "secret-pass")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    out = capsys.readouterr().out
    for field in attrs.fields(type(config)):
        assert f"{field.name}=" in out
    assert "secret-user" not in out
    assert "secret-pass" not in out
    assert "userid=*******" in out
    assert "password=*******" in out


def test_empty_env_var_optional_field_falls_back_to_none(monkeypatch, tmp_path):
    """gitea #67: `IBC_XYZ=` must be treated as absent, matching IBC's own
    getString(key, "").equals("") convention -- not fed through as a real value."""
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_AUTO_LOGOFF_TIME", "")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.auto_logoff_time is None


def test_empty_env_var_non_optional_field_falls_back_to_default(monkeypatch, tmp_path):
    """Same bug (#67), non-Optional side: an empty value used to raise ConfigError
    instead of falling back to the field's default."""
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_MFA_EXIT_INTERVAL", "")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.mfa_exit_interval == 60.0


def test_empty_password_file_falls_back_to_plain_var(monkeypatch, tmp_path):
    """An empty secrets file (`IBC_PASSWORD_FILE` pointing at a blank file) must not
    win over -- or be accepted in place of -- a real `IBC_PASSWORD`."""
    _set_credentials(monkeypatch)
    secret_path = tmp_path / "password.txt"
    secret_path.write_text("\n")
    monkeypatch.setenv("IBC_PASSWORD_FILE", str(secret_path))
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.password.get_secret_value() == "pass"


def test_default_time_zone_falls_back_to_etc_localtime(monkeypatch, tmp_path):
    monkeypatch.delenv("TZ", raising=False)
    zoneinfo_target = tmp_path / "zoneinfo" / "Europe" / "Zurich"
    zoneinfo_target.parent.mkdir(parents=True)
    zoneinfo_target.write_text("")
    localtime = tmp_path / "localtime"
    localtime.symlink_to(zoneinfo_target)
    monkeypatch.setattr(
        "ibcontroller.config.Path",
        lambda p: localtime if p == "/etc/localtime" else Path(p),
    )
    assert _default_time_zone() == "Europe/Zurich"


def test_default_time_zone_none_when_unresolved(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(
        "ibcontroller.config.Path", lambda p: Path("/nonexistent/localtime")
    )
    assert _default_time_zone() is None


def test_time_zone_defaults_from_tz_env_var(monkeypatch, tmp_path):
    """gitea #68: no IBC_TIME_ZONE needed -- the container's own TZ (already the
    Docker convention) is picked up automatically."""
    _set_credentials(monkeypatch)
    monkeypatch.setenv("TZ", "Europe/Zurich")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.time_zone == "Europe/Zurich"


def test_time_zone_env_var_overrides_tz_default(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("TZ", "Europe/Zurich")
    monkeypatch.setenv("IBC_TIME_ZONE", "America/New_York")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.time_zone == "America/New_York"


def test_legacy_section_headers_raise_clearly(monkeypatch, tmp_path):
    """Phase 4 assessment (2026-09-11): config_old.py had a hand-written check for
    this (a real, live-caught bug -- a [section]-header config silently dropped every
    key inside it). Confirmed live that typed-settings' own `FileLoader` already
    catches this for free via `InvalidOptionsError` (`gateway.tws_version` isn't a
    real flat field) -- no hand-written check needed, just regression coverage."""
    _set_credentials(monkeypatch)
    toml_path = _write_toml(
        tmp_path, '[gateway]\ntws_version = "10.45"\n\n[auth]\ntrading_mode = "paper"\n'
    )
    with pytest.raises(ConfigError, match=r"gateway\.tws_version"):
        load_config(config_dir=tmp_path, log_dir=tmp_path, toml_path=toml_path)


def test_env_dump_masks_sensitive_ibc_vars(monkeypatch, tmp_path, capsys):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_PASSWORD_PAPER", "hunter2")
    monkeypatch.setenv("IBC_USERID_PAPER", "paperuser")
    secret = tmp_path / "pass"
    secret.write_text("filepass")
    monkeypatch.setenv("IBC_PASSWORD_FILE", str(secret))
    monkeypatch.setenv("IBC_LOG_LEVEL", "debug")
    monkeypatch.setenv("OTHER_PASSWORD", "outside-prefix")
    load_config(config_dir=tmp_path, log_dir=tmp_path)
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "paperuser" not in out
    assert "outside-prefix" not in out
    assert "env IBC_PASSWORD_PAPER=******" in out
    assert f"env IBC_PASSWORD_FILE={secret}" in out
    assert "env IBC_LOG_LEVEL=debug" in out
