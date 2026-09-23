"""L1: launches and supervises TWS/Gateway.

Resolves everything needed to start TWS or IB Gateway (install location, JRE,
classpath, JVM options) and spawns the agent-embedded process.

Split into two halves with different testing needs: `build_launch_plan` is
pure, filesystem-only logic (classpath assembly, JRE discovery, `.vmoptions`/
`Info.plist`/`i4jparams.conf` parsing), testable against a synthetic directory
tree. `launch_instance` is the async half that spawns the process and waits
for it to answer a real `ping`.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import platform
import re
from dataclasses import dataclass
from pathlib import Path

import anyio.to_thread

from ibcontroller.actions import navigate_menu
from ibcontroller.agent_client import (
    AgentClientError,
    AgentCommandConnection,
    AgentEventConnection,
)
from ibcontroller.app_dirs import resolve_runtime_dir
from ibcontroller.config import Config
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import ShutdownLabels
from ibcontroller.logging_setup import (
    configure_gateway_stdout,
    configure_logging,
    configure_trace,
)

logger = logging.getLogger(__name__)

_INSTALL4J_DPROPS_STATIC = [
    "-Dtwslaunch.autoupdate.serviceImpl=com.ib.tws.twslaunch.install4j.Install4jAutoUpdateService",
    "-Dexe4j.isInstall4j=true",
    "-Dinstall4jType=standalone",
]
_ENTRY_CLASSES = {
    "gateway": "ibgateway.GWClient",
    "tws": "jclient.LoginFrame",
}

_MACOS_PROGRAM_NAMES = {
    "gateway": "IB Gateway",
    "tws": "Trader Workstation",
}


class LauncherError(Exception):
    """Anything about assembling or launching the agent process -- a missing
    install, no JRE found, or the agent never becoming ready."""


@dataclass(frozen=True)
class LaunchPlan:
    """Everything `build_launch_plan` resolves, before anything is actually
    spawned -- kept as its own value so the pure, filesystem-only resolution
    can be tested and inspected without spawning a real process."""

    java_bin: Path
    command: list[str]
    command_socket_path: str
    event_socket_path: str
    settings_dir: str


@dataclass(frozen=True)
class LaunchedInstance:
    """What `launch_instance` hands back. `clean_shutdown` and ad hoc callers
    build `send_command` calls against `dispatcher.command_conn` directly, so
    there's no separate `command_conn` field here to keep in sync with it."""

    process: asyncio.subprocess.Process
    dispatcher: Dispatcher
    command_socket_path: str
    event_socket_path: str
    settings_dir: str
    stdout_drain_task: asyncio.Task[None]


def _os_name() -> str:
    return "macos" if platform.system() == "Darwin" else "linux"


def _resolve_tws_path(config: Config, os_name: str) -> Path:
    if config.tws_path:
        return Path(config.tws_path).expanduser()
    return Path.home() / ("Applications" if os_name == "macos" else "Jts")


def resolve_tws_settings_path(config: Config) -> Path:
    """Where TWS/Gateway itself stores its settings -- IBC's own
    `TWS_SETTINGS_PATH` concept, deliberately kept separate from
    `_resolve_tws_path`'s install location. Instances that share a settings
    directory collide on state, since some files TWS creates while running
    aren't separated by username and only one instance can access them at a
    time.

    Unlike IBC's own default (a shared install-folder settings directory
    unless a user opts into `TWS_SETTINGS_PATH`), ibcontroller always
    defaults to a separate per-instance directory, `~/Jts/{instance}` --
    running live and paper concurrently is this project's normal use case,
    not an edge case to opt into safety for."""
    if config.tws_settings_path:
        return Path(config.tws_settings_path).expanduser()
    return Path.home() / "Jts" / config.instance


def _ini_section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    """The line-index range (start, end), exclusive of the header itself, of
    `section`'s own lines -- `None` if the section doesn't exist at all.
    Matches IBC's own `JtsIniManager.getSettingIndex`'s section-scoping."""
    try:
        start = lines.index(section) + 1
    except ValueError:
        return None
    end = start
    while end < len(lines) and not lines[end].startswith("["):
        end += 1
    return start, end


def _ensure_ini_settings(
    lines: list[str], section: str, settings: list[tuple[str, str, bool]]
) -> tuple[list[str], bool]:
    """`settings` is `(key, expected_value, overwrite_if_different)` triples.
    A missing key is appended to the section (creating the section if it
    doesn't exist yet); an existing key with the wrong value is only
    overwritten if `overwrite_if_different` is true, so a value a deployment
    set deliberately is never silently reverted. Returns the possibly-
    modified lines and whether anything actually changed, so the caller only
    rewrites the file when needed."""
    lines = list(lines)
    changed = False
    bounds = _ini_section_bounds(lines, section)
    if bounds is None:
        lines.append(section)
        bounds = (len(lines), len(lines))
    start, end = bounds

    to_append = []
    for key, expected, overwrite in settings:
        prefix = f"{key}="
        found = next(
            (i for i in range(start, end) if lines[i].startswith(prefix)), None
        )
        if found is None:
            to_append.append(f"{key}={expected}")
            changed = True
        elif overwrite and lines[found] != prefix + expected:
            lines[found] = prefix + expected
            changed = True

    if to_append:
        lines[end:end] = to_append
    return lines, changed


def _ensure_jts_ini(settings_dir: Path, *, is_gateway: bool) -> None:
    """Ensures `jts.ini` contains a known-good minimal set of settings
    *before* Gateway/TWS ever starts, avoiding the "Use SSL encryption"
    dialog (and a Locale/proxy-message quirk) entirely rather than reacting
    to a dialog that may not even appear consistently.

    Sets `UseSSL=true` under `[Logon]` (without it, TWS/Gateway shows a
    dialog offering to restart using SSL or close the program), plus
    `Locale=en` (force English regardless of OS locale) and
    `displayedproxymsg=1` (suppress an unrelated proxy-access dialog).
    `ApiOnly=true` under `[IBGateway]` is Gateway-specific: without it,
    Gateway shows a login form with a structure this project doesn't expect
    and can't find the trading mode selector in.

    Deliberately does not set `TrustedIPs`/`LocalServerPort` -- ibcontroller
    has no config field for either yet, and a settings-file line it can't
    populate correctly would be worse than not adding it at all."""
    path = settings_dir / "jts.ini"
    lines = path.read_text().splitlines() if path.is_file() else []

    lines, changed = _ensure_ini_settings(
        lines,
        "[Logon]",
        [
            ("s3store", "true", False),
            ("Locale", "en", True),
            ("displayedproxymsg", "1", True),
            ("UseSSL", "true", True),
        ],
    )
    if is_gateway:
        lines, gw_changed = _ensure_ini_settings(
            lines, "[IBGateway]", [("ApiOnly", "true", True)]
        )
        changed = changed or gw_changed

    if changed or not path.is_file():
        path.write_text("\n".join(lines) + "\n")


def _program_path(program: str, os_name: str, tws_path: Path, tws_version: str) -> Path:
    """Resolves `program`'s install directory for `tws_version` under
    `tws_path`. `program` and `tws_version` are plain parameters, not read
    from `config`, so `_resolve_program_path`'s fallback can derive the
    *other* program's path without building a second `Config` -- keeps this
    function pure and testable against plain strings."""
    program = program.lower()
    if os_name == "macos":
        name = _MACOS_PROGRAM_NAMES[program]
        return tws_path / f"{name} {tws_version}"
    if program == "gateway":
        return tws_path / "ibgateway" / tws_version
    return tws_path / tws_version


def _resolve_program_path(
    program: str, os_name: str, tws_path: Path, tws_version: str
) -> tuple[Path, str]:
    """Resolves `program`'s install directory. A TWS request whose install
    has no `jars/` directory falls back to the same-version Gateway install
    instead, with the TWS entry class (`jclient.LoginFrame`) unchanged -- the
    Gateway distribution's jars carry both entry classes. Only the
    TWS->Gateway direction falls back; a Gateway request never falls back to
    a TWS install.

    Returns `(install_dir, resolved_program)` -- the second value is the
    *actual* program of the resolved install (`"gateway"` when a TWS request
    fell back), which the caller uses to pick the right `.vmoptions` file
    (`ibgateway.vmoptions`, not `tws.vmoptions`, in a Gateway dir)."""
    requested_program = program.lower()
    program_path = _program_path(requested_program, os_name, tws_path, tws_version)
    if requested_program == "tws" and not (program_path / "jars").is_dir():
        return _program_path("gateway", os_name, tws_path, tws_version), "gateway"
    return program_path, requested_program


def _scan_install_dirs(
    tws_path: Path, os_name: str, program: str
) -> list[tuple[str, Path]]:
    """Every installed version of `program` found under `tws_path`, verified
    by a `jars/` subdirectory. Layout differs by OS and program: TWS lives at
    `Trader Workstation *` (macOS) or as top-level directories excluding
    `ibgateway/` (Linux); Gateway lives at `IB Gateway *` (macOS) or under
    `ibgateway/` (Linux). Returns `(version, path)` pairs. `program`'s own
    tree only -- `_list_version_candidates` calls this twice for a TWS
    request, once for its own tree and once for the Gateway tree as
    fallback."""
    candidates: list[tuple[str, Path]] = []
    if os_name == "macos":
        name = _MACOS_PROGRAM_NAMES[program]
        for entry in sorted(tws_path.glob(f"{name} *")):
            if entry.is_dir() and (entry / "jars").is_dir():
                candidates.append((entry.name.removeprefix(f"{name} "), entry))
    elif program == "gateway":
        gw_dir = tws_path / "ibgateway"
        if gw_dir.is_dir():
            for entry in sorted(gw_dir.iterdir()):
                if entry.is_dir() and (entry / "jars").is_dir():
                    candidates.append((entry.name, entry))
    elif tws_path.is_dir():
        for entry in sorted(tws_path.iterdir()):
            if (
                entry.name != "ibgateway"
                and entry.is_dir()
                and (entry / "jars").is_dir()
            ):
                candidates.append((entry.name, entry))
    return candidates


def _list_version_candidates(
    tws_path: Path, os_name: str, program: str
) -> list[tuple[str, Path]]:
    """Every installed version found under `tws_path`, verified by a `jars/`
    subdirectory -- the same check `_build_classpath` itself makes, so a
    candidate this returns is guaranteed launchable. Returns `(version,
    program_path)` pairs, sorted by directory name per tree for a
    deterministic iteration order (not by version -- `_detect_tws_version`
    does that numerically once it has the final candidate list).

    Shares `_resolve_program_path`'s TWS->Gateway fallback trigger: when a
    TWS request finds no TWS install at all, the Gateway installs become the
    candidate pool. `tws_channel`'s filter is applied afterwards, in
    `_detect_tws_version`, uniformly across whichever pool -- a channel
    mismatch is a config error surfaced by that filter, never silently
    relaxed."""
    candidates = _scan_install_dirs(tws_path, os_name, program)
    if candidates:
        return candidates
    if program == "tws":
        return _scan_install_dirs(tws_path, os_name, "gateway")
    return candidates


def _version_sort_key(version: str) -> tuple[int, ...]:
    """Numeric comparison, not lexical -- handles both macOS's dotted `"10.50"`
    and the Windows/Linux single-integer `"1050"` style without assuming
    either format; extracts every digit run and compares them as a tuple of
    ints (`"10.50"` -> `(10, 50)`, `"1050"` -> `(1050,)`)."""
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _read_i4j_variable(install4j_dir: Path, name: str) -> str | None:
    """A plain install4j `<variable name="{name}" value="..." />` entry --
    distinct from `_read_i4j_vmoptions`'s own handling of the much larger
    `javaOptions` variable's own value blob (two different shapes in the same
    `i4jparams.conf` file). Used for `channel`, which each installed version
    carries its own real value for, not a fixed label."""
    conf = install4j_dir / "i4jparams.conf"
    if not conf.is_file():
        return None
    match = re.search(rf'name="{re.escape(name)}" value="([^"]*)"', conf.read_text())
    return match.group(1) if match else None


def _candidates_for_channel(
    tws_path: Path, os_name: str, program: str, channel: str | None
) -> list[tuple[str, Path]]:
    """`_list_version_candidates`'s output, filtered by `channel` -- separated
    so `_detect_tws_version` can retry a TWS request against the Gateway pool
    after the TWS pool comes up empty under the filter. A TWS install present
    but on the wrong channel must not short-circuit that fallback."""
    candidates = _list_version_candidates(tws_path, os_name, program)
    if channel is not None:
        candidates = [
            (version, path)
            for version, path in candidates
            if _read_i4j_variable(path / ".install4j", "channel") == channel
        ]
    return candidates


def _detect_tws_version(
    tws_path: Path, os_name: str, program: str, channel: str | None
) -> str:
    """Auto-detects the installed version of `program` under `tws_path`,
    optionally filtered by `channel` -- a real desktop install can carry
    several versions side by side, each on its own update channel, and
    `jars/` alone can't tell them apart.

    `channel`, if given, filters candidates by their own
    `_read_i4j_variable(..., "channel")` first. When the requested-program
    pool filters down to nothing, a TWS request retries against the Gateway
    pool: the filter still applies uniformly there, so a channel mismatch is
    still an error, but an empty TWS pool is not -- that's exactly when the
    fallback should engage. Whatever's left (or the unfiltered list, if
    `channel` is `None`) is resolved by picking the greatest version
    numerically, logged clearly either way so it's never a silent choice.

    Raises `LauncherError` when no candidate remains."""
    candidates = _candidates_for_channel(tws_path, os_name, program, channel)
    if not candidates and program == "tws":
        candidates = _candidates_for_channel(tws_path, os_name, "gateway", channel)

    if not candidates:
        where = f"channel={channel!r} under {tws_path}" if channel else str(tws_path)
        raise LauncherError(
            f"no {program} installation found ({where}) -- set tws_version "
            "explicitly, or check tws_path/tws_channel"
        )

    if len(candidates) == 1:
        version, _ = candidates[0]
        logger.info("IBController > auto-detected %s version: %s", program, version)
        return version

    chosen_version, _ = max(candidates, key=lambda vp: _version_sort_key(vp[0]))
    logger.info(
        "IBController > multiple %s installations found under %s (%s)%s -- using %s "
        "(the greatest)",
        program,
        tws_path,
        ", ".join(version for version, _ in candidates),
        f", channel={channel!r}" if channel else "",
        chosen_version,
    )
    return chosen_version


def _build_classpath(program_path: Path, install4j_dir: Path, agent_jar: Path) -> str:
    jars_dir = program_path / "jars"
    if not jars_dir.is_dir():
        raise LauncherError(
            f"IBController > {jars_dir} not found -- is it installed under "
            "{program_path.parent}?"
        )
    jars = sorted(str(p) for p in jars_dir.glob("*.jar"))
    jars.append(str(install4j_dir / "i4jruntime.jar"))
    jars.append(str(agent_jar))
    return ":".join(jars)


def _find_java_bin(os_name: str, install4j_dir: Path, program_path: Path) -> Path:
    """Two genuinely different cascades, not one path with a branch, for
    locating the bundled java executable per OS."""
    if os_name == "macos":
        candidate = (
            install4j_dir / "jre.bundle" / "Contents" / "Home" / "jre" / "bin" / "java"
        )
        if candidate.is_file():
            return candidate
        candidate = install4j_dir / "jre.bundle" / "Contents" / "Home" / "bin" / "java"
        if candidate.is_file():
            return candidate
    else:
        for cfg_name in ("pref_jre.cfg", "inst_jre.cfg"):
            cfg = install4j_dir / cfg_name
            if cfg.is_file():
                candidate = Path(cfg.read_text().strip()) / "bin" / "java"
                if candidate.is_file():
                    return candidate
        candidate = program_path / "jre" / "bin" / "java"
        if candidate.is_file():
            return candidate
    raise LauncherError(
        f"no bundled java executable found (looked under {install4j_dir})"
    )


def _read_vmoptions_file(path: Path) -> list[str]:
    """Skips comments and `-D` lines -- `-D` properties are added explicitly
    instead, see `_INSTALL4J_DPROPS_STATIC`."""
    if not path.is_file():
        return []
    options = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-D"):
            continue
        options.append(line)
    return options


def _read_i4j_vmoptions(install4j_dir: Path) -> list[str]:
    """`--add-opens`/`--add-exports` (JPMS strong encapsulation), the real
    XML-parser `-D` fixes (e.g. Gateway's `-Djdk.xml.elementAttributeLimit`),
    and `-DjxBrowserKey` all live in install4j's own `javaOptions` variable
    inside `i4jparams.conf`, not in `tws.vmoptions`/`ibgateway.vmoptions` --
    true on both Linux and macOS (checked directly against IBC's own
    `ibcstart.sh`, which reads this same variable, from this same
    `{program_path}/.install4j` location, on both platforms uniformly, and
    never touches macOS's `Info.plist` at all). `-D` tokens are kept here
    rather than filtered -- this is the only place some installs carry them,
    so filtering would silently drop real options."""
    raw = _read_i4j_variable(install4j_dir, "javaOptions")
    if not raw:
        return []
    return [opt for opt in raw.split() if "${" not in opt]


def _prevent_native_restart(program_path: Path) -> Path:
    """Renames the `.app` bundle so Gateway/TWS's own restart logic can't
    find and re-invoke its native launcher stub
    (`<name>.app/Contents/MacOS/JavaApplicationStub`) directly. Left alone,
    a scheduled restart relaunches Gateway that way, completely outside
    `launcher.py`, with no agent embedded -- ibcontroller silently loses
    control of the instance on every restart. Renaming the bundle breaks
    whatever internal reference Gateway's own restart logic uses to find and
    re-invoke it, so only an explicit relaunch through this module can bring
    it back.

    Idempotent: a second call after the rename already happened is a no-op,
    returning the same renamed path.

    macOS only, called only under `os_name == "macos"` in `build_launch_plan`
    -- see `_prevent_native_restart_linux` for the Linux equivalent, called
    under the `else` branch there."""
    original = program_path / f"{program_path.name}.app"
    renamed = program_path / f"{program_path.name}-1.app"
    if renamed.is_dir():
        return renamed
    if original.is_dir():
        original.rename(renamed)
        logger.warning(
            "renamed %s -> %s to prevent Gateway's own restart logic from "
            "relaunching itself outside ibcontroller's control",
            original,
            renamed,
        )
        return renamed
    return original


def _prevent_native_restart_linux(program_path: Path, script_name: str) -> Path:
    """`_prevent_native_restart`'s Linux equivalent -- renames the
    install4j-generated native launch script (`ibgateway`/`tws`, a plain
    shell script sitting directly in `program_path`) instead of a `.app`
    bundle, for the same reason: left alone, Gateway/TWS's own
    scheduled-restart logic invokes that script directly to relaunch itself,
    completely outside `launcher.py`, with no agent embedded -- ibcontroller
    loses control of the instance on every restart. Renaming the script
    breaks whatever internal reference that restart logic uses to find and
    re-invoke it, so only an explicit relaunch through this module can bring
    the instance back.

    Idempotent, matching `_prevent_native_restart`: a second call after the
    rename already happened is a no-op, returning the same renamed path.
    The return value doesn't need to be read back by a vmoptions reader --
    `_read_i4j_vmoptions` reads from `.install4j/i4jparams.conf`, which
    doesn't move when this script is renamed -- so callers only need this
    for its side effect."""
    original = program_path / script_name
    renamed = program_path / f"{script_name}-1"
    if renamed.exists():
        return renamed
    if original.exists():
        original.rename(renamed)
        logger.warning(
            "renamed %s -> %s to prevent %s's own restart logic from "
            "relaunching itself outside ibcontroller's control",
            original,
            renamed,
            script_name,
        )
        return renamed
    return original


def build_launch_plan(
    config: Config,
    agent_jar: str | Path,
    *,
    os_name: str | None = None,
    runtime_dir: str | Path | None = None,
    restart_hash: str | None = None,
) -> LaunchPlan:
    """The pure, filesystem-only half of launching -- resolves everything
    needed to spawn the agent process (install location, JRE, classpath, JVM
    options), without spawning anything.

    `runtime_dir` is where the agent's own command/event sockets are created --
    resolved via `app_dirs.resolve_runtime_dir()` (platformdirs) unless a
    caller overrides it (tests do, to avoid depending on the real
    OS-specific runtime location). Java never derives this path itself --
    `AgentMain` takes the command-socket path as a plain CLI argument -- so
    this is the one place the convention lives.

    `restart_hash`, when given (`login.find_autorestart_hash`'s return
    value), adds `-Drestart=<hash>` to the command -- lets Gateway resume the
    session after a scheduled restart without a full 2FA prompt."""
    os_name = os_name or _os_name()
    sock_dir = Path(runtime_dir) if runtime_dir is not None else resolve_runtime_dir()
    sock_dir.mkdir(parents=True, exist_ok=True)
    tws_path = _resolve_tws_path(config, os_name)
    settings_dir = resolve_tws_settings_path(config)
    settings_dir.mkdir(parents=True, exist_ok=True)
    _ensure_jts_ini(settings_dir, is_gateway=config.program.lower() == "gateway")
    tws_version = config.tws_version or _detect_tws_version(
        tws_path, os_name, config.program.lower(), config.tws_channel
    )
    program_path, resolved_program = _resolve_program_path(
        config.program.lower(), os_name, tws_path, tws_version
    )
    if resolved_program != config.program.lower():
        logger.warning(
            "IBController > no %s installation found (tws_version=%s, tws_path=%s) -- "
            "falling back to the %s installation %s and running it as %s "
            "(IBC's own ibcstart.sh fallback)",
            config.program,
            tws_version,
            tws_path,
            resolved_program,
            program_path,
            config.program,
        )
    install4j_dir = program_path / ".install4j"
    classpath = _build_classpath(program_path, install4j_dir, Path(agent_jar))
    java_bin = _find_java_bin(os_name, install4j_dir, program_path)

    program = config.program.lower()
    vmoptions_file = program_path / (
        "ibgateway.vmoptions" if resolved_program == "gateway" else "tws.vmoptions"
    )
    vm_options = _read_vmoptions_file(vmoptions_file)
    vm_options.extend(_INSTALL4J_DPROPS_STATIC)
    if config.java_heap_size:
        # Deterministic override: strip the installed file's -Xmx, don't rely
        # on "last -Xmx wins" JVM behavior.
        vm_options = [opt for opt in vm_options if not opt.startswith("-Xmx")]
        vm_options.append(f"-Xmx{config.java_heap_size}")
    channel = _read_i4j_variable(install4j_dir, "channel") or config.tws_channel
    vm_options.append(f"-Dchannel={channel}")

    if os_name == "macos":
        _prevent_native_restart(program_path)
    else:
        script_name = "ibgateway" if resolved_program == "gateway" else "tws"
        _prevent_native_restart_linux(program_path, script_name)
    vm_options.extend(_read_i4j_vmoptions(install4j_dir))

    # Agent-side logging (java.util.logging, AgentMain.configureLogging, 2026-09-08):
    # its own per-instance file under the same shared log dir Python uses, so the
    # streams (ibcontroller-{instance}.log / gateway-{instance}.log / this file)
    # never share a file.
    vm_options.append(
        f"-Dibcontroller.logfile="
        f"{Path(config.log_dir) / f'ibcontroller-java-agent-{config.instance}.log'}"
    )
    vm_options.append(
        f"-Dibcontroller.log.level={logging.getLevelName(config.log_level)}"
    )

    entry_class = _ENTRY_CLASSES[program]
    command_socket_path = str(
        sock_dir / f"ibcontroller-agent-{config.instance}-cmd.sock"
    )
    event_socket_path = str(
        sock_dir / f"ibcontroller-agent-{config.instance}-events.sock"
    )

    command = [
        str(java_bin),
        *vm_options,
        f"-DjtsConfigDir={settings_dir}",
        *([f"-Drestart={restart_hash}"] if restart_hash is not None else []),
        "-cp",
        classpath,
        "ibcontroller.agent.AgentMain",
        command_socket_path,
        entry_class,
        str(settings_dir),
    ]
    return LaunchPlan(
        java_bin=java_bin,
        command=command,
        command_socket_path=command_socket_path,
        event_socket_path=event_socket_path,
        settings_dir=str(settings_dir),
    )


async def _wait_for_ready(command_socket_path: str, timeout: float) -> None:
    """Matches this project's own L1 framing exactly: "ping succeeding means
    the agent process is up." Retries a real `ping`, not just a raw connect --
    a bare connect succeeding doesn't prove the agent's own accept loop has
    picked the connection up yet (AF_UNIX accepts into the kernel backlog
    regardless), `ping` does."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_error: Exception | None = None
    while loop.time() < deadline:
        probe = AgentCommandConnection(command_socket_path)
        try:
            await probe.connect()
            await probe.ping()
            return
        except (OSError, AgentClientError) as exc:
            last_error = exc
        finally:
            await probe.close()
        await asyncio.sleep(0.2)
    raise LauncherError(
        f"agent at {command_socket_path} did not respond to ping within {timeout}s"
    ) from last_error


async def _drain_stdout(
    process: asyncio.subprocess.Process, stdout_logger: logging.Logger
) -> None:
    """Reads the subprocess's stdout (Gateway's own console/log4j output,
    `stderr` redirected into the same stream) until EOF, i.e. until the
    process itself exits -- see `launch_instance`'s own callout for why this
    exists at all: an unread pipe eventually blocks the writer.

    Each line is handed to `stdout_logger` (built by
    `logging_setup.configure_gateway_stdout`) rather than written to a file
    directly -- the actual write happens on that logger's own listener
    thread, off this coroutine, the same way `configure_logging`/
    `configure_trace` already keep their own writes off the event loop."""
    assert process.stdout is not None
    while True:
        line = await process.stdout.readline()
        if not line:
            return
        stdout_logger.info(line.decode("utf-8", errors="replace").rstrip("\n"))


async def launch_instance(
    config: Config,
    agent_jar: str | Path,
    *,
    ready_timeout: float = 30.0,
    restart_hash: str | None = None,
) -> LaunchedInstance:
    """Becomes the parent of the agent's own JVM process. Waits for a real
    `ping` before returning, then builds and starts the real `Dispatcher`
    callers actually use -- the process is terminated if readiness times out,
    rather than handed back half-working.

    `restart_hash` is passed straight through to `build_launch_plan` -- see
    that function's own docstring for what it does and why.

    `build_launch_plan` itself runs via `anyio.to_thread.run_sync`, not
    called directly -- it's a long, purely synchronous chain of filesystem
    calls, and this runs on every launch *and* every scheduled restart while
    the Dispatcher's own event/command tasks are already live on the same
    loop.

    Configures the `ibcontroller` logging hierarchy first
    (`configure_logging`/`configure_trace`), and the launched process's own
    stdout logger (`configure_gateway_stdout`), so `_drain_stdout` has it
    ready before the process is spawned below. `configure_trace` gates
    purely on the `trace_enabled` toggle; its files (the raw wire trace) and
    `ibcontroller.log` both land under `config.log_dir`.
    """
    configure_logging(
        level=config.log_level,
        log_dir=config.log_dir,
        filename=f"ibcontroller-{config.instance}.log",
        sink=config.log_sink,
    )
    configure_trace(
        instance=config.instance,
        enabled=config.trace_enabled,
        trace_dir=config.log_dir,
    )
    stdout_logger = configure_gateway_stdout(
        config.instance, config.log_dir, sink=config.log_sink
    )
    plan = await anyio.to_thread.run_sync(
        functools.partial(
            build_launch_plan, config, agent_jar, restart_hash=restart_hash
        )
    )
    logger.info(
        "IBController > launching instance %s (%s)",
        config.instance,
        config.trading_mode,
    )
    logger.info(
        "IBController > launch command for %s: %s", config.instance, plan.command
    )
    process = await asyncio.create_subprocess_exec(
        *plan.command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        # New session/process group: a terminal Ctrl-C's SIGINT is delivered to the
        # whole foreground process group. Without this, the JVM child gets it
        # directly, races our own signal handler, and exits via its own
        # Shutdown/Terminator hook (returncode 128+SIGINT) instead of via
        # clean_shutdown's File/Close -- skipping Gateway/TWS's own UI-close path
        # (and its "Shutdown progress" dialog) entirely. This makes Ctrl-C reach
        # the child only through clean_shutdown, same as a real File/Close.
    )
    # Must stay drained for the process's whole life: Gateway logs continuously
    # (log4j, stderr=STDOUT too), and once the OS pipe buffer fills, any Java
    # thread writing to stdout blocks forever. Self-terminates on EOF, no
    # explicit cancellation needed.
    stdout_drain_task = asyncio.ensure_future(_drain_stdout(process, stdout_logger))
    try:
        await _wait_for_ready(plan.command_socket_path, ready_timeout)
    except LauncherError:
        logger.warning("instance %s never became ready, terminating", config.instance)
        process.terminate()
        raise

    cmd_conn = AgentCommandConnection(plan.command_socket_path)
    event_conn = AgentEventConnection(plan.event_socket_path)
    dispatcher = Dispatcher(
        cmd_conn,
        event_conn,
        instance=config.instance,
    )
    await dispatcher.start()
    logger.info(
        "IBController > instance %s ready, pid=%s", config.instance, process.pid
    )
    return LaunchedInstance(
        process=process,
        dispatcher=dispatcher,
        command_socket_path=plan.command_socket_path,
        event_socket_path=plan.event_socket_path,
        settings_dir=plan.settings_dir,
        stdout_drain_task=stdout_drain_task,
    )


async def clean_shutdown(
    launched: LaunchedInstance,
    *,
    program: str,
    logged_in: bool,
    labels: ShutdownLabels,
    timeout: float = 15.0,
) -> None:
    """Shutdown is not a process signal if login has completed -- invokes the
    app's own File -> Exit (TWS) / File -> Close (Gateway) menu item first, so
    it can save session state and upload settings. Only hard-terminates
    directly when login never completed (nothing to save), or as a last
    resort if the clean path hangs.

    `labels` supplies the menu path -- see `labels.ShutdownLabels`'s own
    docstring.

    `logged_in` is the caller's own responsibility to know; this layer
    doesn't track login state itself.

    Uses `navigate_menu`, not `click`: "Close"/"Exit" are menu items, not
    ordinary buttons -- `click`'s `findByAccessibleName` lookup can never
    find them, since a `JMenu`'s dropdown lives in a `JPopupMenu`, outside
    the ordinary component tree while the menu is closed. Goes through
    `actions.navigate_menu` rather than a direct `dispatcher.send_command`
    call, so a shutdown landing while the menu item is momentarily disabled
    still retries instead of failing immediately; a short `timeout` bounds
    that retry so a genuinely stuck menu still falls through to the
    hard-kill fallback promptly.

    Checks `launched.process.returncode` first: a process that already
    exited on its own before this was ever called -- the user manually
    closing Gateway, or a scheduled restart -- has no live command socket
    left to send `navigate_menu` over, and no OS PID left for
    `terminate()` to signal, so the already-exited branch only stops the
    dispatcher. `terminate()` stays reserved for the "never logged in, but
    still running" branch, where the process genuinely is still alive. The
    `except` below is broadened to `OSError` as defense in depth for the
    narrower race of the process dying between the `returncode` check and
    the command actually being sent.
    """
    if launched.process.returncode is not None:
        logger.info(
            "IBController > shutting down (process already exited) -- nothing to "
            "terminate"
        )
        await launched.dispatcher.stop()
        return

    if not logged_in:
        logger.info(
            "IBController > shutting down (never logged in) -- terminating directly"
        )
        launched.process.terminate()
        await launched.dispatcher.stop()
        return

    target = (
        labels.gateway_menu_path
        if program.lower() == "gateway"
        else labels.tws_menu_path
    )
    try:
        await navigate_menu(launched.dispatcher, target, timeout=5.0)
    except (AgentClientError, TimeoutError, OSError):
        pass  # best-effort -- the wait-then-terminate fallback below is what matters

    try:
        await asyncio.wait_for(launched.process.wait(), timeout=timeout)
        logger.info(
            "IBController > shut down cleanly via %s (returncode=%s)",
            target,
            launched.process.returncode,
        )
    except TimeoutError:
        logger.warning(
            "IBController > clean shutdown via %s did not exit within %ss -- "
            "terminating",
            target,
            timeout,
        )
        launched.process.terminate()
    await launched.dispatcher.stop()
