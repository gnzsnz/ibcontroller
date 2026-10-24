"""ibcontroller's single-instance runner -- everything `cli.py`'s `run`/`init` commands
need beyond argument parsing, which is deliberately kept there (this module has no
`typer` dependency, so it stays directly testable/runnable without going through a CLI
invocation -- `python -m ibcontroller.main` still works).

**Config-dir scaffolding (`ensure_config_scaffold`)** Two bundled
templates ship under `ibcontroller/data/` (`ibcontroller.toml.example`,
`labels.json.example`) and get copied into `config_dir` the first time each is
missing -- but only `ibcontroller.toml` is copied under its *real* filename. The other
keeps its `.example` suffix: `labels.load_labels(config_dir=...)` applies `{config_dir}/
labels.json` *unconditionally* the moment that exact filename exists -- auto-creating
it live would be exactly the silent behavior change. A user must deliberately rename it
to opt in.

`ibcontroller.toml.example` itself ships with every key commented out, so copying it
as `ibcontroller.toml` changes nothing versus built-in defaults -- `run_async` still
calls the real `load_config`, which still raises a clear `ConfigError` for the fields
that have no default (`trading_mode`/`tws_version`/credentials), guiding a first run
rather than silently starting with meaningless values.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import signal
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import anyio.to_thread

from ibcontroller.app_dirs import resolve_app_dirs
from ibcontroller.config import (
    ConfigError,
    DeclarativeInput,
    load_config,
    load_declarative_input,
)
from ibcontroller.control_loop import ShutdownCause, run_control_loop
from ibcontroller.dialogs import (
    DialogCatalog,
    DialogChoice,
    load_catalog,
    parse_custom_dialogs,
    resolve_dialogs,
)
from ibcontroller.labels import load_labels
from ibcontroller.launcher import LauncherError
from ibcontroller.settings import (
    SettingCatalog,
    SettingChoice,
    load_setting_catalog,
    parse_custom_settings,
    resolve_settings,
)

logger = logging.getLogger("ibcontroller.main")

# dest filename in config_dir -> bundled resource name under ibcontroller/data/. Only
# "ibcontroller.toml" is copied under its real, load-bearing name -- see module
# docstring for why the other deliberately keeps its ".example" suffix.
_SCAFFOLD_FILES: dict[str, str] = {
    "ibcontroller.toml": "ibcontroller.toml.example",
    "labels.json.example": "labels.json.example",
}


def _resolve_choices(
    dialog_catalog: DialogCatalog,
    setting_catalog: SettingCatalog,
    declarative: DeclarativeInput,
) -> tuple[list[DialogChoice], list[SettingChoice]]:
    """Parses the user's `[[dialog]]` and `[[setting]]` entries and picks a mode
    per dialog and a checked value per setting. Runs at startup so a bad value
    stops the run before launch. `[ibkr]` is one namespace: a name shared by a
    dialog and a setting is an error, a name that is neither is a warning, and so
    is a custom setting with no value."""
    source = "ibcontroller.toml"
    custom_dialogs = parse_custom_dialogs(declarative.dialogs, source=source)
    custom_settings = parse_custom_settings(declarative.settings, source=source)
    dialog_names = {e.name for e in [*dialog_catalog.dialog, *custom_dialogs]}
    setting_names = {e.name for e in [*setting_catalog.setting, *custom_settings]}
    if clash := sorted(dialog_names & setting_names):
        raise ConfigError(f"names used by both a dialog and a setting: {clash}")
    if unknown := sorted(set(declarative.ibkr) - dialog_names - setting_names):
        logger.warning(
            "IBController > [ibkr] names no setting or dialog, ignored: %s",
            ", ".join(unknown),
        )
    for entry in custom_settings:
        if entry.name not in declarative.ibkr:
            logger.warning(
                "IBController > custom setting %s has no value, left untouched: "
                "set %s in [ibkr] or IBKR_%s",
                entry.name,
                entry.name,
                entry.name.upper(),
            )
    dialogs = resolve_dialogs(
        dialog_catalog, custom_dialogs, declarative.ibkr, declarative.ibkr_sources
    )
    settings = resolve_settings(
        setting_catalog, custom_settings, declarative.ibkr, declarative.ibkr_sources
    )
    return dialogs, settings


def _load_jar_path() -> Path:
    """Resolves the bundled Java agent jar's real path, failing loudly (not
    later, opaquely, inside `launch_instance`) if it isn't actually there --
    e.g. a source checkout where `make` was never run, or a wheel built without
    the jar present at build time (see the Makefile's `dist` target)."""

    resource = resources.files("ibcontroller").joinpath("ibcontroller-agent.jar")
    jar_path = Path(str(resource))
    if not jar_path.is_file():
        raise LauncherError(
            f"ibcontroller-agent.jar not found at {jar_path} -- build it first "
            "(`make` from the project root) before running from a source checkout, "
            "or reinstall the package if this is a wheel install."
        )
    return jar_path


def ensure_config_scaffold(
    config_dir: Path, log_dir: Path, *, force: bool = False
) -> list[Path]:
    """Creates `config_dir`/`log_dir` if missing, then copies each of
    `_SCAFFOLD_FILES`'s bundled templates into `config_dir`, one file at a time, only
    when the destination doesn't already exist (or unconditionally when `force=True`,
    e.g. after an ibcontroller upgrade that changed the bundled examples). Returns the
    list of files actually written, so a caller (`cli.py`'s `init`/`run`) can report
    exactly what happened rather than silently overwriting or silently doing nothing."""

    config_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for dest_name, resource_name in _SCAFFOLD_FILES.items():
        dest = config_dir / dest_name
        if dest.exists() and not force:
            continue
        template = resources.files("ibcontroller").joinpath("data", resource_name)
        dest.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(dest)
    return written


async def run_async(
    config_dir: Path,
    log_dir: Path,
    *,
    dotenv_path: Path | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> ShutdownCause:
    """Loads config/labels for the one instance described by `{config_dir}/
    ibcontroller.toml`, then runs it to completion via `control_loop.run_control_loop`.

    Scaffolds `config_dir` first (non-forced -- see `ensure_config_scaffold`), so a
    fresh install's first `run` produces the same starter files `init` would, then
    fails with a clear `ConfigError` (not a `FileNotFoundError`) if the required fields
    are still unset.

    Labels load once, here, with `config_dir`, so a user's `{config_dir}/labels.json`
    override applies; `run_control_loop` requires them rather than loading its own.

    `cli_overrides`, if given, is passed straight through to `load_config` -- the
    per-invocation `Config` field overrides `cli.py`'s `run` command builds from its
    own `--trading-mode`/`--tws-path`/`--tws-settings-path`/`--instance` options.

    Ctrl-C (`SIGINT`) and `SIGTERM` both cancel the running task -- the graceful-stop
    contract `control_loop.py`'s own docstring already documents ("cancel this
    coroutine's own task to request a graceful stop... a plain Ctrl-C in a CLI"),
    wired up here for the first time. POSIX only (`loop.add_signal_handler`), matching
    this project's current platform scope."""

    # Startup file I/O (scaffold, config, labels, jar lookup) runs off the event loop.
    await anyio.to_thread.run_sync(ensure_config_scaffold, config_dir, log_dir)
    config = await anyio.to_thread.run_sync(
        functools.partial(
            load_config,
            config_dir=config_dir,
            log_dir=log_dir,
            dotenv_path=dotenv_path,
            cli_overrides=cli_overrides,
        )
    )
    # after load_config, which loads the dotenv; validates the file's structure
    declarative = await anyio.to_thread.run_sync(load_declarative_input, config_dir)
    labels = await anyio.to_thread.run_sync(
        functools.partial(load_labels, config_dir=config_dir)
    )
    dialog_catalog = await anyio.to_thread.run_sync(load_catalog)
    setting_catalog = await anyio.to_thread.run_sync(load_setting_catalog)
    dialogs, settings = _resolve_choices(dialog_catalog, setting_catalog, declarative)
    agent_jar_path = await anyio.to_thread.run_sync(_load_jar_path)

    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(
        run_control_loop(
            config, agent_jar_path, labels=labels, dialogs=dialogs, settings=settings
        )
    )
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    try:
        return await task
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.remove_signal_handler(sig)


def main() -> None:
    """Thin synchronous wrapper for direct invocation (`python -m ibcontroller.main`).
    `cli.py` (the package's real installed entry point, `ibcontroller` on `PATH`) calls
    `run_async` directly instead, so it can catch `ConfigError` and report it as a clean
    CLI error rather than a traceback."""
    config_dir, log_dir = resolve_app_dirs()
    asyncio.run(run_async(config_dir, log_dir))


if __name__ == "__main__":
    main()
