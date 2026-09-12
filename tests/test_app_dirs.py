"""Unit tests for app_dirs.py -- no live Gateway needed."""

from __future__ import annotations

from pathlib import Path

import platformdirs

from ibcontroller.app_dirs import resolve_app_dirs, resolve_runtime_dir


def test_default_uses_platformdirs():
    config_dir, log_dir = resolve_app_dirs(env={})
    dirs = platformdirs.PlatformDirs("ibcontroller", appauthor=False)
    assert config_dir == Path(dirs.user_config_dir)
    assert log_dir == Path(dirs.user_log_dir)


def test_docker_mode_env_var_overrides_platformdirs():
    config_dir, log_dir = resolve_app_dirs(env={"IBCONTROLLER_APP_DIR": "/data/ibc"})
    assert config_dir == Path("/data/ibc/config")
    assert log_dir == Path("/data/ibc/log")


def test_docker_mode_ignores_empty_string():
    """Empty counts as unset, matching config.py's own convention elsewhere --
    lets a deployment clear the var without deleting it."""
    config_dir, _log_dir = resolve_app_dirs(env={"IBCONTROLLER_APP_DIR": ""})
    dirs = platformdirs.PlatformDirs("ibcontroller", appauthor=False)
    assert config_dir == Path(dirs.user_config_dir)


def test_defaults_to_real_os_environ():
    """No `env=` passed at all -- falls back to `os.environ`, same convention as
    config.py's own `load_config`."""
    config_dir, log_dir = resolve_app_dirs()
    assert isinstance(config_dir, Path)
    assert isinstance(log_dir, Path)


def test_runtime_dir_default_uses_platformdirs():
    runtime_dir = resolve_runtime_dir(env={})
    dirs = platformdirs.PlatformDirs("ibcontroller", appauthor=False)
    assert runtime_dir == Path(dirs.user_runtime_dir)


def test_runtime_dir_docker_mode_env_var_overrides_platformdirs():
    runtime_dir = resolve_runtime_dir(env={"IBCONTROLLER_APP_DIR": "/data/ibc"})
    assert runtime_dir == Path("/data/ibc/run")


def test_runtime_dir_docker_mode_ignores_empty_string():
    runtime_dir = resolve_runtime_dir(env={"IBCONTROLLER_APP_DIR": ""})
    dirs = platformdirs.PlatformDirs("ibcontroller", appauthor=False)
    assert runtime_dir == Path(dirs.user_runtime_dir)
