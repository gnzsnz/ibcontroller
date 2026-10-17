"""Shared pytest fixtures, auto-discovered -- no import needed in test modules."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

import ibcontroller.config as _config


@pytest.fixture(autouse=True)
def _no_real_dotenv_discovery(monkeypatch):
    """Safety net: `config.load_config`'s `dotenv_path` defaults to
    `None`, which makes `python-dotenv`'s `load_dotenv` search *upward from the
    current directory* for a real `.env` file -- and this repo has one, at its
    root, with real credentials. A test that doesn't pass an explicit
    `dotenv_path` (most don't -- they expect `load_config` to fail cleanly on
    missing credentials) would otherwise have those credentials silently
    backfilled by the real file, and in a full end-to-end test
    (`test_main.py`'s `run_async`, `test_cli.py`'s `run` command) actually
    launch a real Gateway/TWS session with real credentials as a side effect
    of running `pytest`. Guarded at the one real chokepoint (`config.load_dotenv`,
    the name imported into `config.py`) rather than in each test: only the
    no-path (upward-search) call is neutered -- any test that wants real
    dotenv behavior already passes its own explicit tmp-path `.env` file
    (`test_dotenv_path_*`), which is untouched by this guard."""
    real_load_dotenv = _config.load_dotenv

    def _guarded_load_dotenv(dotenv_path=None, *args, **kwargs):
        if dotenv_path is None:
            return False
        return real_load_dotenv(dotenv_path, *args, **kwargs)

    monkeypatch.setattr(_config, "load_dotenv", _guarded_load_dotenv)


@pytest.fixture
def sock_path():
    """A short path directly under /tmp -- AF_UNIX has a ~104-byte path limit, and
    pytest's own `tmp_path` fixture nests deep enough (pytest-of-<user>/pytest-N/
    <test-name>.../) to blow past it on macOS."""
    path = Path(f"/tmp/ibcontroller-test-{uuid.uuid4().hex[:8]}.sock")  # nosec B108
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture
def run_dir():
    """A short runtime dir for `build_launch_plan`, which rejects socket paths
    over the AF_UNIX limit -- `tmp_path` is too deep on macOS (see `sock_path`)."""
    path = Path(f"/tmp/ibc-run-{uuid.uuid4().hex[:8]}")  # nosec B108
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def event_sock_path():
    """Same as `sock_path`, for tests that need a second, independent socket (a
    command socket and an event socket at once)."""
    path = Path(f"/tmp/ibcontroller-test-{uuid.uuid4().hex[:8]}-events.sock")  # nosec B108
    yield path
    path.unlink(missing_ok=True)
