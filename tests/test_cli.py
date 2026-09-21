"""cli.py -- the installed `ibcontroller` command. Thin: argument parsing plus mapping
`main.py`'s exceptions to exit codes and human-readable messages. Runs through
`typer.testing.CliRunner` (in-process, no subprocess) against a tmp_path-scoped
`IBC_APP_DIR`, same isolation pattern as `tests/test_trace_integration.py` --
never touches the real platformdirs locations.
"""

from __future__ import annotations

from typer.testing import CliRunner

from ibcontroller import cli as _cli
from ibcontroller import main as _main
from ibcontroller.control_loop import ShutdownCause

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

    assert result.exit_code == 1
    assert "credentials not found" in result.output
    # the scaffold ran before load_config failed, same as run_async's own contract
    assert (app_dir / "config" / "ibcontroller.toml").exists()


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

    assert result.exit_code == 1
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
