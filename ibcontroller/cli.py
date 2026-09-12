"""ibcontroller CLI entrypoint.  This is the `ibcontroller` command that is installed
by the package.  It is a thin wrapper around `ibcontroller.main`, which does the actual
work of loading config/labels and running the control loop -- this module only parses
arguments, reports what happened in human-readable form, and maps exceptions to exit
codes.
"""

from __future__ import annotations

import asyncio
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

import typer

from ibcontroller import main as _main
from ibcontroller.agent_client import AgentClientError
from ibcontroller.app_dirs import resolve_app_dirs
from ibcontroller.config import ConfigError
from ibcontroller.launcher import LauncherError
from ibcontroller.login import LoginError
from ibcontroller.recognisers import LoginFailedError
from ibcontroller.settings import SettingsError

# Known "operational" failure modes (config mistakes, an install that isn't where
# configured, a real login/settings failure) -- reported as a clean one-line message
# and exit 1, not a traceback. Anything not in this tuple is unexpected and should show
# its full traceback (a real bug report, not a deployment mistake to fix and retry).
_OPERATIONAL_ERRORS = (
    ConfigError,
    RuntimeError,
    LauncherError,
    LoginError,
    LoginFailedError,
    SettingsError,
    AgentClientError,
)

app = typer.Typer(
    no_args_is_help=True,
    help="Login/launch controller for TWS and IBKR Gateway.",
)


@app.command()
def init(
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite existing scaffold files with the bundled templates.",
    ),
) -> None:
    """Scaffold the config directory with starter ibcontroller.toml/
    ibkr_settings.toml.example/labels.json.example files. Never overwrites a file
    that already exists unless --force is given."""
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
            "and set IBCONTROLLER_USERID/IBCONTROLLER_PASSWORD before running "
            "`ibcontroller run`."
        )


@app.command()
def run(
    dotenv: Path | None = typer.Option(  # noqa: B008 -- typer's own idiom
        None,
        "--dotenv",
        help="Optional .env file to load into the environment before reading config "
        "(only fills variables not already set).",
    ),
) -> None:
    """Run one ibcontroller instance until it stops (Ctrl-C for a graceful shutdown,
    or Gateway/TWS exiting or restarting on its own). Scaffolds the config directory
    first if it's missing, same as `ibcontroller init`."""
    config_dir, log_dir = resolve_app_dirs()
    try:
        cause = asyncio.run(_main.run_async(config_dir, log_dir, dotenv_path=dotenv))
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        typer.echo(
            f"\nEdit {config_dir / 'ibcontroller.toml'} (run `ibcontroller init` "
            "first if it doesn't exist yet) and set "
            "IBCONTROLLER_USERID/IBCONTROLLER_PASSWORD.",
            err=True,
        )
        raise typer.Exit(1) from exc
    except _OPERATIONAL_ERRORS as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"stopped: {cause.name}")


@app.command()
def version() -> None:
    """Print the installed ibcontroller version."""
    try:
        typer.echo(_pkg_version("ibcontroller"))
    except PackageNotFoundError:
        typer.echo("unknown (not installed)")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
