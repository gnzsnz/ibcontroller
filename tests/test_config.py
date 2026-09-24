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

from ibcontroller.config import ConfigError, TradingMode, load_config


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
    with pytest.raises(ConfigError, match="credentials not found"):
        load_config(
            config_dir=tmp_path,
            log_dir=tmp_path,
            dotenv_path=tmp_path / "nonexistent.env",
        )


def test_log_level_converts_name_to_constant(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_LOG_LEVEL", "debug")
    config = load_config(config_dir=tmp_path, log_dir=tmp_path)
    assert config.log_level == logging.DEBUG


def test_log_level_rejects_bad_name(monkeypatch, tmp_path):
    _set_credentials(monkeypatch)
    monkeypatch.setenv("IBC_LOG_LEVEL", "bogus")
    with pytest.raises(ConfigError):
        load_config(config_dir=tmp_path, log_dir=tmp_path)


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
