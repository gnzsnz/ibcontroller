"""main.py -- the single-instance runner CLI's `run`/`init` commands are thin wrappers
around. Covers the two real bugs the plan for this work found (`_load_jar_path` never
actually checked the jar existed; `labels` was never threaded into
`run_control_loop`, silently disabling the `{config_dir}/labels.json` override
mechanism -- CLAUDE.md Open item 11) plus the new config-dir scaffolding this module
adds to close that gap safely.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ibcontroller import main as _main
from ibcontroller.config import Config, ConfigError
from ibcontroller.control_loop import ShutdownCause
from ibcontroller.labels import Labels

pytestmark = pytest.mark.asyncio

_BASE_ENV = {
    "IBCONTROLLER_USERID": "papuser",
    "IBCONTROLLER_PASSWORD": "pappass",  # nosec B105
    "IBCONTROLLER_TRADING_MODE": "paper",
    "IBCONTROLLER_TWS_VERSION": "10.50",
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
    with pytest.raises(RuntimeError, match=r"ibcontroller-agent\.jar not found"):
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
    monkeypatch.delenv("IBCONTROLLER_TRADING_MODE", raising=False)
    monkeypatch.delenv("IBCONTROLLER_TWS_VERSION", raising=False)
    monkeypatch.delenv("IBCONTROLLER_USERID", raising=False)
    monkeypatch.delenv("IBCONTROLLER_PASSWORD", raising=False)

    with pytest.raises(ConfigError, match="credentials not found"):
        await _main.run_async(config_dir, log_dir)
    # the scaffold still ran even though load_config went on to fail
    assert (config_dir / "ibcontroller.toml").exists()


async def test_run_async_passes_config_and_labels_into_run_control_loop(
    tmp_path, monkeypatch
):
    """Regression test for CLAUDE.md Open item 11: today's `control_loop.
    run_control_loop`'s own default (`labels=None` -> `load_labels()`, no
    `config_dir`) never sees a user's `{config_dir}/labels.json` override.
    `run_async` must load labels *with* `config_dir` and actually pass them through."""
    for key, value in _BASE_ENV.items():
        monkeypatch.setenv(key, value)
    config_dir = tmp_path / "config"
    log_dir = tmp_path / "log"

    captured_config: Config | None = None
    captured_labels: Labels | None = None
    captured_agent_jar: Path | None = None

    async def fake_run_control_loop(config, agent_jar, *, labels=None):
        nonlocal captured_config, captured_labels, captured_agent_jar
        captured_config = config
        captured_labels = labels
        captured_agent_jar = agent_jar
        return ShutdownCause.REQUESTED

    monkeypatch.setattr(_main, "run_control_loop", fake_run_control_loop)
    monkeypatch.setattr(_main, "_load_jar_path", lambda: tmp_path / "fake.jar")

    cause = await _main.run_async(config_dir, log_dir)

    assert cause is ShutdownCause.REQUESTED
    assert captured_config is not None
    assert captured_config.trading_mode.value == "paper"
    assert captured_labels is not None
    assert captured_agent_jar == tmp_path / "fake.jar"
