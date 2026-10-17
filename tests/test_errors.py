"""errors.py -- the exit-code contract read off the exception hierarchy."""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import ibcontroller
from ibcontroller.agent_client import AgentClientError, ProtocolError
from ibcontroller.config import ConfigError, MissingCredentialsError
from ibcontroller.errors import IBControllerError, TransientError
from ibcontroller.labels import LabelsError
from ibcontroller.launcher import AgentStartupError, LauncherError
from ibcontroller.login import (
    CredentialsRejectedError,
    LoginError,
    LoginFrameTimeoutError,
    MfaTimeoutError,
    TransientLoginError,
)
from ibcontroller.schedule import ScheduleError
from ibcontroller.settings import SettingsError

# Raised and handled inside a cycle, never reaching `cli.py`: keep the default code 1.
_NEVER_EXITS = {SettingsError, ScheduleError}


@pytest.mark.parametrize(
    ("exc_type", "expected_exit_code"),
    [
        (ConfigError, 78),  # EX_CONFIG
        (MissingCredentialsError, 78),
        (LabelsError, 78),
        (LauncherError, 78),
        (CredentialsRejectedError, 77),  # EX_NOPERM
        (MfaTimeoutError, 75),  # EX_TEMPFAIL
        (TransientError, 69),  # EX_UNAVAILABLE
        (TransientLoginError, 69),
        (LoginError, 69),
        (LoginFrameTimeoutError, 69),
        (AgentStartupError, 69),
        (AgentClientError, 69),
        (ProtocolError, 69),
    ],
)
def test_exit_code_through_the_hierarchy(exc_type, expected_exit_code):
    assert issubclass(exc_type, IBControllerError)
    assert exc_type.exit_code == expected_exit_code


def test_agent_startup_error_is_not_a_launcher_error():
    """Transient, so it must not inherit `LauncherError`'s config exit code."""
    assert not issubclass(AgentStartupError, LauncherError)


def _all_subclasses(cls: type) -> set[type]:
    found: set[type] = set()
    for sub in cls.__subclasses__():
        found.add(sub)
        found |= _all_subclasses(sub)
    return found


def test_every_public_exception_has_an_explicit_exit_code():
    for module in pkgutil.iter_modules(ibcontroller.__path__):
        importlib.import_module(f"ibcontroller.{module.name}")
    for exc_type in _all_subclasses(IBControllerError):
        if exc_type in _NEVER_EXITS:
            assert exc_type.exit_code == 1
        else:
            assert exc_type.exit_code != IBControllerError.exit_code, exc_type
