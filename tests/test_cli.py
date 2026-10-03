"""cli.py -- the installed `ibcontroller` command. Thin: argument parsing plus mapping
`main.py`'s exceptions to exit codes and human-readable messages. Runs through
`typer.testing.CliRunner` (in-process, no subprocess) against a tmp_path-scoped
`IBC_APP_DIR`, same isolation pattern as `tests/test_trace_integration.py` --
never touches the real platformdirs locations.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from ibcontroller import cli as _cli
from ibcontroller import main as _main
from ibcontroller.agent_client import AgentClientError
from ibcontroller.config import ConfigError
from ibcontroller.control_loop import ShutdownCause
from ibcontroller.launcher import AgentStartupError, LauncherError
from ibcontroller.login import LoginError, MfaTimeoutError
from ibcontroller.recognisers import LoginFailedError

runner = CliRunner()

_BASE_ENV = {
    "IBC_USERID": "papuser",
    "IBC_PASSWORD": "pappass",  # nosec B105
    "IBC_TRADING_MODE": "paper",
    "IBC_TWS_VERSION": "10.50",
}


def test_init_scaffolds_config_dir(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    monkeypatch.setenv("IBC_APP_DIR", str(app_dir))

    result = runner.invoke(_cli.app, ["init"])

    assert result.exit_code == 0, result.output
    assert "wrote ibcontroller.toml" in result.output
    assert (app_dir / "config" / "ibcontroller.toml").exists()
    # the two example files keep their .example suffix -- deliberately inert, see
    # main.py's own module docstring.
    assert (app_dir / "config" / "ibkr_settings.toml.example").exists()
    assert (app_dir / "config" / "labels.json.example").exists()


def test_init_reports_nothing_to_do_on_second_run(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    monkeypatch.setenv("IBC_APP_DIR", str(app_dir))
    runner.invoke(_cli.app, ["init"])

    result = runner.invoke(_cli.app, ["init"])

    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output


def test_run_without_config_auto_scaffolds_then_fails_clearly(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    monkeypatch.setenv("IBC_APP_DIR", str(app_dir))
    for key in _BASE_ENV:
        monkeypatch.delenv(key, raising=False)

    result = runner.invoke(_cli.app, ["run"])

    assert result.exit_code == 78  # EX_CONFIG
    assert "credentials not found" in result.output
    assert "set IBC_USERID/IBC_PASSWORD" in result.output
    # the scaffold ran before load_config failed, same as run_async's own contract
    assert (app_dir / "config" / "ibcontroller.toml").exists()


def test_run_shows_general_hint_not_credentials_hint_for_other_config_errors(
    tmp_path, monkeypatch
):
    """Gitea #63: the credentials hint must only show up for a real
    missing-credentials failure, not every `ConfigError` (e.g. a bad
    `trading_mode` value, credentials fine)."""
    app_dir = tmp_path / "app"
    monkeypatch.setenv("IBC_APP_DIR", str(app_dir))

    async def fake_run_async(
        config_dir, log_dir, *, dotenv_path=None, cli_overrides=None
    ):
        raise ConfigError("trading_mode: 'xyz' is not a valid TradingMode")

    monkeypatch.setattr(_main, "run_async", fake_run_async)

    result = runner.invoke(_cli.app, ["run"])

    assert result.exit_code == 78  # EX_CONFIG
    assert "trading_mode" in result.output
    assert "IBC_USERID/IBC_PASSWORD" not in result.output
    assert "Check" in result.output  # the general hint, not the credentials one


def test_run_reports_shutdown_cause_on_success(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    monkeypatch.setenv("IBC_APP_DIR", str(app_dir))

    async def fake_run_async(
        config_dir, log_dir, *, dotenv_path=None, cli_overrides=None
    ):
        return ShutdownCause.REQUESTED

    monkeypatch.setattr(_main, "run_async", fake_run_async)

    result = runner.invoke(_cli.app, ["run"])

    assert result.exit_code == 0, result.output
    assert "stopped: REQUESTED" in result.output


@pytest.mark.parametrize(
    ("cause", "expected_exit_code"),
    [
        (ShutdownCause.REQUESTED, 0),
        (ShutdownCause.TIDY_CLOSEDOWN, 0),
        (ShutdownCause.PROCESS_CLOSED, 0),  # returncode 0, e.g. File>Close
        (ShutdownCause.PROCESS_EXITED, 69),  # EX_UNAVAILABLE
        (ShutdownCause.CONNECTION_LOST, 69),  # EX_UNAVAILABLE
        (ShutdownCause.LOGIN_FAILED, 77),  # EX_NOPERM
    ],
)
def test_run_exit_code_matches_shutdown_cause_contract(
    tmp_path, monkeypatch, cause, expected_exit_code
):
    """The exit-code contract agreed in gitea #60 -- a supervisor (Docker
    restart policy, systemd) tells a clean stop from a real failure apart by
    exit code alone, not by the `stopped: <CAUSE>` line."""
    app_dir = tmp_path / "app"
    monkeypatch.setenv("IBC_APP_DIR", str(app_dir))

    async def fake_run_async(
        config_dir, log_dir, *, dotenv_path=None, cli_overrides=None
    ):
        return cause

    monkeypatch.setattr(_main, "run_async", fake_run_async)

    result = runner.invoke(_cli.app, ["run"])

    assert result.exit_code == expected_exit_code, result.output
    assert f"stopped: {cause.name}" in result.output


@pytest.mark.parametrize(
    ("exc", "expected_exit_code"),
    [
        (LauncherError("no install found"), 78),  # EX_CONFIG
        (AgentStartupError("agent process exited before ping"), 69),  # EX_UNAVAILABLE
        (RuntimeError("agent jar not found"), 78),  # EX_CONFIG
        (LoginFailedError("bad credentials"), 77),  # EX_NOPERM
        (MfaTimeoutError("mfa_exit_interval watchdog fired"), 75),  # EX_TEMPFAIL
        (AgentClientError("socket closed"), 69),  # EX_UNAVAILABLE
        (LoginError("login stalled"), 69),  # EX_UNAVAILABLE, gitea #60 gap 3
    ],
)
def test_run_exit_code_matches_exception_contract(
    tmp_path, monkeypatch, exc, expected_exit_code
):
    app_dir = tmp_path / "app"
    monkeypatch.setenv("IBC_APP_DIR", str(app_dir))

    async def fake_run_async(
        config_dir, log_dir, *, dotenv_path=None, cli_overrides=None
    ):
        raise exc

    monkeypatch.setattr(_main, "run_async", fake_run_async)

    result = runner.invoke(_cli.app, ["run"])

    assert result.exit_code == expected_exit_code, result.output
    assert f"error: {exc}" in result.output


def test_run_exit_code_is_1_for_an_unhandled_exception(tmp_path, monkeypatch):
    """Not in `cli._OPERATIONAL_ERRORS` -- a genuine bug, shown as a
    traceback rather than a clean one-line message (see `run`'s own
    docstring/comments), and reported as exit 1."""
    app_dir = tmp_path / "app"
    monkeypatch.setenv("IBC_APP_DIR", str(app_dir))

    async def fake_run_async(
        config_dir, log_dir, *, dotenv_path=None, cli_overrides=None
    ):
        raise ValueError("this is a bug, not an operational failure")

    monkeypatch.setattr(_main, "run_async", fake_run_async)

    result = runner.invoke(_cli.app, ["run"])

    assert result.exit_code == 1, result.output


def test_run_field_flags_become_cli_overrides(tmp_path, monkeypatch):
    """--trading-mode/--tws-path/--tws-channel/--program/--tws-settings-path/
    --instance reach run_async as cli_overrides, unset ones dropped."""
    app_dir = tmp_path / "app"
    monkeypatch.setenv("IBC_APP_DIR", str(app_dir))
    captured = {}

    async def fake_run_async(
        config_dir, log_dir, *, dotenv_path=None, cli_overrides=None
    ):
        captured.update(cli_overrides or {})
        return ShutdownCause.REQUESTED

    monkeypatch.setattr(_main, "run_async", fake_run_async)

    result = runner.invoke(
        _cli.app,
        [
            "run",
            "--trading-mode=live",
            "--tws-path=/opt/tws",
            "--tws-channel=latest",
            "--program=tws",
            "--tws-settings-path=/opt/tws-settings",
            "--instance=custom",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured == {
        "trading_mode": "live",
        "tws_path": "/opt/tws",
        "tws_channel": "latest",
        "program": "tws",
        "tws_settings_path": "/opt/tws-settings",
        "instance": "custom",
    }


def test_run_without_flags_produces_no_cli_overrides(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    monkeypatch.setenv("IBC_APP_DIR", str(app_dir))
    captured = {}

    async def fake_run_async(
        config_dir, log_dir, *, dotenv_path=None, cli_overrides=None
    ):
        captured.update(cli_overrides or {})
        return ShutdownCause.REQUESTED

    monkeypatch.setattr(_main, "run_async", fake_run_async)

    result = runner.invoke(_cli.app, ["run"])

    assert result.exit_code == 0, result.output
    assert captured == {}


def test_run_app_dir_flag_wins_over_env_var(tmp_path, monkeypatch):
    env_app_dir = tmp_path / "env-app"
    flag_app_dir = tmp_path / "flag-app"
    monkeypatch.setenv("IBC_APP_DIR", str(env_app_dir))
    for key in _BASE_ENV:
        monkeypatch.delenv(key, raising=False)

    result = runner.invoke(_cli.app, ["run", f"--app-dir={flag_app_dir}"])

    assert result.exit_code == 78  # EX_CONFIG
    # the scaffold ran before load_config failed on missing credentials, same as
    # test_run_without_config_auto_scaffolds_then_fails_clearly
    assert (flag_app_dir / "config" / "ibcontroller.toml").exists()
    assert not (env_app_dir / "config").exists()


def test_init_app_dir_flag_wins_over_env_var(tmp_path, monkeypatch):
    env_app_dir = tmp_path / "env-app"
    flag_app_dir = tmp_path / "flag-app"
    monkeypatch.setenv("IBC_APP_DIR", str(env_app_dir))

    result = runner.invoke(_cli.app, ["init", f"--app-dir={flag_app_dir}"])

    assert result.exit_code == 0, result.output
    assert (flag_app_dir / "config" / "ibcontroller.toml").exists()
    assert not (env_app_dir / "config").exists()


def test_version_prints_something():
    result = runner.invoke(_cli.app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip()
