"""Unit tests for launcher.py's pure, filesystem-only half (CLAUDE.md's "Working
method": unit tests at each layer, once validated) -- a synthetic install tree
standing in for a real Gateway/TWS install, no live Gateway needed. The async half
(`launch_instance`, `clean_shutdown`) needs a real install to mean anything and is
live-validated separately, not unit-tested here."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import plistlib
from pathlib import Path
from tempfile import gettempdir
from typing import Any

import anyio.to_thread
import attrs
import pytest
from typed_settings.types import Secret

from ibcontroller.agent_client import AgentCommandConnection, AgentEventConnection
from ibcontroller.config import Config, TradingMode
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import load_labels
from ibcontroller.launcher import (
    LaunchedInstance,
    LauncherError,
    _build_classpath,
    _detect_tws_version,
    _drain_stdout,
    _ensure_jts_ini,
    _find_java_bin,
    _list_version_candidates,
    _prevent_native_restart,
    _prevent_native_restart_linux,
    _program_path,
    _read_i4j_variable,
    _read_jxbrowser_key,
    _read_linux_vmoptions,
    _read_macos_vmoptions,
    _read_vmoptions_file,
    _resolve_program_path,
    _resolve_tws_path,
    _version_sort_key,
    _wait_for_ready,
    build_launch_plan,
    clean_shutdown,
    resolve_tws_settings_path,
)
from ibcontroller.logging_setup import configure_gateway_stdout, stop_logging
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio

LABELS = load_labels()


def _config(**overrides: Any) -> Config:
    base = Config(
        tws_version="10.50",
        trading_mode=TradingMode.PAPER,
        userid=Secret("u"),
        password=Secret("p"),
        log_dir=f"{gettempdir()}/log",
    )
    return attrs.evolve(base, **overrides)


def test_resolve_tws_path_defaults_macos():
    path = _resolve_tws_path(_config(), "macos")
    assert path.name == "Applications"


def test_resolve_tws_path_defaults_linux():
    path = _resolve_tws_path(_config(), "linux")
    assert path.name == "Jts"


def test_resolve_tws_settings_path_defaults_per_instance():
    """Deliberately NOT `_resolve_tws_path`'s install location -- a real bug
    found live (2026-09-05): paper and live shared one settings directory by
    conflating the two, matching IBC's own userguide.md warning about running
    multiple instances against the same settings folder. Default shape is
    `~/Jts/{instance}`, always separated, unlike IBC's own opt-in default."""
    path = resolve_tws_settings_path(_config(instance="paper"))
    assert path.name == "paper"
    assert path.parent.name == "Jts"
    assert path.parent.parent == Path.home()


def test_resolve_tws_settings_path_different_instances_never_collide():
    paper_path = resolve_tws_settings_path(_config(instance="paper"))
    live_path = resolve_tws_settings_path(_config(instance="live"))
    assert paper_path != live_path


def test_resolve_tws_settings_path_explicit_override(tmp_path):
    override = tmp_path / "custom-settings"
    path = resolve_tws_settings_path(_config(tws_settings_path=str(override)))
    assert path == override


def test_ensure_jts_ini_creates_minimal_file_gateway(tmp_path):
    """Ports IBC's own JtsIniManager.java (2026-09-05) -- avoids the real
    "Use SSL encryption" dialog entirely (confirmed live: its own component
    tree includes the exact JPMS-crash-prone class from the Milestone)."""
    _ensure_jts_ini(tmp_path, is_gateway=True)
    lines = (tmp_path / "jts.ini").read_text().splitlines()
    assert "[Logon]" in lines
    assert "UseSSL=true" in lines
    assert "Locale=en" in lines
    assert "displayedproxymsg=1" in lines
    assert "s3store=true" in lines
    assert "[IBGateway]" in lines
    assert "ApiOnly=true" in lines


def test_ensure_jts_ini_creates_minimal_file_tws_no_apionly(tmp_path):
    _ensure_jts_ini(tmp_path, is_gateway=False)
    lines = (tmp_path / "jts.ini").read_text().splitlines()
    assert "UseSSL=true" in lines
    assert "[IBGateway]" not in lines


def test_ensure_jts_ini_adds_missing_setting_to_existing_file(tmp_path):
    (tmp_path / "jts.ini").write_text("[Logon]\nLocale=en\n")
    _ensure_jts_ini(tmp_path, is_gateway=False)
    lines = (tmp_path / "jts.ini").read_text().splitlines()
    assert "UseSSL=true" in lines
    assert "Locale=en" in lines  # untouched, still correct


def test_ensure_jts_ini_overwrites_wrong_usessl_value(tmp_path):
    (tmp_path / "jts.ini").write_text("[Logon]\nUseSSL=false\n")
    _ensure_jts_ini(tmp_path, is_gateway=False)
    lines = (tmp_path / "jts.ini").read_text().splitlines()
    assert "UseSSL=true" in lines
    assert "UseSSL=false" not in lines


def test_ensure_jts_ini_never_overwrites_existing_s3store(tmp_path):
    """Matches IBC's own documented exception: a deliberately-set
    s3store=false (cross-connect configurations) must never be silently
    reverted, unlike every other setting here."""
    (tmp_path / "jts.ini").write_text("[Logon]\ns3store=false\n")
    _ensure_jts_ini(tmp_path, is_gateway=False)
    lines = (tmp_path / "jts.ini").read_text().splitlines()
    assert "s3store=false" in lines


def test_ensure_jts_ini_no_rewrite_when_already_correct(tmp_path):
    _ensure_jts_ini(tmp_path, is_gateway=True)
    path = tmp_path / "jts.ini"
    before = path.read_text()
    before_mtime = path.stat().st_mtime_ns
    _ensure_jts_ini(tmp_path, is_gateway=True)
    assert path.read_text() == before
    assert path.stat().st_mtime_ns == before_mtime


def test_build_launch_plan_creates_jts_ini(tmp_path):
    base = _make_synthetic_install(tmp_path, os_name="macos")
    settings_dir = tmp_path / "settings"
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(settings_dir),
        instance="paper",
    )
    build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=tmp_path / "run"
    )
    assert (settings_dir / "jts.ini").is_file()
    assert "UseSSL=true" in (settings_dir / "jts.ini").read_text()


def test_resolve_tws_path_explicit_override():
    path = _resolve_tws_path(_config(tws_path="/custom/path"), "macos")
    assert str(path) == "/custom/path"


def test_program_path_macos_gateway(tmp_path):
    path = _program_path("gateway", "macos", tmp_path, "10.50")
    assert path == tmp_path / "IB Gateway 10.50"


def test_program_path_macos_tws(tmp_path):
    path = _program_path("tws", "macos", tmp_path, "10.50")
    assert path == tmp_path / "Trader Workstation 10.50"


def test_program_path_linux_gateway(tmp_path):
    path = _program_path("gateway", "linux", tmp_path, "10.50")
    assert path == tmp_path / "ibgateway" / "10.50"


def test_program_path_linux_tws(tmp_path):
    path = _program_path("tws", "linux", tmp_path, "10.50")
    assert path == tmp_path / "10.50"


def test_resolve_program_path_macos_prefers_tws_install(tmp_path):
    """TWS->Gateway fallback (gitea #25): a TWS request resolves to the TWS
    install whenever its `jars/` exists -- IBC's own ibcstart.sh priority."""
    _make_version_dir(tmp_path, "Trader Workstation 10.50")
    _make_version_dir(tmp_path, "IB Gateway 10.50")
    path, resolved = _resolve_program_path("tws", "macos", tmp_path, "10.50")
    assert path == tmp_path / "Trader Workstation 10.50"
    assert resolved == "tws"


def test_resolve_program_path_macos_falls_back_to_gateway_install(tmp_path):
    """No TWS install (no `jars/`), same-version Gateway install present -- the
    request resolves to the Gateway install running as TWS (entry class stays
    `jclient.LoginFrame`; this only resolves the install path). Same `jars/`
    existence check as IBC's `if [[ ! -e .../jars ]]`."""
    (tmp_path / "Trader Workstation 10.50").mkdir()  # exists, but no jars/
    _make_version_dir(tmp_path, "IB Gateway 10.50")
    path, resolved = _resolve_program_path("tws", "macos", tmp_path, "10.50")
    assert path == tmp_path / "IB Gateway 10.50"
    assert resolved == "gateway"


def test_resolve_program_path_linux_tws_falls_back_to_ibgateway(tmp_path):
    _make_version_dir(tmp_path / "ibgateway", "10.50")
    path, resolved = _resolve_program_path("tws", "linux", tmp_path, "10.50")
    assert path == tmp_path / "ibgateway" / "10.50"
    assert resolved == "gateway"


def test_resolve_program_path_gateway_never_falls_back_to_tws(tmp_path):
    """Scope decision (2026-09-13, gitea #25): only TWS->Gateway exists. A Gateway
    request returns the Gateway path even when a TWS install is present instead."""
    _make_version_dir(tmp_path, "Trader Workstation 10.50")
    path, resolved = _resolve_program_path("gateway", "macos", tmp_path, "10.50")
    assert path == tmp_path / "IB Gateway 10.50"
    assert resolved == "gateway"


def test_build_classpath(tmp_path):
    program_path = tmp_path / "install"
    jars_dir = program_path / "jars"
    jars_dir.mkdir(parents=True)
    (jars_dir / "b.jar").write_text("")
    (jars_dir / "a.jar").write_text("")
    install4j_dir = program_path / ".install4j"
    install4j_dir.mkdir()
    (install4j_dir / "i4jruntime.jar").write_text("")
    agent_jar = tmp_path / "ibcontroller-agent.jar"

    classpath = _build_classpath(program_path, install4j_dir, agent_jar)
    parts = classpath.split(":")
    assert parts == [
        str(jars_dir / "a.jar"),
        str(jars_dir / "b.jar"),
        str(install4j_dir / "i4jruntime.jar"),
        str(agent_jar),
    ]


def test_build_classpath_missing_jars_dir_raises(tmp_path):
    with pytest.raises(LauncherError, match="not found"):
        _build_classpath(
            tmp_path / "nope", tmp_path / ".install4j", tmp_path / "agent.jar"
        )


def test_find_java_bin_macos_jre_subdir(tmp_path):
    java = (
        tmp_path
        / ".install4j"
        / "jre.bundle"
        / "Contents"
        / "Home"
        / "jre"
        / "bin"
        / "java"
    )
    java.parent.mkdir(parents=True)
    java.write_text("")
    found = _find_java_bin("macos", tmp_path / ".install4j", tmp_path)
    assert found == java


def test_find_java_bin_macos_fallback_no_jre_subdir(tmp_path):
    java = tmp_path / ".install4j" / "jre.bundle" / "Contents" / "Home" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_text("")
    found = _find_java_bin("macos", tmp_path / ".install4j", tmp_path)
    assert found == java


def test_find_java_bin_linux_pref_jre_cfg(tmp_path):
    install4j_dir = tmp_path / ".install4j"
    install4j_dir.mkdir()
    jre_dir = tmp_path / "some-jre"
    java = jre_dir / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_text("")
    (install4j_dir / "pref_jre.cfg").write_text(str(jre_dir))
    found = _find_java_bin("linux", install4j_dir, tmp_path)
    assert found == java


def test_find_java_bin_linux_falls_back_to_inst_jre_cfg(tmp_path):
    install4j_dir = tmp_path / ".install4j"
    install4j_dir.mkdir()
    (install4j_dir / "pref_jre.cfg").write_text(str(tmp_path / "does-not-exist"))
    jre_dir = tmp_path / "inst-jre"
    java = jre_dir / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_text("")
    (install4j_dir / "inst_jre.cfg").write_text(str(jre_dir))
    found = _find_java_bin("linux", install4j_dir, tmp_path)
    assert found == java


def test_find_java_bin_linux_falls_back_to_program_jre(tmp_path):
    install4j_dir = tmp_path / ".install4j"
    install4j_dir.mkdir()
    java = tmp_path / "jre" / "bin" / "java"
    java.parent.mkdir(parents=True)
    java.write_text("")
    found = _find_java_bin("linux", install4j_dir, tmp_path)
    assert found == java


def test_find_java_bin_raises_when_nothing_found(tmp_path):
    install4j_dir = tmp_path / ".install4j"
    install4j_dir.mkdir()
    with pytest.raises(LauncherError, match="no bundled java executable"):
        _find_java_bin("linux", install4j_dir, tmp_path)


def test_read_vmoptions_file_skips_comments_and_dprops(tmp_path):
    vmoptions = tmp_path / "ibgateway.vmoptions"
    vmoptions.write_text(
        "-Xmx768m\n# a comment\n\n-Dsomething=already-here\n-XX:+UseG1GC\n"
    )
    options = _read_vmoptions_file(vmoptions)
    assert options == ["-Xmx768m", "-XX:+UseG1GC"]


def test_read_vmoptions_file_missing_returns_empty(tmp_path):
    assert _read_vmoptions_file(tmp_path / "does-not-exist.vmoptions") == []


def test_read_jxbrowser_key_found(tmp_path):
    install4j_dir = tmp_path / ".install4j"
    install4j_dir.mkdir()
    (install4j_dir / "i4jparams.conf").write_text(
        "...DjxBrowserKey=ABC123 more stuff..."
    )
    assert _read_jxbrowser_key(install4j_dir) == "ABC123"


def test_read_jxbrowser_key_missing_file(tmp_path):
    install4j_dir = tmp_path / ".install4j"
    install4j_dir.mkdir()
    assert _read_jxbrowser_key(install4j_dir) is None


def test_read_jxbrowser_key_absent_in_file(tmp_path):
    install4j_dir = tmp_path / ".install4j"
    install4j_dir.mkdir()
    (install4j_dir / "i4jparams.conf").write_text("nothing relevant here")
    assert _read_jxbrowser_key(install4j_dir) is None


def test_read_macos_vmoptions_filters_templates_and_dprops(tmp_path):
    program_path = tmp_path / "IB Gateway 10.50"
    app_bundle = program_path / "IB Gateway 10.50.app"
    app_dir = app_bundle / "Contents"
    app_dir.mkdir(parents=True)
    plist_data = {
        "JavaVM": {
            "VMOptionArray": [
                "--add-opens=java.desktop/javax.swing=ALL-UNNAMED",
                "-Dsome.prop=value",
                "${SOME_TEMPLATE_VAR}",
                "--add-exports=java.desktop/sun.awt=ALL-UNNAMED",
            ]
        }
    }
    with (app_dir / "Info.plist").open("wb") as f:
        plistlib.dump(plist_data, f)

    options = _read_macos_vmoptions(app_bundle)
    assert options == [
        "--add-opens=java.desktop/javax.swing=ALL-UNNAMED",
        "--add-exports=java.desktop/sun.awt=ALL-UNNAMED",
    ]


def test_read_macos_vmoptions_missing_plist_returns_empty(tmp_path):
    assert _read_macos_vmoptions(tmp_path / "IB Gateway 10.50.app") == []


def test_read_linux_vmoptions_keeps_dprops_filters_templates(tmp_path):
    install4j_dir = tmp_path / ".install4j"
    install4j_dir.mkdir()
    (install4j_dir / "i4jparams.conf").write_text(
        '<variable name="javaOptions" value="--add-opens=java.desktop/'
        "javax.swing=ALL-UNNAMED -Djdk.xml.elementAttributeLimit=10000 "
        '${SOME_TEMPLATE_VAR}" />'
    )

    options = _read_linux_vmoptions(install4j_dir)

    assert options == [
        "--add-opens=java.desktop/javax.swing=ALL-UNNAMED",
        "-Djdk.xml.elementAttributeLimit=10000",
    ]


def test_read_linux_vmoptions_missing_file_returns_empty(tmp_path):
    assert _read_linux_vmoptions(tmp_path / ".install4j") == []


def test_prevent_native_restart_renames_the_app_bundle(tmp_path):
    program_path = tmp_path / "IB Gateway 10.50"
    original = program_path / "IB Gateway 10.50.app"
    original.mkdir(parents=True)

    result = _prevent_native_restart(program_path)

    renamed = program_path / "IB Gateway 10.50-1.app"
    assert result == renamed
    assert renamed.is_dir()
    assert not original.exists()


def test_prevent_native_restart_is_idempotent(tmp_path):
    program_path = tmp_path / "IB Gateway 10.50"
    original = program_path / "IB Gateway 10.50.app"
    original.mkdir(parents=True)

    first = _prevent_native_restart(program_path)
    second = _prevent_native_restart(program_path)

    assert first == second
    assert second.is_dir()


def test_prevent_native_restart_missing_bundle_returns_original_path(tmp_path):
    program_path = tmp_path / "IB Gateway 10.50"
    result = _prevent_native_restart(program_path)
    assert result == program_path / "IB Gateway 10.50.app"


def test_prevent_native_restart_linux_renames_the_script(tmp_path):
    program_path = tmp_path / "ibgateway" / "10.50"
    program_path.mkdir(parents=True)
    original = program_path / "ibgateway"
    original.write_text("#!/bin/sh\nexec true\n")

    result = _prevent_native_restart_linux(program_path, "ibgateway")

    renamed = program_path / "ibgateway-1"
    assert result == renamed
    assert renamed.is_file()
    assert not original.exists()


def test_prevent_native_restart_linux_is_idempotent(tmp_path):
    program_path = tmp_path / "ibgateway" / "10.50"
    program_path.mkdir(parents=True)
    (program_path / "ibgateway").write_text("#!/bin/sh\nexec true\n")

    first = _prevent_native_restart_linux(program_path, "ibgateway")
    second = _prevent_native_restart_linux(program_path, "ibgateway")

    assert first == second
    assert second.is_file()


def test_prevent_native_restart_linux_missing_script_returns_original_path(tmp_path):
    program_path = tmp_path / "ibgateway" / "10.50"
    program_path.mkdir(parents=True)
    result = _prevent_native_restart_linux(program_path, "ibgateway")
    assert result == program_path / "ibgateway"


def _make_synthetic_install(tmp_path, *, os_name: str, program: str = "gateway"):
    base = tmp_path / ("Applications" if os_name == "macos" else "Jts")
    if program == "gateway":
        program_path = (
            base / "IB Gateway 10.50"
            if os_name == "macos"
            else base / "ibgateway" / "10.50"
        )
    else:
        program_path = (
            base / "Trader Workstation 10.50" if os_name == "macos" else base / "10.50"
        )
    (program_path / "jars").mkdir(parents=True)
    (program_path / "jars" / "core.jar").write_text("")
    install4j_dir = program_path / ".install4j"
    install4j_dir.mkdir()
    (install4j_dir / "i4jruntime.jar").write_text("")
    vmoptions_name = "ibgateway.vmoptions" if program == "gateway" else "tws.vmoptions"
    (program_path / vmoptions_name).write_text("-Xmx768m\n")

    if os_name == "macos":
        java = install4j_dir / "jre.bundle" / "Contents" / "Home" / "bin" / "java"
        java.parent.mkdir(parents=True)
        java.write_text("")
        app_dir = program_path / f"{program_path.name}.app" / "Contents"
        app_dir.mkdir(parents=True)
        with (app_dir / "Info.plist").open("wb") as f:
            plistlib.dump(
                {"JavaVM": {"VMOptionArray": ["--add-opens=x/y=ALL-UNNAMED"]}}, f
            )
    else:
        java = program_path / "jre" / "bin" / "java"
        java.parent.mkdir(parents=True)
        java.write_text("")
        script_name = "ibgateway" if program == "gateway" else "tws"
        (program_path / script_name).write_text("#!/bin/sh\nexec true\n")
    return base


def _make_version_dir(base_dir: Path, name: str, *, channel: str | None = None) -> Path:
    """A minimal version directory -- just enough for `_list_version_candidates`'s
    own `jars/` check and, when `channel` is given, `_read_i4j_variable`'s regex."""
    version_dir = base_dir / name
    (version_dir / "jars").mkdir(parents=True)
    if channel is not None:
        install4j_dir = version_dir / ".install4j"
        install4j_dir.mkdir()
        (install4j_dir / "i4jparams.conf").write_text(
            f'<variable name="channel" value="{channel}" />\n'
        )
    return version_dir


def test_read_i4j_variable_returns_the_value(tmp_path):
    install4j_dir = tmp_path / ".install4j"
    install4j_dir.mkdir()
    (install4j_dir / "i4jparams.conf").write_text(
        '<variable name="channel" value="stable" />\n'
    )
    assert _read_i4j_variable(install4j_dir, "channel") == "stable"


def test_read_i4j_variable_returns_none_when_variable_missing(tmp_path):
    install4j_dir = tmp_path / ".install4j"
    install4j_dir.mkdir()
    (install4j_dir / "i4jparams.conf").write_text(
        '<variable name="other" value="x" />\n'
    )
    assert _read_i4j_variable(install4j_dir, "channel") is None


def test_read_i4j_variable_returns_none_when_file_missing(tmp_path):
    assert _read_i4j_variable(tmp_path / ".install4j", "channel") is None


def test_version_sort_key_orders_numerically_not_lexically():
    assert _version_sort_key("10.45") < _version_sort_key("10.50")
    # A plain lexical string comparison gets this backwards -- "999" > "1019".
    assert _version_sort_key("999") < _version_sort_key("1019")


def test_list_version_candidates_macos_gateway(tmp_path):
    _make_version_dir(tmp_path, "IB Gateway 10.45", channel="stable")
    _make_version_dir(tmp_path, "IB Gateway 10.50", channel="latest")
    candidates = _list_version_candidates(tmp_path, "macos", "gateway")
    assert sorted(version for version, _ in candidates) == ["10.45", "10.50"]


def test_list_version_candidates_ignores_dirs_without_jars(tmp_path):
    (tmp_path / "IB Gateway 10.45").mkdir()  # no jars/ subdirectory
    _make_version_dir(tmp_path, "IB Gateway 10.50")
    candidates = _list_version_candidates(tmp_path, "macos", "gateway")
    assert [version for version, _ in candidates] == ["10.50"]


def test_list_version_candidates_linux_gateway(tmp_path):
    gw_dir = tmp_path / "ibgateway"
    _make_version_dir(gw_dir, "10.45", channel="stable")
    _make_version_dir(gw_dir, "10.50", channel="latest")
    candidates = _list_version_candidates(tmp_path, "linux", "gateway")
    assert sorted(version for version, _ in candidates) == ["10.45", "10.50"]


def test_list_version_candidates_linux_tws_excludes_ibgateway_dir(tmp_path):
    _make_version_dir(tmp_path, "10.45")
    _make_version_dir(tmp_path / "ibgateway", "10.50")  # a gateway install alongside
    candidates = _list_version_candidates(tmp_path, "linux", "tws")
    assert [version for version, _ in candidates] == ["10.45"]


def test_list_version_candidates_macos_tws_prefers_tws_installs(tmp_path):
    """The fallback only engages when *no* TWS install exists at all -- a TWS
    install present alongside a Gateway one must win (IBC's priority, gitea #25)."""
    _make_version_dir(tmp_path, "Trader Workstation 10.45")
    _make_version_dir(tmp_path, "IB Gateway 10.50")
    candidates = _list_version_candidates(tmp_path, "macos", "tws")
    assert [version for version, _ in candidates] == ["10.45"]


def test_list_version_candidates_macos_tws_falls_back_to_gateway_installs(tmp_path):
    """No TWS install under the tree at all -- the Gateway installs become the
    candidate pool for a TWS request (mirrors IBC's own fallback; `tws_channel`'s
    filter, applied later in `_detect_tws_version`, still gates them uniformly)."""
    _make_version_dir(tmp_path, "IB Gateway 10.45", channel="stable")
    _make_version_dir(tmp_path, "IB Gateway 10.50", channel="latest")
    candidates = _list_version_candidates(tmp_path, "macos", "tws")
    assert sorted(version for version, _ in candidates) == ["10.45", "10.50"]


def test_list_version_candidates_linux_tws_falls_back_to_ibgateway(tmp_path):
    _make_version_dir(tmp_path / "ibgateway", "10.50")
    candidates = _list_version_candidates(tmp_path, "linux", "tws")
    assert [version for version, _ in candidates] == ["10.50"]


def test_detect_tws_version_single_candidate(tmp_path):
    _make_version_dir(tmp_path, "IB Gateway 10.45", channel="stable")
    assert _detect_tws_version(tmp_path, "macos", "gateway", None) == "10.45"


def test_detect_tws_version_multiple_candidates_picks_greatest(tmp_path):
    """Per the user's own explicit instruction: 'latest' is the greatest version,
    not an error -- ambiguity is resolved, not rejected, when no channel filter is
    given."""
    _make_version_dir(tmp_path, "IB Gateway 10.45", channel="stable")
    _make_version_dir(tmp_path, "IB Gateway 10.50", channel="latest")
    assert _detect_tws_version(tmp_path, "macos", "gateway", None) == "10.50"


def test_detect_tws_version_channel_filter_picks_the_greatest_within_it(tmp_path):
    """The real case this was built for: 'stable' isn't the greatest version
    overall, it's the greatest version *within the stable channel* -- confirmed
    against real installs (IB Gateway 10.45 -> stable, IB Gateway 10.50 -> latest)."""
    _make_version_dir(tmp_path, "IB Gateway 10.45", channel="stable")
    _make_version_dir(tmp_path, "IB Gateway 10.50", channel="latest")
    assert _detect_tws_version(tmp_path, "macos", "gateway", "stable") == "10.45"


def test_detect_tws_version_zero_candidates_raises(tmp_path):
    with pytest.raises(LauncherError, match="no gateway installation found"):
        _detect_tws_version(tmp_path, "macos", "gateway", None)


def test_detect_tws_version_channel_filter_matches_nothing_raises(tmp_path):
    _make_version_dir(tmp_path, "IB Gateway 10.50", channel="latest")
    with pytest.raises(LauncherError, match="channel='stable'"):
        _detect_tws_version(tmp_path, "macos", "gateway", "stable")


def test_detect_tws_version_tws_falls_back_to_gateway_tree(tmp_path):
    """A TWS request with no TWS install auto-detects against the Gateway
    installs (gitea #25) -- channel-aware like the primary tree."""
    _make_version_dir(tmp_path, "IB Gateway 10.50", channel="stable")
    assert _detect_tws_version(tmp_path, "macos", "tws", "stable") == "10.50"


def test_detect_tws_version_tws_falls_back_when_tws_on_wrong_channel(tmp_path):
    """Regression test for the live-caught break (2026-09-13): a TWS install
    present but on the wrong channel must NOT short-circuit the fallback.
    `tws` + `latest` with only `Trader Workstation 10.45` (channel=stable)
    installed used to raise -- the `[10.45]` TWS pool (present, but filtered to
    nothing by `stable != latest`) never fell through to `IB Gateway 10.50`
    (channel=latest). A TWS pool that filters empty is exactly the signal to
    consult the Gateway pool, not to error."""
    _make_version_dir(tmp_path, "Trader Workstation 10.45", channel="stable")
    _make_version_dir(tmp_path, "IB Gateway 10.50", channel="latest")
    assert _detect_tws_version(tmp_path, "macos", "tws", "latest") == "10.50"


def test_detect_tws_version_tws_channel_filter_picks_tws_when_it_matches(tmp_path):
    """A TWS install on the requested channel wins over a Gateway on the same
    channel -- the fallback only engages when the TWS pool filters empty."""
    _make_version_dir(tmp_path, "Trader Workstation 10.45", channel="stable")
    _make_version_dir(tmp_path, "IB Gateway 10.50", channel="stable")
    assert _detect_tws_version(tmp_path, "macos", "tws", "stable") == "10.45"


def test_detect_tws_version_tws_channel_filter_still_strict_on_fallback_tree(tmp_path):
    """Per maintainer decision (2026-09-13): the channel filter gate is not
    relaxed for the fallback tree -- asking `tws` with `stable` while only a
    `latest` Gateway exists is a config error, surfaced, not silently satisfied."""
    _make_version_dir(tmp_path, "IB Gateway 10.50", channel="latest")
    with pytest.raises(LauncherError, match="channel='stable'"):
        _detect_tws_version(tmp_path, "macos", "tws", "stable")


def test_build_launch_plan_macos_gateway(tmp_path):
    base = _make_synthetic_install(tmp_path, os_name="macos")
    settings_dir = tmp_path / "settings"
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(settings_dir),
        instance="paper",
    )
    runtime_dir = tmp_path / "run"
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=runtime_dir
    )

    assert plan.command_socket_path == str(
        runtime_dir / "ibcontroller-agent-paper-cmd.sock"
    )
    assert plan.event_socket_path == str(
        runtime_dir / "ibcontroller-agent-paper-events.sock"
    )
    assert runtime_dir.is_dir()
    assert "ibgateway.GWClient" in plan.command
    assert "-Xmx768m" in plan.command
    assert "--add-opens=x/y=ALL-UNNAMED" in plan.command
    assert any(
        opt.startswith("-Dtwslaunch.autoupdate.serviceImpl=") for opt in plan.command
    )
    assert f"-DjtsConfigDir={settings_dir}" in plan.command
    assert plan.settings_dir == str(settings_dir)
    assert settings_dir.is_dir()
    # the settings dir must never default to the install dir -- that's the bug
    assert f"-DjtsConfigDir={base}" not in plan.command

    # The native .app bundle must be renamed -- otherwise Gateway's own
    # restart logic can relaunch it directly, with no agent attached,
    # racing any future relaunch-after-restart logic of our own (confirmed
    # live, 2026-09-06: a real scheduled restart came back via
    # <name>.app/Contents/MacOS/JavaApplicationStub, not through us at all).
    program_path = base / "IB Gateway 10.50"
    assert not (program_path / "IB Gateway 10.50.app").exists()
    assert (program_path / "IB Gateway 10.50-1.app").is_dir()


def test_build_launch_plan_java_heap_size_overrides_vmoptions_file(tmp_path):
    base = _make_synthetic_install(tmp_path, os_name="macos")
    settings_dir = tmp_path / "settings"
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(settings_dir),
        instance="paper",
        java_heap_size="2g",
    )
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=tmp_path / "run"
    )
    assert "-Xmx2g" in plan.command
    assert "-Xmx768m" not in plan.command


def test_build_launch_plan_auto_detects_tws_version_when_unset(tmp_path):
    """`Config.tws_channel` defaults to "stable" (2026-09-11 decision, TODO.md), so
    auto-detection filters to stable-channel installs -- the synthetic install must
    carry that channel metadata, matching every real install (see
    `test_build_launch_plan_reads_channel_dynamically`)."""
    base = _make_synthetic_install(tmp_path, os_name="macos")
    (base / "IB Gateway 10.50" / ".install4j" / "i4jparams.conf").write_text(
        '<variable name="channel" value="stable" />\n'
    )
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(tmp_path / "settings"),
        instance="paper",
        tws_version=None,
    )
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=tmp_path / "run"
    )
    assert any("IB Gateway 10.50" in part for part in plan.command)


def test_build_launch_plan_reads_channel_dynamically(tmp_path):
    """`-Dchannel=` must reflect the real install's own value, not a hardcoded
    literal -- confirmed live, 2026-09-09: `IB Gateway 10.45` carries
    `channel=stable`, `IB Gateway 10.50` carries `channel=latest`, each in its own
    `i4jparams.conf`."""
    base = _make_synthetic_install(tmp_path, os_name="macos")
    program_path = base / "IB Gateway 10.50"
    (program_path / ".install4j" / "i4jparams.conf").write_text(
        '<variable name="channel" value="stable" />\n'
    )
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(tmp_path / "settings"),
        instance="paper",
    )
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=tmp_path / "run"
    )
    assert "-Dchannel=stable" in plan.command
    assert "-Dchannel=latest" not in plan.command


def test_build_launch_plan_channel_falls_back_to_config_when_not_found(tmp_path):
    """No `channel` in the real install's `i4jparams.conf` -- falls back to
    `Config.tws_channel` (default "stable"), not a hardcoded literal (tea #35)."""
    base = _make_synthetic_install(tmp_path, os_name="macos")  # no i4jparams.conf
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(tmp_path / "settings"),
        instance="paper",
    )
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=tmp_path / "run"
    )
    assert "-Dchannel=stable" in plan.command


def test_build_launch_plan_omits_restart_flag_when_hash_not_given(tmp_path):
    base = _make_synthetic_install(tmp_path, os_name="macos")
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(tmp_path / "settings"),
        instance="paper",
    )
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=tmp_path / "run"
    )
    assert not any(opt.startswith("-Drestart=") for opt in plan.command)


def test_build_launch_plan_adds_agent_logging_flags(tmp_path):
    """2026-09-08: the agent's java.util.logging is routed to its own per-instance
    file under the same shared log dir Python uses (never the same filename as Python's
    own ibcontroller-{instance}.log / gateway-{instance}.log -- all flat in that
    directory since the per-instance subdirectory was removed the same day), and the
    level is passed as the string name matching AgentMain.parseLevel."""
    base = _make_synthetic_install(tmp_path, os_name="macos")
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(tmp_path / "settings"),
        instance="paper",
        log_dir=str(tmp_path / "logs"),
        log_level=logging.DEBUG,
    )
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=tmp_path / "run"
    )
    logfile_flag = (
        f"-Dibcontroller.logfile="
        f"{tmp_path / 'logs' / 'ibcontroller-java-agent-paper.log'}"
    )
    assert logfile_flag in plan.command
    assert "-Dibcontroller.log.level=DEBUG" in plan.command
    # Python's own (non-agent) log files have distinct names -- the agent's file
    # must never collide with ibcontroller-{instance}.log or gateway-{instance}.log.
    logfile = next(
        opt for opt in plan.command if opt.startswith("-Dibcontroller.logfile=")
    )
    assert "ibcontroller-java-agent-paper.log" in logfile
    assert "ibcontroller-paper.log" not in logfile
    assert "gateway-paper.log" not in logfile


def test_build_launch_plan_adds_restart_flag_when_hash_given(tmp_path):
    """2026-09-07: IBC's own `ibcstart.sh` and ibctl's independent
    implementation both pass `-Drestart=<hash>` on a relaunch after a
    scheduled restart -- added here after confirming live that the marker
    file's mere presence is not sufficient by itself."""
    base = _make_synthetic_install(tmp_path, os_name="macos")
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(tmp_path / "settings"),
        instance="paper",
    )
    plan = build_launch_plan(
        config,
        tmp_path / "agent.jar",
        os_name="macos",
        runtime_dir=tmp_path / "run",
        restart_hash="nlabafcdedmocmpmkmkcecpfjmillhejiljogfeh",
    )
    assert "-Drestart=nlabafcdedmocmpmkmkcecpfjmillhejiljogfeh" in plan.command


def test_build_launch_plan_macos_gateway_survives_relaunch_after_rename(tmp_path):
    """A second `build_launch_plan` call (matching a real relaunch-after-
    restart) must still find the real VM options even though the bundle is
    already renamed from the first call -- confirms `_read_macos_vmoptions`
    reads from wherever `_prevent_native_restart` says the bundle actually is,
    not a hardcoded original name."""
    base = _make_synthetic_install(tmp_path, os_name="macos")
    settings_dir = tmp_path / "settings"
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(settings_dir),
        instance="paper",
    )
    runtime_dir = tmp_path / "run"

    build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=runtime_dir
    )
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=runtime_dir
    )

    assert "--add-opens=x/y=ALL-UNNAMED" in plan.command


def test_build_launch_plan_linux_tws(tmp_path):
    base = _make_synthetic_install(tmp_path, os_name="linux", program="tws")
    settings_dir = tmp_path / "settings"
    config = _config(
        program="tws",
        tws_path=str(base),
        tws_settings_path=str(settings_dir),
        instance="live",
    )
    runtime_dir = tmp_path / "run"
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="linux", runtime_dir=runtime_dir
    )

    assert plan.command_socket_path == str(
        runtime_dir / "ibcontroller-agent-live-cmd.sock"
    )
    assert "jclient.LoginFrame" in plan.command
    assert "-Xmx768m" in plan.command
    assert f"-DjtsConfigDir={settings_dir}" in plan.command

    # The native launch script must be renamed -- otherwise TWS/Gateway's own
    # restart logic can invoke it directly, with no agent attached, bypassing
    # launcher.py entirely (confirmed live, 2026-09-14: a real scheduled
    # restart on Linux left ibcontroller unable to detect PROCESS_EXITED and
    # relaunch/relogin).
    program_path = base / "10.50"
    assert not (program_path / "tws").exists()
    assert (program_path / "tws-1").is_file()


def test_build_launch_plan_linux_reads_add_opens_from_i4jparams(tmp_path):
    """The bug this guards against: a real TWS-on-Linux launch crashed with
    `InaccessibleObjectException` because `tws.vmoptions` never carries
    `--add-opens`/`--add-exports` -- only install4j's own `javaOptions`
    variable in `i4jparams.conf` does (confirmed live, 2026-09-14)."""
    base = _make_synthetic_install(tmp_path, os_name="linux", program="tws")
    program_path = base / "10.50"
    (program_path / ".install4j" / "i4jparams.conf").write_text(
        '<variable name="javaOptions" value="--add-opens=java.desktop/'
        'javax.swing=ALL-UNNAMED" />\n'
    )
    settings_dir = tmp_path / "settings"
    config = _config(
        program="tws",
        tws_path=str(base),
        tws_settings_path=str(settings_dir),
        instance="live",
    )
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="linux", runtime_dir=tmp_path / "run"
    )

    assert "--add-opens=java.desktop/javax.swing=ALL-UNNAMED" in plan.command


def test_build_launch_plan_macos_tws_falls_back_to_gateway_install(tmp_path, caplog):
    """No TWS install, same-version Gateway install present (gitea #25): the
    plan uses the Gateway install's path, `.install4j`, bundled JRE and
    `ibgateway.vmoptions` (the Gateway dir has no `tws.vmoptions`), keeps the
    TWS entry class `jclient.LoginFrame`, and never writes the Gateway-only
    `[IBGateway] ApiOnly` into jts.ini -- the app genuinely runs as a TWS
    front-end. Native-restart prevention renames the resolved Gateway bundle,
    and the fallback is logged (IBC switches silently; this project never does)."""
    base = _make_synthetic_install(tmp_path, os_name="macos", program="gateway")
    settings_dir = tmp_path / "settings"
    config = _config(
        program="tws",
        tws_path=str(base),
        tws_settings_path=str(settings_dir),
        instance="paper",
    )
    with caplog.at_level(logging.WARNING):
        plan = build_launch_plan(
            config,
            tmp_path / "agent.jar",
            os_name="macos",
            runtime_dir=tmp_path / "run",
        )

    assert "jclient.LoginFrame" in plan.command
    assert "ibgateway.GWClient" not in plan.command
    assert "-Xmx768m" in plan.command  # from ibgateway.vmoptions
    assert "--add-opens=x/y=ALL-UNNAMED" in plan.command  # from the Gateway Info.plist
    assert "Trader Workstation" not in plan.command
    assert f"-DjtsConfigDir={settings_dir}" in plan.command
    assert "[IBGateway]" not in (settings_dir / "jts.ini").read_text()
    assert any(
        "falling back to the gateway installation" in r.message for r in caplog.records
    )

    program_path = base / "IB Gateway 10.50"
    assert not (program_path / "IB Gateway 10.50.app").exists()
    assert (program_path / "IB Gateway 10.50-1.app").is_dir()


def test_build_launch_plan_macos_tws_prefers_tws_install_when_present(tmp_path, caplog):
    """No fallback when a TWS install exists -- the TWS dir wins, no warning
    logged (IBC's own priority; gitea #25)."""
    base = _make_synthetic_install(tmp_path, os_name="macos", program="tws")
    _make_version_dir(tmp_path / "Applications", "IB Gateway 10.50")
    settings_dir = tmp_path / "settings"
    config = _config(
        program="tws",
        tws_path=str(base),
        tws_settings_path=str(settings_dir),
        instance="paper",
    )
    with caplog.at_level(logging.WARNING):
        plan = build_launch_plan(
            config,
            tmp_path / "agent.jar",
            os_name="macos",
            runtime_dir=tmp_path / "run",
        )

    assert "jclient.LoginFrame" in plan.command
    assert "Trader Workstation 10.50" in str(plan.command)
    assert not any("falling back" in r.message for r in caplog.records)


def test_build_launch_plan_macos_tws_auto_detects_version_in_gateway_tree(tmp_path):
    """`tws_version` unset + no TWS install at all: version detection itself
    runs against the Gateway tree (channel-aware), then the plan resolves the
    Gateway install -- the whole TWS-from-Gateway path end to end."""
    base = _make_synthetic_install(tmp_path, os_name="macos", program="gateway")
    (base / "IB Gateway 10.50" / ".install4j" / "i4jparams.conf").write_text(
        '<variable name="channel" value="stable" />\n'
    )
    config = _config(
        program="tws",
        tws_path=str(base),
        tws_settings_path=str(tmp_path / "settings"),
        instance="paper",
        tws_version=None,
    )
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="macos", runtime_dir=tmp_path / "run"
    )
    assert "IB Gateway 10.50" in str(plan.command)
    assert "jclient.LoginFrame" in plan.command


def test_build_launch_plan_linux_tws_falls_back_to_ibgateway(tmp_path):
    base = _make_synthetic_install(tmp_path, os_name="linux", program="gateway")
    settings_dir = tmp_path / "settings"
    config = _config(
        program="tws",
        tws_path=str(base),
        tws_settings_path=str(settings_dir),
        instance="paper",
    )
    plan = build_launch_plan(
        config, tmp_path / "agent.jar", os_name="linux", runtime_dir=tmp_path / "run"
    )
    assert "jclient.LoginFrame" in plan.command
    assert "ibgateway" in str(plan.command)
    assert f"-DjtsConfigDir={settings_dir}" in plan.command


def test_build_launch_plan_default_runtime_dir_uses_platformdirs(tmp_path, monkeypatch):
    """No `runtime_dir` override -- falls back to `app_dirs.resolve_runtime_dir()`,
    same "Docker mode" env-var override as `resolve_app_dirs`."""
    monkeypatch.setenv("IBC_APP_DIR", str(tmp_path / "appdir"))
    base = _make_synthetic_install(tmp_path, os_name="macos")
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(tmp_path / "settings"),
        instance="paper",
    )
    plan = build_launch_plan(config, tmp_path / "agent.jar", os_name="macos")

    expected_dir = tmp_path / "appdir" / "run"
    assert plan.command_socket_path == str(
        expected_dir / "ibcontroller-agent-paper-cmd.sock"
    )
    assert expected_dir.is_dir()


async def test_build_launch_plan_via_anyio_to_thread_does_not_block_event_loop(
    tmp_path,
):
    """`launch_instance` calls `build_launch_plan` through
    `anyio.to_thread.run_sync`, not directly -- its synchronous filesystem
    chain (`_ensure_jts_ini`, `_detect_tws_version`, `_build_classpath`,
    `_find_java_bin`, ...) must not stall the event loop while it runs. A
    concurrent counter task on a tight `asyncio.sleep(0)` loop must keep
    advancing while the wrapped call is still in flight."""
    base = _make_synthetic_install(tmp_path, os_name="linux")
    config = _config(
        program="gateway",
        tws_path=str(base),
        tws_settings_path=str(tmp_path / "settings"),
        instance="paper",
    )
    counter = 0
    stop = asyncio.Event()

    async def tick() -> None:
        nonlocal counter
        while not stop.is_set():
            counter += 1
            await asyncio.sleep(0)

    ticker = asyncio.ensure_future(tick())
    try:
        plan = await anyio.to_thread.run_sync(
            functools.partial(
                build_launch_plan,
                config,
                tmp_path / "agent.jar",
                os_name="linux",
                runtime_dir=tmp_path / "run",
            )
        )
    finally:
        stop.set()
        await ticker

    assert plan.settings_dir == str(tmp_path / "settings")
    assert counter > 0


# --- _wait_for_ready (async, needs a real socket -- fake server, no Gateway) --------


async def test_wait_for_ready_succeeds_once_agent_answers_ping(sock_path):
    """The agent isn't listening immediately -- _wait_for_ready must retry, not
    fail on the first attempt."""

    async def start_server_after_delay():
        await asyncio.sleep(0.15)
        async with FakeCommandServer(
            sock_path, lambda _req: {"ok": True, "version": "0.0.1-dev", "uptime_s": 0}
        ):
            await asyncio.sleep(1)  # stay up long enough for the poll to succeed

    server_task = asyncio.create_task(start_server_after_delay())
    try:
        await _wait_for_ready(str(sock_path), timeout=2.0)
    finally:
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await server_task


async def test_wait_for_ready_times_out_when_nothing_ever_listens(sock_path):
    with pytest.raises(LauncherError, match="did not respond to ping"):
        await _wait_for_ready(str(sock_path), timeout=0.5)


# --- _drain_stdout (async, off-loop write via configure_gateway_stdout) -------------


class _FakeStdoutPipe:
    """Just enough of `asyncio.StreamReader` for `_drain_stdout`: `readline()`
    hands back one queued line at a time, then `b""` (EOF) once exhausted --
    matching a real pipe once the process exits."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProcessWithStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _FakeStdoutPipe(lines)


async def test_drain_stdout_writes_lines_through_the_queued_stdout_logger(tmp_path):
    """`_drain_stdout` hands each decoded line to a `configure_gateway_stdout`
    logger instead of writing/flushing a file itself -- the real write
    happens on that logger's own listener thread. The resulting file still
    ends up with the same content a direct write would have produced."""
    stdout_logger = configure_gateway_stdout("paper", tmp_path, sink="file")
    process = _FakeProcessWithStdout([b"line one\n", b"line two\n"])

    # pyrefly: ignore [bad-argument-type]
    await _drain_stdout(process, stdout_logger)
    stop_logging()

    assert (tmp_path / "gateway-paper.log").read_text() == "line one\nline two\n"


async def test_drain_stdout_std_sink_reaches_console_not_a_file(tmp_path, capsys):
    """sink="std" (the default, gitea #26) routes Gateway/TWS's own console
    output through a `StreamHandler` instead of `gateway-{instance}.log`, so it
    reaches `docker logs` -- no file is ever created."""
    stdout_logger = configure_gateway_stdout("paper", tmp_path)
    process = _FakeProcessWithStdout([b"line one\n", b"line two\n"])

    # pyrefly: ignore [bad-argument-type]
    await _drain_stdout(process, stdout_logger)
    stop_logging()

    assert not (tmp_path / "gateway-paper.log").exists()
    assert "line one\nline two\n" in capsys.readouterr().err


# --- clean_shutdown (async, needs a real socket -- fake server, no Gateway) ---------


class _FakeProcess:
    """Just enough of `asyncio.subprocess.Process` for `clean_shutdown`:
    a settable `returncode`, a `terminate()` that's recorded and unblocks
    `wait()`, and a `wait()` that otherwise hangs until terminated -- matching
    a real process that doesn't exit on its own just because a menu item was
    clicked."""

    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode
        self.terminated = False
        self._wait_event = asyncio.Event()
        if returncode is not None:
            self._wait_event.set()

    def terminate(self) -> None:
        # Matches real asyncio.subprocess.Process: signalling an OS PID that
        # no longer exists raises ProcessLookupError -- this is exactly the
        # real, live-caught bug clean_shutdown's "already exited" branch had
        # to stop hitting (2026-09-06).
        if self.returncode is not None:
            raise ProcessLookupError()
        self.terminated = True
        self._wait_event.set()

    async def wait(self) -> int:
        await self._wait_event.wait()
        return self.returncode if self.returncode is not None else -15


async def _start_dispatcher(cmd_sock, event_sock) -> Dispatcher:
    dispatcher = Dispatcher(
        AgentCommandConnection(cmd_sock), AgentEventConnection(event_sock)
    )
    await dispatcher.start()
    return dispatcher


def _launched(process, dispatcher, sock_path, event_sock_path) -> LaunchedInstance:
    return LaunchedInstance(
        process=process,
        dispatcher=dispatcher,
        command_socket_path=str(sock_path),
        event_socket_path=str(event_sock_path),
        settings_dir=f"{gettempdir()}/settings",
        stdout_drain_task=asyncio.ensure_future(asyncio.sleep(0)),
    )


async def test_clean_shutdown_terminates_directly_when_process_already_exited(
    sock_path, event_sock_path
):
    """Regression test for two real, live-caught bugs, both hit back to back
    on 2026-09-06 testing the exact same scenario (the user closing Gateway
    manually before `clean_shutdown` ever ran): first, a raw
    `ConnectionResetError` from attempting `navigate_menu` over a dead
    connection (the old `except AgentClientError` never caught it); then,
    once that was fixed to check `returncode` first, a `ProcessLookupError`
    from this same branch still calling `terminate()` on a process that no
    longer has an OS PID to signal. Both are asserted here: no command sent,
    and `terminate()` never called (the fake raises `ProcessLookupError` if
    it is, matching real `asyncio.subprocess.Process` behaviour)."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start_dispatcher(sock_path, event_sock_path)
        process = _FakeProcess(returncode=0)
        launched = _launched(process, dispatcher, sock_path, event_sock_path)
        await clean_shutdown(
            launched, program="gateway", logged_in=True, labels=LABELS.shutdown
        )

    assert not process.terminated  # never called -- would have raised otherwise
    assert calls == []  # never attempted navigate_menu against the dead process


async def test_clean_shutdown_terminates_when_never_logged_in_and_still_running(
    sock_path, event_sock_path
):
    """The other branch that calls `terminate()` -- login never completed, but
    the process is genuinely still alive, so `terminate()` must actually be
    called here (unlike the already-exited case above)."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start_dispatcher(sock_path, event_sock_path)
        process = _FakeProcess(returncode=None)
        launched = _launched(process, dispatcher, sock_path, event_sock_path)
        await clean_shutdown(
            launched, program="gateway", logged_in=False, labels=LABELS.shutdown
        )

    assert process.terminated
    assert calls == []  # never logged in -- no graceful menu attempt at all


async def test_clean_shutdown_uses_graceful_navigate_menu_when_process_alive(
    sock_path, event_sock_path
):
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start_dispatcher(sock_path, event_sock_path)
        process = _FakeProcess(returncode=None)
        launched = _launched(process, dispatcher, sock_path, event_sock_path)
        # A short timeout: this fake process never exits on its own after the
        # click (no real Gateway behind it), so the fallback terminate() path
        # fires -- exercised deliberately, not the point of this test.
        await clean_shutdown(
            launched,
            program="gateway",
            logged_in=True,
            labels=LABELS.shutdown,
            timeout=0.1,
        )

    assert {"cmd": "navigate_menu", "path": "File/Close"} in calls


async def test_clean_shutdown_uses_tws_menu_path_for_tws(sock_path, event_sock_path):
    """Real, live-caught bug (2026-09-09, an audit prompted by two other
    hardcoded-label bugs the same day): the graceful-shutdown menu path used
    to be a hardcoded `"File/Close"`/`"File/Exit"` ternary directly in
    `clean_shutdown`, not sourced from `labels.json` like every other
    user-facing string this project matches or clicks against. Confirms
    `program="tws"` selects `labels.shutdown.tws_menu_path`, not Gateway's."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        return {"ok": True, "clicked": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start_dispatcher(sock_path, event_sock_path)
        process = _FakeProcess(returncode=None)
        launched = _launched(process, dispatcher, sock_path, event_sock_path)
        await clean_shutdown(
            launched,
            program="tws",
            logged_in=True,
            labels=LABELS.shutdown,
            timeout=0.1,
        )

    assert {"cmd": "navigate_menu", "path": "File/Exit"} in calls
    assert {"cmd": "navigate_menu", "path": "File/Close"} not in calls
