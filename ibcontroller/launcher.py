"""launcher.py -- L1 (main architecture document, "Process base layer: launch,
flags, supervise"): owns starting TWS/Gateway -- nothing above this layer works if
some other process launches it first. Ports `scripts/launch-agent.sh`'s own
validated logic (that script's own header names itself "Phase 1's stand-in for
Python's future launcher.py") -- same primary sources (IBC's own
`ibcstart.sh`/`gatewaystartmacos.sh`), not re-derived from scratch. Every finding
that script's comments already document carries over unchanged: the four
install4j `-D` properties, the macOS JPMS `Info.plist` gap, the `jxBrowserKey` fix
for Passkey 2FA (CLAUDE.md's Build plan, Phase 1 steps 3 and the Milestone).

One genuine improvement the Python port buys for free, not just a straight port:
macOS's `Info.plist` is read via the stdlib's `plistlib` directly (verified against
the real installed `Info.plist` before relying on it) -- no `plutil`/`jq`
subprocess pair needed, unlike the bash version, which had no native plist parser
available to it at all.

Split deliberately into two halves with different testing needs: `build_launch_plan`
is pure, filesystem-only logic (classpath assembly, JRE discovery, `.vmoptions`/
`Info.plist`/`i4jparams.conf` parsing) -- testable against a synthetic directory
tree, no live Gateway needed, same as every other layer's own unit tests.
`launch_instance` is the async half that actually spawns the process and waits for
it to answer a real `ping` (matching this project's own L1 framing precisely:
"ping succeeding means the agent process is up") -- that half needs a real install
to mean anything, and is live-validated separately, not unit-tested in isolation.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import platform
import plistlib
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
# The channel install4j was built for ("stable"/"latest") -- historically hardcoded to
# "latest" here (matching IBC's own ibcstart.sh, which hardcodes it identically), kept
# only as the fallback now that build_launch_plan reads the real value dynamically per
# install (see _read_i4j_variable) -- confirmed live that stable and latest are
# genuinely different, differently-versioned installs, not just a label.
_DEFAULT_CHANNEL = "latest"

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
    """Everything `build_launch_plan` resolved, before anything is actually
    spawned -- kept as its own value so the pure, filesystem-only resolution can
    be tested (and inspected) without spawning a real process."""

    java_bin: Path
    command: list[str]
    command_socket_path: str
    event_socket_path: str
    settings_dir: str


@dataclass(frozen=True)
class LaunchedInstance:
    """What `launch_instance` hands back -- `clean_shutdown` and ad hoc callers
    build `send_command` calls against `dispatcher.command_conn`, so there's
    no separate `command_conn` field here to keep in sync with it (removed
    2026-09-11, see `Dispatcher.command_conn`'s own docstring)."""

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
        return Path(config.tws_path)
    return Path.home() / ("Applications" if os_name == "macos" else "Jts")


def resolve_tws_settings_path(config: Config) -> Path:
    """Where TWS/Gateway itself stores its settings -- IBC's own
    `TWS_SETTINGS_PATH` (`userguide.md`'s "Running multiple instances of
    TWS"), deliberately kept separate from `_resolve_tws_path`'s install
    location.

    **A real bug, not a hypothetical, found live (2026-09-05):** this
    distinction was already named as a requirement back when `launcher.py`
    was first designed (CLAUDE.md, Build plan step 3: "one -DjtsConfigDir per
    instance... otherwise two instances sharing an install collide on
    state") but never actually implemented -- `build_launch_plan` passed
    `_resolve_tws_path`'s own *install* directory straight to
    `-DjtsConfigDir`, so paper and live shared the exact same settings
    directory the one time they were run concurrently. IBC's own userguide is
    explicit about the consequence: "there are some files that TWS creates
    while running that are not separated by username... only one instance of
    TWS can access them at a time" -- unless each instance gets its own
    `TWS_SETTINGS_PATH`. Both Gateway processes died silently within minutes
    of that concurrent run, matching exactly the failure mode IBC's own docs
    warn about; not proven as the root cause (no crash report, no direct
    causal log line), but the single most concrete, checkable candidate found
    so far for a pattern flagged three separate times this project and never
    otherwise explained.

    Unlike IBC's own default (shared-install-folder settings unless a user
    opts into `TWS_SETTINGS_PATH`), ibcontroller defaults to a *separate*
    per-instance directory always -- `~/Jts/{instance}`, matching the
    project's own userguide-cited example (`C:\\JtsLive`/`C:\\JtsPaper`)
    literally, not just in spirit -- since running live and paper
    concurrently is this project's whole reason to exist, not an edge case to
    opt into safety for."""
    if config.tws_settings_path:
        return Path(config.tws_settings_path)
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
    """Ports IBC's own `JtsIniManager.findSettingAndLog`/`getMissingSettings`
    -- `settings` is `(key, expected_value, overwrite_if_different)` triples.
    A missing key is appended to the section (creating the section if it
    doesn't exist yet); an existing key with the wrong value is only
    overwritten if `overwrite_if_different` is true (matches IBC's own
    `s3store` exception -- a value a specific deployment set deliberately is
    never silently reverted). Returns the possibly-modified lines and
    whether anything actually changed, so the caller only rewrites the file
    when needed."""
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
    """Ports IBC's own `JtsIniManager.java` -- avoids the "Use SSL encryption"
    dialog (and a Locale/proxy-message quirk) entirely by ensuring `jts.ini`
    contains a known-good minimal set of settings *before* Gateway/TWS ever
    starts, rather than reactively clicking a button on a dialog that may not
    even appear consistently.

    **Confirmed real and load-bearing, live, 2026-09-05** -- a genuinely fresh
    settings directory (`~/Jts/{instance}`, first use ever, the direct result
    of that same day's settings-path fix) showed exactly this dialog, its
    real text matching IBC's own code comment describing it verbatim ("SSL
    encryption is required... Would you like to reconnect with SSL
    support?"). Its own component tree includes `twslaunch.jtscomponents.
    effect.m` -- the exact class whose static initializer caused the JPMS
    crash investigated in the original Milestone -- so avoiding the dialog
    also avoids ever exercising that code path at all, not just surviving it
    (separately confirmed survivable: clicking "Reconnect using SSL" on a
    real occurrence didn't crash, matching the already-verified `--add-opens`
    flag set).

    IBC's own comment explains the mechanism precisely (`JtsIniManager.java`):
    `UseSSL=true` was added because "IB started insisting on use of SSL: if
    UseSSL=false was set, a dialog was displayed by TWS and Gateway giving
    the user the choice to restart using SSL or to close the program."
    `Locale=en` and `displayedproxymsg=1` are IBC's other two long-standing
    `[Logon]` fixes (force English regardless of OS locale; suppress an
    unrelated, "annoying, factually incorrect" proxy-access dialog) -- ported
    alongside since they cost nothing extra and fix real, documented
    problems. `ApiOnly=true` under `[IBGateway]` is Gateway-specific (IBC:
    without it, "the gateway displays a login form that has a structure IBC
    doesn't expect, and it can't find the trading mode selector" -- a
    version-old issue, kept since it's the known-good baseline, not because
    we've hit it ourselves).

    **Deliberately not ported**: `TrustedIPs`/`LocalServerPort` -- both need a
    value ibcontroller has no config field for yet (IBC reads them from its
    own `TrustedTwsApiClientIPs`/`OverrideTwsApiPort` settings); adding a
    settings-file line ibcontroller can't populate correctly would be worse
    than not adding it at all.
    """
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
    """`program` and `tws_version` are plain parameters, not read from `config` --
    the caller (`build_launch_plan`) has already resolved both, whether pinned
    (`config.tws_version`) or auto-detected (`_detect_tws_version`), and a
    fallback resolution (`_resolve_program_path`) needs to derive the *other*
    program's path without building a second `Config`. Keeps this function
    pure/testable against plain strings, same discipline as the rest of this
    module's pure half."""
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
    """Ports IBC's own `ibcstart.sh` fallback (the `if [[ ! -e "${program_path}/
    jars" ]]` block at lines 263-266): a TWS request whose install has no
    `jars/` directory resolves to the same-version Gateway install instead,
    with the TWS entry class (`jclient.LoginFrame`) unchanged -- the Gateway
    distribution's jars carry both entry classes (verified live, the
    `jts4launch-*.jar` in a Gateway install ships `jclient/LoginFrame.class`
    alongside `ibgateway/GWClient.class`).

    Returns `(install_dir, resolved_program)` -- the second value is the
    *actual* program of the resolved install (`"gateway"` when a TWS request
    fell back), which the caller uses to pick the right `.vmoptions` file
    (`ibgateway.vmoptions`, not `tws.vmoptions`, in a Gateway dir -- IBC's own
    `alt_vmoptions_source`). Only the TWS->Gateway direction exists here,
    per maintainer decision (2026-09-13, gitea #25); a Gateway request never
    falls back to a TWS install.

    Same `jars/` existence check IBC uses as its fallback trigger, not a
    different one -- deliberately identical, so a version that exists in TWS
    form wins even when a same-named Gateway install also exists."""
    requested_program = program.lower()
    program_path = _program_path(requested_program, os_name, tws_path, tws_version)
    if requested_program == "tws" and not (program_path / "jars").is_dir():
        return _program_path("gateway", os_name, tws_path, tws_version), "gateway"
    return program_path, requested_program


def _scan_install_dirs(
    tws_path: Path, os_name: str, program: str
) -> list[tuple[str, Path]]:
    """The per-program half of `_list_version_candidates`: `program`'s own tree
    only -- TWS on macOS (`Trader Workstation *`) or Linux (top-level dirs
    minus `ibgateway/`), Gateway on macOS (`IB Gateway *`) or Linux
    (`ibgateway/`). `_list_version_candidates` calls this twice for a TWS
    request (own tree, then the Gateway tree as fallback); splitting the scan
    out keeps that function's own shape minimal rather than nesting a full
    second scan beside it."""
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
    subdirectory -- the same check `_build_classpath` itself makes, so a candidate
    this returns is guaranteed launchable. Returns `(version, program_path)`
    pairs, sorted by directory name per tree for a deterministic iteration order
    (not by version -- `_detect_tws_version` does that numerically once it has
    the final candidate list).

    The TWS->Gateway fallback (gitea #25, 2026-09-13) lives here too, sharing
    `_resolve_program_path`'s trigger: when a TWS request finds no TWS install at
    all, the Gateway installs become the candidate pool (mirroring IBC's own
    ibcstart.sh fallback). `tws_channel`'s filter is applied afterwards, in
    `_detect_tws_version`, uniformly across whichever pool -- a channel mismatch
    is a config error surfaced by that filter, never silently relaxed (maintainer
    decision 2026-09-13)."""
    candidates = _scan_install_dirs(tws_path, os_name, program)
    if candidates:
        return candidates
    if program == "tws":
        return _scan_install_dirs(tws_path, os_name, "gateway")
    return candidates


def _version_sort_key(version: str) -> tuple[int, ...]:
    """Numeric comparison, not lexical -- handles both macOS's dotted `"10.50"` and
    the Windows/Linux single-integer `"1050"` style (CLAUDE.md's Windows assessment)
    without assuming either format; extracts every digit run and compares them as a
    tuple of ints (`"10.50"` -> `(10, 50)`, `"1050"` -> `(1050,)`)."""
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _read_i4j_variable(install4j_dir: Path, name: str) -> str | None:
    """A plain install4j `<variable name="{name}" value="..." />` entry -- distinct
    from `_read_jxbrowser_key`'s own regex, which pulls a `-D` flag out of the much
    larger `javaOptions` variable's own value blob (two different shapes in the same
    `i4jparams.conf` file). Used for `channel` -- confirmed live that each installed
    version carries its own real value here (`IB Gateway 10.45` -> `"stable"`,
    `IB Gateway 10.50` -> `"latest"`), not a fixed label."""
    conf = install4j_dir / "i4jparams.conf"
    if not conf.is_file():
        return None
    match = re.search(rf'name="{re.escape(name)}" value="([^"]*)"', conf.read_text())
    return match.group(1) if match else None


def _candidates_for_channel(
    tws_path: Path, os_name: str, program: str, channel: str | None
) -> list[tuple[str, Path]]:
    """`_list_version_candidates`'s output, filtered by `channel` -- separated so
    `_detect_tws_version` can retry a TWS request against the Gateway pool after
    the TWS pool comes up empty under the filter (gitea #25, live-caught
    2026-09-13: a TWS install present but on the wrong channel must not short-
    circuit the fallback -- `tws-latest` with only `Trader Workstation 10.45`
    (stable) present fell back to nothing instead of `IB Gateway 10.50`)."""
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
    """Ports `ibctl`'s own `detect_version` (`ibctl/src/supervisor.rs`, confirmed live
    -- its own `tws_major_vrsn` TOML field is documented "auto-detected if empty"),
    extended to be channel-aware: `ibctl` only ever needs to handle one installed
    version (its own Docker image), but a real desktop install can carry several
    side by side, each on its own update channel -- confirmed live, `~/Applications`
    on this machine has `IB Gateway 10.45` (channel=stable) and `IB Gateway 10.50`
    (channel=latest) at once, and `jars/` alone can't tell them apart.

    `channel`, if given, filters candidates by their own `_read_i4j_variable(...,
    "channel")` first. When the requested-program pool filters down to nothing, a
    TWS request retries against the Gateway pool -- the TWS->Gateway fallback
    (gitea #25): the filter still applies uniformly there, so a channel mismatch
    is still an error (a Gateway install on the *wrong* channel never satisfies a
    TWS request), but an empty TWS pool is not -- that's exactly when the fallback
    should engage. Whatever's left (or the unfiltered list, if `channel` is `None`)
    is resolved by picking the greatest version numerically -- per the user's own
    explicit instruction ("for latest should be easy because is the 'greatest', for
    stable... the 'greatest with channel=stable'"), not an error: unlike this
    project's usual "don't guess" default (Appendix D's rejected timeout-guess
    fallback, `GatewayDialogHandler`'s "leave it for the user"), the "greatest
    version" tie-break here was given directly by the user as the actual intended
    semantics of "latest"/"stable", not a guess standing in for missing information
    -- logged clearly either way so it's never a silent choice."""
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
    """Two genuinely different cascades, not one path with a branch (IBC's own
    `ibcstart.sh`, "Determine the location of java executable")."""
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
    """Skips comments and `-D` lines the same way `ibcstart.sh` does (it adds
    its own `-D` properties explicitly -- see `_INSTALL4J_DPROPS_STATIC`)."""
    if not path.is_file():
        return []
    options = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-D"):
            continue
        options.append(line)
    return options


def _read_jxbrowser_key(install4j_dir: Path) -> str | None:
    """Passkey (WebAuthn) 2FA renders in an embedded browser (JxBrowser). The
    official install4j launcher passes `-DjxBrowserKey=<license key>`; without
    it JxBrowser can't initialise at all ("Failed to create browser"). IBC hit
    this and fixed it the same way (`ibcstart.sh`, commit `1ca8157`, June
    2026) -- read the key dynamically, since it's version-specific."""
    conf = install4j_dir / "i4jparams.conf"
    if not conf.is_file():
        return None
    match = re.search(r"DjxBrowserKey=([^\"\s]+)", conf.read_text())
    return match.group(1) if match else None


def _read_macos_vmoptions(app_bundle: Path) -> list[str]:
    """`--add-opens`/`--add-exports` (JPMS strong encapsulation) live in the
    `.app` bundle's `Info.plist` (install4j's `JavaVM`/`VMOptionArray`), not in
    `ibgateway.vmoptions` -- confirmed 2026-09-04 by a real failure (a static
    initializer threw `InaccessibleObjectException`, symptom: Configure >
    Settings wouldn't open). Read live from the installed app, not hardcoded,
    since it can change per version. `plistlib` directly (verified against a
    real install first) -- one less external-tool dependency (`plutil` + `jq`)
    than the bash version needed.

    Takes the `.app` bundle's own path directly (not `program_path` -- changed
    2026-09-06, see `_prevent_native_restart`): the bundle may already have been
    renamed by that function, so re-deriving `f"{program_path.name}.app"` here
    would silently miss it on every relaunch after the first."""
    app_plist = app_bundle / "Contents" / "Info.plist"
    if not app_plist.is_file():
        return []
    with app_plist.open("rb") as f:
        data = plistlib.load(f)
    raw_options = data.get("JavaVM", {}).get("VMOptionArray", [])
    return [opt for opt in raw_options if "${" not in opt and not opt.startswith("-D")]


def _prevent_native_restart(program_path: Path) -> Path:
    """Ports IBC's own `ibcstart.sh` step ("Renaming IB's TWS or Gateway start
    script to prevent restart without IBC") -- confirmed live necessary,
    2026-09-06, not just IBC-style caution copied blind: a real scheduled
    Gateway restart was observed to relaunch Gateway via its own native
    install4j launcher stub (`<name>.app/Contents/MacOS/JavaApplicationStub`)
    directly, completely outside `launcher.py`, with zero agent attached.
    Left alone, that means (a) `control_loop.py`'s own future relaunch-after-
    restart logic would race the native stub for the same account the moment
    it also tries to relaunch, and (b) even without that race, the "restarted"
    Gateway has no agent embedded at all -- ibcontroller silently loses control
    of the instance on every single daily restart. Renaming the bundle breaks
    whatever internal reference Gateway's own restart logic uses to find and
    re-invoke it, so only an explicit relaunch through this module can bring
    it back -- the exact mechanism IBC's script already relies on, not a new
    invention.

    Idempotent, matching IBC's own `if [[ -e ... ]]` guard: a second call after
    the rename already happened is a no-op, returning the same renamed path.
    Returns the `.app` bundle's current real path either way, since
    `_read_macos_vmoptions` needs to read `Info.plist` from wherever it
    actually lives now, renamed or not.

    macOS only, called only under `os_name == "macos"` in `build_launch_plan`
    -- IBC's own Linux branch renames the `tws`/`ibgateway` launch *scripts*
    instead, a different mechanism this project hasn't needed yet (no live
    Linux install to verify against, per CLAUDE.md's own platform note)."""
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


def build_launch_plan(
    config: Config,
    agent_jar: str | Path,
    *,
    os_name: str | None = None,
    runtime_dir: str | Path | None = None,
    restart_hash: str | None = None,
) -> LaunchPlan:
    """The pure, filesystem-only half of launching -- resolves everything
    `scripts/launch-agent.sh` resolves, without spawning anything.

    `runtime_dir` is where the agent's own command/event sockets are created --
    resolved via `app_dirs.resolve_runtime_dir()` (platformdirs) unless a caller
    overrides it (tests do, to avoid depending on the real OS-specific runtime
    location). Java never derives this path itself -- `AgentMain` takes the
    command-socket path as a plain CLI argument -- so this is the one place the
    convention lives; `scripts/launch-agent.sh`'s own `/tmp` convention is a
    Phase 1 dev tool, superseded by this module rather than kept in sync with
    it (bandit B108: a hardcoded `/tmp/...` path is the classic /tmp-symlink-
    attack class, CWE-377).

    `restart_hash` (2026-09-07): when given (`login.find_autorestart_hash`'s
    return value), adds `-Drestart=<hash>` to the command -- IBC's own
    `ibcstart.sh` always passes this on a relaunch after a scheduled restart,
    and ibctl's independent implementation does too, describing it as what
    lets Gateway resume the session without 2FA. Added after confirming live
    that the marker file's mere *presence* on disk is not sufficient by
    itself -- three separate, reproducible tests (two different gap lengths)
    showed a completely blank login form despite `is_restart()` correctly
    finding the marker. Whether this flag actually changes that outcome is
    the open, live-testable question this parameter exists to answer -- not
    assumed to fix it just because two other implementations pass it."""
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
    channel = _read_i4j_variable(install4j_dir, "channel") or _DEFAULT_CHANNEL
    vm_options.append(f"-Dchannel={channel}")

    jxbrowser_key = _read_jxbrowser_key(install4j_dir)
    if jxbrowser_key:
        vm_options.append(f"-DjxBrowserKey={jxbrowser_key}")

    if os_name == "macos":
        app_bundle = _prevent_native_restart(program_path)
        vm_options.extend(_read_macos_vmoptions(app_bundle))

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

    `restart_hash` (2026-09-07): passed straight through to
    `build_launch_plan` -- see that function's own docstring for what it does
    and why.

    `build_launch_plan` itself runs via `anyio.to_thread.run_sync`, not called
    directly -- it's a long, purely synchronous chain of filesystem calls
    (`.mkdir`, `.read_text`/`.write_text`, `.glob`/`.iterdir`, and more, across
    every helper it calls: `_ensure_jts_ini`, `_detect_tws_version`,
    `_build_classpath`, `_find_java_bin`, `_read_vmoptions_file`,
    `_read_jxbrowser_key`, and on macOS `_read_macos_vmoptions`/
    `_prevent_native_restart`), and this runs on every launch *and* every
    scheduled restart while the Dispatcher's own event/command tasks are
    already live on the same loop. Wrapping the one outer call keeps
    `build_launch_plan` and its helpers exactly as they are -- plain,
    synchronous, independently unit-tested against a synthetic directory tree
    -- rather than rewriting each one to an async equivalent.

    Configures the `ibcontroller` logging hierarchy first -- this was the one
    real caller `logging_setup.py` was built for (`login.py`/`recognisers.py`/
    `dispatch.py` all log through `logging.getLogger(__name__)`, a no-op
    until something calls `configure_logging`), found missing entirely
    (2026-09-05): the module had unit tests but no real call site anywhere in
    application code. Also configures the launched process's own stdout
    logger (`configure_gateway_stdout`) here, alongside the other two, so
    `_drain_stdout` has it ready before the process is spawned below.

    **`config.log_dir` always resolved (2026-09-05):** trace streams are enabled
    independently of our own logs -- `configure_trace` gates purely on the
    `trace_enabled` toggle, and its files (the raw wire trace) land in the same
    `config.log_dir`, never `None` for our own `ibcontroller.log`.
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
    )
    # **A real bug, found live (2026-09-05), not the modal-dialog deadlock
    # first suspected**: nothing ever read this pipe. Gateway logs
    # continuously (log4j, redirected here via stderr=STDOUT too) -- once the
    # OS pipe buffer fills, any Java thread that writes to stdout blocks on
    # that write, forever, since nothing will ever drain it. Confirmed live:
    # the GUI/EDT stayed fully responsive (the user could open dialogs/menus
    # normally) while our own agent's command socket appeared completely
    # frozen (a plain `ping` on a fresh connection timed out) -- consistent
    # with some non-EDT thread (whatever logs on our agent's own
    # command-handling path) blocking on a full pipe, not a Swing-level
    # deadlock. Drained unconditionally for the process's whole life, into
    # `stdout_logger` (Gateway's own diagnostic noise, a different concern
    # from `log_level`/`configure_logging` above) -- self-terminates on EOF,
    # i.e. once the process itself exits, no explicit cancellation needed.
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
    it can save session state and upload settings (IBC's `StopTask` makes the
    identical distinction). Only hard-terminates directly when login never
    completed (nothing to save), or as a last resort if the clean path hangs.

    `labels` (2026-09-09): the menu path itself used to be a hardcoded
    `"File/Close"`/`"File/Exit"` ternary right here -- found by an audit
    prompted by two other hardcoded-label bugs the same day (the TWS
    main-window menu item, the TWS Settings menu path). See `labels.
    ShutdownLabels`'s own docstring.

    `logged_in` is the caller's own responsibility to know -- L6's job once
    `login.py` exists; this layer doesn't track login state itself.

    **Corrected 2026-09-05 (a real bug, caught live against a real logged-in
    Gateway): uses `navigate_menu`, not `click`.** "Close"/"Exit" are menu
    items, not ordinary buttons -- `click`'s `findByAccessibleName` lookup can
    never find them (a `JMenu`'s dropdown lives in a `JPopupMenu`, outside the
    ordinary component tree while the menu is closed), so this always
    silently fell through to the hard-kill fallback below instead of the
    intended graceful exit. See `actions.py`'s own docstring and
    `WriteOps.java`'s `navigateMenu` for the full story.

    **Corrected again 2026-09-12: goes through `actions.navigate_menu`, not a
    direct `dispatcher.send_command(functools.partial(dispatcher.command_conn.
    navigate_menu, ...))`.** The direct form was the one real bypass of the
    `actions.py` vocabulary in production code (issue #22) -- it skipped
    `actions.navigate_menu`'s own disabled-item retry loop, so a shutdown
    landing while the menu item is still momentarily disabled failed
    immediately instead of retrying like every other `navigate_menu` caller.
    A short `timeout` bounds the retry so a genuinely stuck menu still falls
    through to the hard-kill fallback promptly, matching the old behavior's
    intent.

    **Corrected again 2026-09-06 (a second real bug, caught live): a process
    that already exited on its own before this was ever called -- the user
    manually closing Gateway, or a real scheduled restart -- has no live
    command socket left to send `navigate_menu` over.** Confirmed live:
    attempting it raised a raw `ConnectionResetError`, which the existing
    `except AgentClientError` never caught (that only covers the agent's own
    typed protocol errors, not a dead connection) -- the exception propagated
    out of the whole control-loop cycle instead of a clean `PROCESS_EXITED`
    return. Checks `launched.process.returncode` first now (ground truth,
    matching `_run_one_cycle`'s own precedent for this exact check): a dead
    process has nothing left to gracefully ask. The `except` below is also
    broadened to `OSError` as defense in depth for the narrower race (the
    process dying between this check and the command actually being sent).

    **Corrected a third time, same day, caught by the very next live run of
    the fix above: `launched.process.terminate()` itself raises
    `ProcessLookupError` when called on a process that's already exited** --
    there's no OS PID left to signal. The already-exited branch below no
    longer calls `terminate()` at all, only stops the dispatcher; `terminate()`
    stays reserved for the "never logged in, but still running" branch, where
    the process genuinely is still alive.
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
        logger.info("IBController > shut down cleanly via %s", target)
    except TimeoutError:
        logger.warning(
            "IBController > clean shutdown via %s did not exit within %ss -- "
            "terminating",
            target,
            timeout,
        )
        launched.process.terminate()
    await launched.dispatcher.stop()
