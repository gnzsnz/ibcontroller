"""Where ibcontroller's own files live on disk when not told otherwise explicitly.
The config file's default location, and where `config.py`'s
`log_dir` (which also hosts trace files when tracing is enabled) defaults to.

**"Docker mode":** `IBC_APP_DIR`, if set, replaces platformdirs' own per-OS
convention (`~/Library/Application Support`, `%APPDATA%`, `$XDG_CONFIG_HOME`, ...)
outright -- a container wants one predictable mounted volume with a couple of
subdirectories in it, not an OS convention designed for a desktop user account.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import platformdirs

_APP_NAME = "ibcontroller"


def resolve_app_dirs(env: Mapping[str, str] | None = None) -> tuple[Path, Path]:
    """
    Returns `(config_dir, log_dir)`.  Single source of truth for where our files live.
    """
    env = os.environ if env is None else env
    app_dir = env.get("IBC_APP_DIR")
    if app_dir:
        base = Path(app_dir).expanduser()
        return base / "config", base / "log"
    dirs = platformdirs.PlatformDirs(_APP_NAME, appauthor=False)
    return Path(dirs.user_config_dir).expanduser(), Path(dirs.user_log_dir).expanduser()


def resolve_runtime_dir(env: Mapping[str, str] | None = None) -> Path:
    """
    Where the agent's own Unix domain sockets live.
    """
    env = os.environ if env is None else env
    app_dir = env.get("IBC_APP_DIR")
    if app_dir:
        return Path(app_dir).expanduser() / "run"
    dirs = platformdirs.PlatformDirs(_APP_NAME, appauthor=False)
    return Path(dirs.user_runtime_dir).expanduser()
