"""ibcontroller CLI entrypoint.  This is the `ibcontroller` command that is installed
by the package.  It is a thin wrapper around `ibcontroller.main`, which does the actual
work of loading config/labels and running the control loop -- this module only parses
arguments, reports what happened in human-readable form, and maps exceptions to exit
codes.
"""

from __future__ import annotations

import asyncio
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

import attrs
import typer

from ibcontroller import main as _main
from ibcontroller.app_dirs import resolve_app_dirs
from ibcontroller.config import (
    Config,
    ConfigError,
    MissingCredentialsError,
    Program,
    TradingMode,
)
from ibcontroller.control_loop import ShutdownCause
from ibcontroller.errors import IBControllerError, TransientError
from ibcontroller.labels import LabelsError
from ibcontroller.launcher import LauncherError
from ibcontroller.login import SessionCededError

# A `ShutdownCause` returned by `run_control_loop` without an exception -- checked
# only for causes that can actually reach `run()` (`COLD_RESTART` always loops
# internally, see `control_loop.run_control_loop`'s own docstring).
_CAUSE_EXIT_CODES: dict[ShutdownCause, int] = {
    ShutdownCause.REQUESTED: 0,
    ShutdownCause.TIDY_CLOSEDOWN: 0,
    ShutdownCause.PROCESS_CLOSED: 0,  # Gateway/TWS exited by choice (File>Close)
    ShutdownCause.PROCESS_EXITED: TransientError.exit_code,
    ShutdownCause.CONNECTION_LOST: TransientError.exit_code,
}


def _exit_code_for_exception(exc: Exception) -> int:
    """Exit status for `exc`: the `exit_code` of its group, 1 for anything unlisted."""
    return exc.exit_code if isinstance(exc, IBControllerError) else 1


app = typer.Typer(
    no_args_is_help=True,
    help="Login/launch controller for TWS and IBKR Gateway.",
)

# Real `Config` field names -- source of truth for `_cli_overrides` below, so a
# future field rename is caught immediately (KeyError-shaped, via the `in` check)
# rather than the override silently never reaching `Config`.
_CONFIG_FIELD_NAMES = frozenset(f.name for f in attrs.fields(Config))


def _cli_overrides(**kwargs: Any) -> dict[str, Any]:
    """Builds `load_config`'s `cli_overrides` dict from `run`'s own per-field CLI
    options: drops unset (`None`) options -- a `None` would wrongly override a real
    TOML/env value -- and stringifies `Path` options (typed-settings expects the same
    string shape TOML/env values already arrive in)."""
    return {
        k: (str(v) if isinstance(v, Path) else v)
        for k, v in kwargs.items()
        if v is not None and k in _CONFIG_FIELD_NAMES
    }


@app.command()
def init(
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing scaffold files with the bundled templates.",
    ),
    app_dir: Path | None = typer.Option(  # noqa: B008 -- typer's own idiom
        None,
        "--app-dir",
        help="Override IBC_APP_DIR for this invocation -- config/log/run "
        "all move under {app_dir}/{config,log,run} instead of the platform default.",
    ),
) -> None:
    """Scaffold the config directory with starter ibcontroller.toml/
    ibkr_settings.toml.example/labels.json.example files. Never overwrites a file
    that already exists unless --force is given."""
    if app_dir is not None:
        os.environ["IBC_APP_DIR"] = str(app_dir)
    config_dir, log_dir = resolve_app_dirs()
    written = _main.ensure_config_scaffold(config_dir, log_dir, force=force)

    typer.echo(f"config dir: {config_dir}")
    typer.echo(f"log dir:    {log_dir}")
    if written:
        for path in written:
            typer.echo(f"  wrote {path.name}")
    else:
        typer.echo(
            "  nothing to do -- every scaffold file already exists "
            "(use --force to overwrite)"
        )
    if "ibcontroller.toml" in {p.name for p in written} or force:
        typer.echo(
            f"\nEdit {config_dir / 'ibcontroller.toml'} (set trading_mode/tws_version) "
            "and set IBC_USERID/IBC_PASSWORD before running "
            "`ibcontroller run`."
        )


@app.command()
def run(  # noqa: PLR0913, PLR0917 -- one typer.Option per Config field, not
    # actually 8 callers'-worth of complexity
    dotenv: Path | None = typer.Option(  # noqa: B008 -- typer's own idiom
        None,
        "--dotenv",
        help="Optional .env file to load into the environment before reading config "
        "(only fills variables not already set).",
    ),
    app_dir: Path | None = typer.Option(  # noqa: B008 -- typer's own idiom
        None,
        "--app-dir",
        help="Override IBC_APP_DIR for this invocation -- config/log/run "
        "all move under {app_dir}/{config,log,run} instead of the platform default.",
    ),
    trading_mode: TradingMode | None = typer.Option(  # noqa: B008
        None,
        "--trading-mode",
        help="Override Config.trading_mode ('live'/'paper') for this invocation.",
    ),
    tws_path: Path | None = typer.Option(  # noqa: B008 -- typer's own idiom
        None,
        "--tws-path",
        help="Override Config.tws_path (the TWS/Gateway install-path inference) "
        "for this invocation.",
    ),
    tws_channel: str | None = typer.Option(
        None,
        "--tws-channel",
        help="Override Config.tws_channel ('stable'/'latest', filters install "
        "auto-detection) for this invocation.",
    ),
    program: Program | None = typer.Option(  # noqa: B008
        None,
        "--program",
        case_sensitive=False,
        help="Override Config.program ('gateway'/'tws') for this invocation.",
    ),
    tws_settings_path: Path | None = typer.Option(  # noqa: B008
        None,
        "--tws-settings-path",
        help="Override Config.tws_settings_path (where TWS/Gateway stores its own "
        "settings) for this invocation.",
    ),
    instance: str | None = typer.Option(
        None,
        "--instance",
        help="Override Config.instance (per-instance log/trace/socket names) for "
        "this invocation. Defaults to '{program}-{trading_mode}', so --trading-mode "
        "alone is usually enough to keep paper/live instances apart.",
    ),
) -> None:
    """Run one ibcontroller instance until it stops (Ctrl-C for a graceful shutdown,
    or Gateway/TWS exiting or restarting on its own). Scaffolds the config directory
    first if it's missing, same as `ibcontroller init`.

    `--trading-mode`/`--tws-path`/`--tws-channel`/`--program`/`--tws-settings-path`/
    `--instance` let one
    invocation pick these `Config` fields directly, overriding TOML/env for this run
    only -- e.g. `ibcontroller run --trading-mode=live --dotenv=.env-live` and
    `ibcontroller run --trading-mode=paper --dotenv=.env-paper` run side by side from
    one shared config."""
    if app_dir is not None:
        os.environ["IBC_APP_DIR"] = str(app_dir)
    config_dir, log_dir = resolve_app_dirs()
    cli_overrides = _cli_overrides(
        trading_mode=trading_mode,
        tws_path=tws_path,
        tws_channel=tws_channel,
        program=program,
        tws_settings_path=tws_settings_path,
        instance=instance,
    )
    try:
        cause = asyncio.run(
            _main.run_async(
                config_dir,
                log_dir,
                dotenv_path=dotenv,
                cli_overrides=cli_overrides,
            )
        )
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        # Credentials hint only for the missing-credentials case -- every
        # other ConfigError (bad value, bad TOML) gets a general one instead.
        if isinstance(exc, MissingCredentialsError):
            typer.echo(
                f"\nEdit {config_dir / 'ibcontroller.toml'} (run `ibcontroller init` "
                "first if it doesn't exist yet) and set "
                "IBC_USERID/IBC_PASSWORD.",
                err=True,
            )
        elif isinstance(exc, LabelsError):
            typer.echo(f"\nCheck {config_dir / 'labels.json'}.", err=True)
        elif isinstance(exc, LauncherError):
            pass  # message already names the fix; the config files are not at fault
        else:
            typer.echo(
                f"\nCheck {config_dir / 'ibcontroller.toml'} and the environment "
                "(run `ibcontroller init` first if it doesn't exist yet).",
                err=True,
            )
        raise typer.Exit(_exit_code_for_exception(exc)) from exc
    except SessionCededError as exc:
        typer.echo(f"stopped: SESSION_CEDED ({exc})")
        raise typer.Exit(SessionCededError.exit_code) from exc
    except IBControllerError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(_exit_code_for_exception(exc)) from exc

    typer.echo(f"stopped: {cause.name}")
    raise typer.Exit(_CAUSE_EXIT_CODES.get(cause, 1))


@app.command()
def version() -> None:
    """Print the installed ibcontroller version."""
    try:
        typer.echo(_pkg_version("py-ib-controller"))
    except PackageNotFoundError:
        typer.echo("unknown (not installed)")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
