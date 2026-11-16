"""main.py -- the single-instance runner CLI's `run`/`init` commands are thin wrappers
around. Covers `_load_jar_path` checking the jar exists, `labels` being threaded into
`run_control_loop` (so the `{config_dir}/labels.json` override takes effect), and
the config-dir scaffolding.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ibcontroller import main as _main
from ibcontroller.config import Config, ConfigError
from ibcontroller.control_loop import ShutdownCause
from ibcontroller.dialogs import DialogChoice, DialogsError
from ibcontroller.labels import Labels
from ibcontroller.launcher import LauncherError
from ibcontroller.logging_setup import stop_logging
from ibcontroller.settings import SettingChoice, SettingsError

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _restore_ibcontroller_logger():
    """`run_async` configures the shared `ibcontroller` logger (handlers,
    `propagate = False`); put it back so later tests' `caplog` still works."""
    logger = logging.getLogger("ibcontroller")
    saved = (logger.handlers, logger.propagate, logger.level)
    yield
    stop_logging()
    logger.handlers, logger.propagate, logger.level = saved


_BASE_ENV = {
    "IBC_USERID": "papuser",
    "IBC_PASSWORD": "pappass",  # nosec B105
    "IBC_TRADING_MODE": "paper",
    "IBC_TWS_VERSION": "10.50",
}


class _FakeTraversable:
    """Minimal stand-in for `importlib.resources.files(...)` -- just enough for
    `_load_jar_path`'s own `.joinpath(...)` call."""

    def __init__(self, root):
        self._root = root

    def joinpath(self, *parts):
        return self._root.joinpath(*parts)


async def test_load_jar_path_raises_clearly_when_jar_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        _main.resources, "files", lambda _pkg: _FakeTraversable(tmp_path)
    )
    with pytest.raises(LauncherError, match=r"ibcontroller-agent\.jar not found"):
        _main._load_jar_path()


async def test_load_jar_path_succeeds_when_jar_present(monkeypatch, tmp_path):
    jar = tmp_path / "ibcontroller-agent.jar"
    jar.write_bytes(b"not a real jar, just needs to exist")
    monkeypatch.setattr(
        _main.resources, "files", lambda _pkg: _FakeTraversable(tmp_path)
    )
    assert _main._load_jar_path() == jar


async def test_ensure_config_scaffold_creates_dirs_and_both_templates(tmp_path):
    config_dir = tmp_path / "config"
    log_dir = tmp_path / "log"

    written = _main.ensure_config_scaffold(config_dir, log_dir)

    assert config_dir.is_dir()
    assert log_dir.is_dir()
    assert {p.name for p in written} == {
        "ibcontroller.toml",
        "labels.json.example",
    }
    # labels.json.example keeps its .example suffix -- deliberately inert, see
    # main.py's own module docstring.
    assert not (config_dir / "labels.json").exists()
    assert (config_dir / "ibcontroller.toml").exists()


async def test_ensure_config_scaffold_never_overwrites_an_existing_file(tmp_path):
    config_dir = tmp_path / "config"
    log_dir = tmp_path / "log"
    _main.ensure_config_scaffold(config_dir, log_dir)
    custom = "# a user's own edited config\n"
    (config_dir / "ibcontroller.toml").write_text(custom)

    written = _main.ensure_config_scaffold(config_dir, log_dir)

    assert written == []
    assert (config_dir / "ibcontroller.toml").read_text() == custom


async def test_ensure_config_scaffold_force_overwrites(tmp_path):
    config_dir = tmp_path / "config"
    log_dir = tmp_path / "log"
    _main.ensure_config_scaffold(config_dir, log_dir)
    (config_dir / "ibcontroller.toml").write_text("a user's own edited config")

    written = _main.ensure_config_scaffold(config_dir, log_dir, force=True)

    assert (config_dir / "ibcontroller.toml") in written
    new_text = (config_dir / "ibcontroller.toml").read_text()
    assert "user's own edited config" not in new_text


async def test_run_async_scaffolds_bundled_ibcontroller_toml_is_inert(
    tmp_path, monkeypatch
):
    """The scaffolded ibcontroller.toml ships with every key commented out -- copying
    it must change nothing versus built-in defaults, so load_config still raises for
    the fields that have no default (credentials) rather than silently starting with
    placeholder values."""
    config_dir = tmp_path / "config"
    log_dir = tmp_path / "log"
    monkeypatch.delenv("IBC_TRADING_MODE", raising=False)
    monkeypatch.delenv("IBC_TWS_VERSION", raising=False)
    monkeypatch.delenv("IBC_USERID", raising=False)
    monkeypatch.delenv("IBC_PASSWORD", raising=False)

    with pytest.raises(ConfigError, match="credentials not found"):
        await _main.run_async(config_dir, log_dir)
    # the scaffold still ran even though load_config went on to fail
    assert (config_dir / "ibcontroller.toml").exists()


async def test_run_async_passes_config_and_labels_into_run_control_loop(
    tmp_path, monkeypatch
):
    """Regression test: `control_loop.
    run_control_loop`'s own default (`labels=None` -> `load_labels()`, no
    `config_dir`) never sees a user's `{config_dir}/labels.json` override.
    `run_async` must load labels *with* `config_dir` and actually pass them through."""
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    config_dir = tmp_path / "config"
    log_dir = tmp_path / "log"

    captured_config: Config | None = None
    captured_labels: Labels | None = None
    captured_dialogs: list[DialogChoice] | None = None
    captured_settings: list[SettingChoice] | None = None
    captured_agent_jar: Path | None = None

    async def fake_run_control_loop(config, agent_jar, *, labels, dialogs, settings):
        nonlocal captured_config, captured_labels, captured_dialogs
        nonlocal captured_settings, captured_agent_jar
        captured_config = config
        captured_settings = settings
        captured_labels = labels
        captured_dialogs = dialogs
        captured_agent_jar = agent_jar
        return ShutdownCause.REQUESTED

    monkeypatch.setattr(_main, "run_control_loop", fake_run_control_loop)
    monkeypatch.setattr(_main, "_load_jar_path", lambda: tmp_path / "fake.jar")

    cause = await _main.run_async(config_dir, log_dir)

    assert cause is ShutdownCause.REQUESTED
    assert captured_config is not None
    assert captured_config.trading_mode.value == "paper"
    assert captured_labels is not None
    assert captured_dialogs is not None
    assert captured_dialogs, "bundled catalog loaded"
    assert captured_settings == [], "no [ibkr] value, nothing to apply"
    assert captured_agent_jar == tmp_path / "fake.jar"


captured_settings: list[SettingChoice] = []


async def _run_capturing_dialogs(tmp_path, monkeypatch, toml_text):
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ibcontroller.toml").write_text(toml_text)
    captured: list[DialogChoice] = []
    captured_settings.clear()

    async def fake_run_control_loop(config, agent_jar, *, labels, dialogs, settings):
        captured.extend(dialogs)
        captured_settings.extend(settings)
        return ShutdownCause.REQUESTED

    monkeypatch.setattr(_main, "run_control_loop", fake_run_control_loop)
    monkeypatch.setattr(_main, "_load_jar_path", lambda: tmp_path / "fake.jar")
    await _main.run_async(config_dir, tmp_path / "log")
    return {c.entry.name: c for c in captured}


async def test_run_async_resolves_dialog_modes_from_file_env_and_custom(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("IBKR_PASSWORD_EXPIRY_WARNING", "yes")
    toml = (
        "[ibkr]\nblind_trading = true\nmy_warning = 'on'\n"
        "[[dialog]]\nname = 'my_warning'\ndefault = 'off'\n"
        "match = [ { text = 'some phrase' } ]\n"
        "[dialog.modes]\noff = []\non = [ { click = 'OK' } ]\n"
    )
    choices = await _run_capturing_dialogs(tmp_path, monkeypatch, toml)
    assert (choices["blind_trading"].mode, choices["blind_trading"].source) == (
        "on",
        "file",
    )
    assert choices["password_expiry_warning"].source == "env"
    assert choices["password_expiry_warning"].mode == "on"
    assert choices["my_warning"].mode == "on"
    assert choices["non_brokerage_account"].source == "default"


async def test_run_async_bad_dialog_mode_is_a_startup_error(tmp_path, monkeypatch):
    with pytest.raises(DialogsError, match="blind_trading"):
        await _run_capturing_dialogs(
            tmp_path, monkeypatch, "[ibkr]\nblind_trading = 'maybe'\n"
        )


async def test_run_async_resolves_settings_from_file_env_and_custom(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("IBKR_AUTO_RESTART_TIME", "11:45 PM")
    toml = (
        "[ibkr]\nread_only_api = true\nmy_port = 4002\n"
        "[[setting]]\nname = 'my_port'\ntree_path = 'API/Settings'\n"
        "action = 'type_text'\nlabel = 'Socket port'\n"
    )
    await _run_capturing_dialogs(tmp_path, monkeypatch, toml)
    got = {c.entry.name: (c.value, c.source) for c in captured_settings}
    assert got == {
        "auto_restart_time": ("11:45 PM", "env"),
        "read_only_api": (True, "file"),
        "my_port": ("4002", "file"),
    }


async def test_run_async_bad_setting_value_is_a_startup_error(tmp_path, monkeypatch):
    with pytest.raises(SettingsError, match="read_only_api"):
        await _run_capturing_dialogs(
            tmp_path, monkeypatch, "[ibkr]\nread_only_api = 'maybe'\n"
        )


async def test_run_async_name_shared_by_dialog_and_setting_is_an_error(
    tmp_path, monkeypatch
):
    toml = (
        "[[setting]]\nname = 'blind_trading'\ntree_path = 'API'\n"
        "action = 'toggle'\nlabel = 'x'\n"
    )
    with pytest.raises(ConfigError, match="blind_trading"):
        await _run_capturing_dialogs(tmp_path, monkeypatch, toml)


_FILE_SINK = "[ibcontroller]\nlog_sink = 'file'\n"


def _log_text(tmp_path) -> str:
    """The instance log file's text, after the listener thread drained."""
    stop_logging()
    (log_file,) = (tmp_path / "log").glob("ibcontroller-*.log")
    return log_file.read_text()


async def test_run_async_warns_about_a_custom_setting_without_a_value(
    tmp_path, monkeypatch
):
    """A custom `[[setting]]` carries no value of its own, so one never given a
    value in `[ibkr]` does nothing; the warning names it and reaches the log
    file, which only exists because logging is configured before resolution."""
    toml = _FILE_SINK + (
        "[[setting]]\nname = 'my_port'\ntree_path = 'API/Settings'\n"
        "action = 'type_text'\nlabel = 'Socket port'\n"
    )
    await _run_capturing_dialogs(tmp_path, monkeypatch, toml)
    text = _log_text(tmp_path)
    assert "my_port" in text
    assert "IBKR_MY_PORT" in text
    assert captured_settings == []


async def test_run_async_no_warning_for_a_custom_setting_with_a_value(
    tmp_path, monkeypatch
):
    toml = _FILE_SINK + (
        "[ibkr]\nmy_port = 4002\n"
        "[[setting]]\nname = 'my_port'\ntree_path = 'API/Settings'\n"
        "action = 'type_text'\nlabel = 'Socket port'\n"
    )
    await _run_capturing_dialogs(tmp_path, monkeypatch, toml)
    assert "WARNING" not in _log_text(tmp_path)


async def test_run_async_warns_about_ibkr_names_that_match_nothing(
    tmp_path, monkeypatch
):
    await _run_capturing_dialogs(
        tmp_path, monkeypatch, _FILE_SINK + "[ibkr]\nread_only_apii = true\n"
    )
    assert "read_only_apii" in _log_text(tmp_path)


async def test_run_async_flushes_the_log_when_startup_fails(tmp_path, monkeypatch):
    """A warning logged just before a startup error must still reach the file:
    `run_control_loop`, which normally drains the log, never runs."""
    toml = _FILE_SINK + "[ibkr]\nread_only_apii = true\nblind_trading = 'maybe'\n"
    with pytest.raises(DialogsError):
        await _run_capturing_dialogs(tmp_path, monkeypatch, toml)
    (log_file,) = (tmp_path / "log").glob("ibcontroller-*.log")
    assert "read_only_apii" in log_file.read_text()
