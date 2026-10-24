"""main.py -- the single-instance runner CLI's `run`/`init` commands are thin wrappers
around. Covers `_load_jar_path` checking the jar exists, `labels` being threaded into
`run_control_loop` (so the `{config_dir}/labels.json` override takes effect), and
the config-dir scaffolding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibcontroller import main as _main
from ibcontroller.config import Config, ConfigError
from ibcontroller.control_loop import ShutdownCause
from ibcontroller.dialogs import DialogChoice, DialogsError
from ibcontroller.labels import Labels
from ibcontroller.launcher import LauncherError

pytestmark = pytest.mark.asyncio

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


async def test_ensure_config_scaffold_creates_dirs_and_all_three_templates(tmp_path):
    config_dir = tmp_path / "config"
    log_dir = tmp_path / "log"

    written = _main.ensure_config_scaffold(config_dir, log_dir)

    assert config_dir.is_dir()
    assert log_dir.is_dir()
    assert {p.name for p in written} == {
        "ibcontroller.toml",
        "ibkr_settings.toml.example",
        "labels.json.example",
    }
    # the two example files keep their .example suffix -- deliberately inert, see
    # main.py's own module docstring.
    assert not (config_dir / "labels.json").exists()
    assert not (config_dir / "ibkr_settings.toml").exists()
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
    captured_agent_jar: Path | None = None

    async def fake_run_control_loop(config, agent_jar, *, labels, dialogs):
        nonlocal captured_config, captured_labels, captured_dialogs, captured_agent_jar
        captured_config = config
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
    assert captured_agent_jar == tmp_path / "fake.jar"


async def _run_capturing_dialogs(tmp_path, monkeypatch, toml_text):
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "ibcontroller.toml").write_text(toml_text)
    captured: list[DialogChoice] = []

    async def fake_run_control_loop(config, agent_jar, *, labels, dialogs):
        captured.extend(dialogs)
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
