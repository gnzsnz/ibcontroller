"""Unit tests for settings.py (L5's Settings domain) -- no live Gateway needed, same
fake-socket-server pattern the rest of this project's tests use."""

from __future__ import annotations

import asyncio
import logging
from tempfile import gettempdir

import attrs
import pytest
from typed_settings.types import Secret

from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    ElementNotFoundError,
    WindowEvent,
    WindowInfo,
)
from ibcontroller.config import Config, TradingMode
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import load_labels
from ibcontroller.logging_setup import configure_logging, stop_logging
from ibcontroller.settings import (
    SettingEntry,
    SettingsError,
    SettingsFile,
    _parse_time_ampm,
    apply_settings_from_file,
    close_settings_dialog,
    load_builtin_settings_file,
    load_settings_file,
    merge_settings_files,
    open_settings_dialog,
)
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio

LABELS = load_labels()


def _config(**overrides) -> Config:
    base = Config(
        program="gateway",
        tws_version="10.50",
        trading_mode=TradingMode.PAPER,
        userid=Secret("u"),
        password=Secret("p"),
        log_dir=f"{gettempdir()}/log",
    )
    return attrs.evolve(base, **overrides)


async def _start(cmd_sock, event_sock) -> Dispatcher:
    dispatcher = Dispatcher(
        AgentCommandConnection(cmd_sock), AgentEventConnection(event_sock)
    )
    await dispatcher.start()
    return dispatcher


def _window_event(
    seq: int, kind: str, class_: str, title: str, window_id: str | None = None
) -> WindowEvent:
    window = WindowInfo(class_=class_, title=title, window_id=window_id)
    return WindowEvent(seq=seq, kind=kind, window=window)


def _tracking_responder(calls: list[dict]):
    """`_await_menu_ready`'s own `dump` check needs a `components` key (empty is
    fine -- these tests aren't exercising the splash-frame-still-open branch);
    everything else just needs `{"ok": True}`, matching `test_login.py`'s own
    `_tracking_responder`."""

    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
        return {"ok": True}

    return responder


# --- load_settings_file -------------------------------------------------------


async def test_load_settings_file_parses_label_and_label_ref_entries(tmp_path):
    toml_path = tmp_path / "ibkr_settings.toml"
    toml_path.write_text(
        """
        [[settings]]
        tree_path = "API/Settings"
        action = "toggle"
        label_ref = "read_only_api"
        value_from_config = "read_only_api"

        [[settings]]
        tree_path = "API/Precautions"
        action = "toggle"
        label = "Bypass Order Precautions for API Orders"
        value = true
        """
    )
    settings_file = await load_settings_file(toml_path)
    assert len(settings_file.settings) == 2
    assert settings_file.settings[0].label_ref == "read_only_api"
    assert settings_file.settings[0].value_from_config == "read_only_api"
    assert settings_file.settings[1].label == "Bypass Order Precautions for API Orders"
    assert settings_file.settings[1].value is True


async def test_load_settings_file_missing_settings_key_is_empty(tmp_path):
    toml_path = tmp_path / "ibkr_settings.toml"
    toml_path.write_text("")
    settings_file = await load_settings_file(toml_path)
    assert settings_file.settings == []


async def test_load_settings_file_missing_file_raises_settings_error(tmp_path):
    missing_path = tmp_path / "does_not_exist.toml"
    with pytest.raises(SettingsError, match="could not read"):
        await load_settings_file(missing_path)


async def test_load_settings_file_malformed_toml_raises_settings_error(tmp_path):
    toml_path = tmp_path / "ibkr_settings.toml"
    toml_path.write_text("[[settings]\nthis is not valid toml")
    with pytest.raises(SettingsError, match="not valid TOML"):
        await load_settings_file(toml_path)


async def test_load_settings_file_invalid_structure_raises_settings_error(tmp_path):
    toml_path = tmp_path / "ibkr_settings.toml"
    toml_path.write_text('settings = "not a list of tables"')
    with pytest.raises(SettingsError, match="invalid structure"):
        await load_settings_file(toml_path)


async def test_load_settings_file_warns_eagerly_on_non_eligible_value_from_config(
    tmp_path, caplog
):
    """A `value_from_config` naming anything outside `_SETTINGS_ELIGIBLE_FIELDS`
    can never resolve (`Config`'s schema is closed) -- warned at load time, well
    before any Gateway launch, in addition to the existing apply-time skip."""
    toml_path = tmp_path / "ibkr_settings.toml"
    toml_path.write_text(
        """
        [[settings]]
        tree_path = "API/Precautions"
        action = "toggle"
        label_ref = "api_precautions_bypass_order_precautions"
        value_from_config = "api_precautions_bypass_order_precautions"
        """
    )
    with caplog.at_level(logging.WARNING, logger="ibcontroller.settings"):
        settings_file = await load_settings_file(toml_path)
    assert len(settings_file.settings) == 1  # still loaded, not dropped
    assert any(
        "api_precautions_bypass_order_precautions" in message
        for message in caplog.messages
    )


async def test_load_settings_file_no_warning_for_eligible_value_from_config(
    tmp_path, caplog
):
    toml_path = tmp_path / "ibkr_settings.toml"
    toml_path.write_text(
        """
        [[settings]]
        tree_path = "API/Settings"
        action = "toggle"
        label_ref = "read_only_api"
        value_from_config = "read_only_api"
        """
    )
    with caplog.at_level(logging.WARNING, logger="ibcontroller.settings"):
        await load_settings_file(toml_path)
    assert caplog.messages == []


# --- apply_settings_from_file --------------------------------------------------


async def test_apply_settings_resolves_label_ref_and_value_from_config(
    sock_path, event_sock_path
):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(read_only_api=False)
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Settings",
                        action="toggle",
                        label_ref="read_only_api",
                        value_from_config="read_only_api",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    assert {"cmd": "expand_tree", "path": "API/Settings"} in calls
    assert {"cmd": "set_checkbox", "target": "Read-Only API", "checked": False} in calls


async def test_apply_settings_resolves_literal_label_and_value(
    sock_path, event_sock_path
):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Precautions",
                        action="toggle",
                        label="Bypass Order Precautions for API Orders",
                        value=True,
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    assert {"cmd": "expand_tree", "path": "API/Precautions"} in calls
    assert {
        "cmd": "set_checkbox",
        "target": "Bypass Order Precautions for API Orders",
        "checked": True,
    } in calls


async def test_apply_settings_logs_each_entry_applied_and_skipped(
    sock_path, event_sock_path, tmp_path
):
    """A real, previously-named gap (2026-09-07): `apply_settings_from_file` had
    zero `logger.*` calls, so there was no way to confirm from a log which
    entries actually applied vs. were silently skipped (a `None`-resolved
    `value_from_config`, e.g. `read_only_api` left unset) -- only by eye in the
    live dialog. File-based, not `caplog`, matching `test_recognisers.py`'s own
    pattern -- `configure_logging` disables propagation to the root logger on
    purpose."""
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    responder = _tracking_responder([])
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()  # read_only_api left at its None default
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Precautions",
                        action="toggle",
                        label="Bypass Order Precautions for API Orders",
                        value=True,
                    ),
                    SettingEntry(
                        tree_path="API/Settings",
                        action="toggle",
                        label_ref="read_only_api",
                        value_from_config="read_only_api",
                    ),
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    # Records reach the file via configure_logging's listener thread (2026-09-08);
    # draining it is what guarantees they've been written before we read.
    stop_logging()
    log_text = (tmp_path / "test.log").read_text()
    assert (
        "applying API/Precautions (Bypass Order Precautions for API Orders) "
        "toggle = True" in log_text
    )
    assert "skipping API/Settings (read_only_api) -- value resolved to None" in log_text


async def test_apply_settings_scopes_every_action_to_the_given_window_id(
    sock_path, event_sock_path
):
    """`window_id` (2026-09-06) -- `_apply_declarative_settings` (control_loop.py)
    always passes `open_settings_dialog`'s own return value here, scoping every
    `expand_tree`/`toggle` call to the one Configuration dialog that opened,
    instead of an unscoped, whole-JVM search."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Precautions",
                        action="toggle",
                        label="Bypass Order Precautions for API Orders",
                        value=True,
                    )
                ]
            )
            await apply_settings_from_file(
                dispatcher, LABELS, config, settings_file, window_id="w9"
            )
        finally:
            await dispatcher.stop()

    assert {"cmd": "expand_tree", "path": "API/Precautions", "window_id": "w9"} in calls
    assert {
        "cmd": "set_checkbox",
        "target": "Bypass Order Precautions for API Orders",
        "checked": True,
        "window_id": "w9",
    } in calls


async def test_apply_settings_skips_entry_when_resolved_value_is_none(
    sock_path, event_sock_path
):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(read_only_api=None)  # explicit "leave unchanged"
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Settings",
                        action="toggle",
                        label_ref="read_only_api",
                        value_from_config="read_only_api",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    assert calls == []


async def test_apply_settings_type_text_action(sock_path, event_sock_path):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="type_text",
                        label="Some Time Field",
                        value="09:30 PM",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "set_text",
        "target": "Some Time Field",
        "value": "09:30 PM",
    } in calls


async def test_apply_settings_type_text_near_label_action(sock_path, event_sock_path):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="type_text_near_label",
                        label_ref="auto_restart_time_label",
                        field_index=0,
                        value="09:30",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "set_text_near_label",
        "label": "Set Auto Restart Time (HH:MM)",
        "index": 0,
        "value": "09:30",
    } in calls


def test_parse_time_ampm_am():
    assert _parse_time_ampm("08:00 AM", setting_name="auto_restart_time") == (
        "08:00",
        "AM",
    )


def test_parse_time_ampm_pm():
    assert _parse_time_ampm("04:00 PM", setting_name="auto_restart_time") == (
        "04:00",
        "PM",
    )


def test_parse_time_ampm_zero_pads_single_digit_hour():
    assert _parse_time_ampm("9:05 AM", setting_name="auto_restart_time") == (
        "09:05",
        "AM",
    )


def test_parse_time_ampm_rejects_bad_format():
    with pytest.raises(SettingsError, match="hh:mm AM or hh:mm PM"):
        _parse_time_ampm("not a time", setting_name="auto_restart_time")


def test_parse_time_ampm_error_names_the_setting():
    with pytest.raises(SettingsError, match="auto_logoff_time setting must be"):
        _parse_time_ampm("not a time", setting_name="auto_logoff_time")


async def test_apply_settings_auto_restart_time_action(sock_path, event_sock_path):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(auto_restart_time="11:59 PM")
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="auto_restart_time",
                        value_from_config="auto_restart_time",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    # The fallback tries the Auto Log Off label first (matching IBC's own
    # order) -- the fake responder answers "ok" for it, so that's the one
    # that actually lands here, not the Auto Restart label.
    assert {
        "cmd": "set_text_near_label",
        "label": "Set Auto Log Off Time (HH:MM)",
        "index": 0,
        "value": "11:59",
    } in calls
    assert {"cmd": "set_checkbox", "target": "PM", "checked": True} in calls
    assert {"cmd": "set_checkbox", "target": "Auto restart", "checked": True} in calls
    assert {"cmd": "expand_tree", "path": "Lock and Exit"} in calls


async def test_apply_settings_auto_restart_time_falls_back_to_restart_label(
    sock_path, event_sock_path
):
    """Live-caught 2026-09-06: a fresh settings dir defaults to "Auto logoff", so
    the time field's own label reads "Set Auto Log Off Time (HH:MM)" until "Auto
    restart" is actually selected. This exercises the other half of the
    fallback -- the logoff label doesn't exist (an existing settings dir already
    on "Auto restart"), so the restart label must be tried next."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        if (
            request.get("cmd") == "set_text_near_label"
            and request.get("label") == "Set Auto Log Off Time (HH:MM)"
        ):
            return {"ok": False, "error": "not_found", "detail": "boom"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(auto_restart_time="08:00 AM")
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="auto_restart_time",
                        value_from_config="auto_restart_time",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "set_text_near_label",
        "label": "Set Auto Restart Time (HH:MM)",
        "index": 0,
        "value": "08:00",
    } in calls


async def test_apply_settings_auto_restart_time_none_skips_entry(
    sock_path, event_sock_path
):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(auto_restart_time=None)
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="auto_restart_time",
                        value_from_config="auto_restart_time",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    assert calls == []


async def test_apply_settings_auto_restart_time_bad_value_is_logged_and_skipped(
    sock_path, event_sock_path, tmp_path
):
    """Per-entry isolation (2026-09-12, issue #1): a malformed entry no longer
    aborts the whole file -- it's logged and skipped, matching every other
    `SettingsError`/`ElementNotFoundError` case below."""
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(auto_restart_time="not a time")
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="auto_restart_time",
                        value_from_config="auto_restart_time",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert "hh:mm AM or hh:mm PM" in (tmp_path / "test.log").read_text()


async def test_apply_settings_auto_logoff_time_action(sock_path, event_sock_path):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(auto_logoff_time="11:59 PM")
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="auto_logoff_time",
                        value_from_config="auto_logoff_time",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "set_text_near_label",
        "label": "Set Auto Log Off Time (HH:MM)",
        "index": 0,
        "value": "11:59",
    } in calls
    assert {"cmd": "set_checkbox", "target": "PM", "checked": True} in calls
    assert {"cmd": "set_checkbox", "target": "Auto logoff", "checked": True} in calls
    assert {"cmd": "expand_tree", "path": "Lock and Exit"} in calls


async def test_apply_settings_auto_logoff_time_falls_back_to_restart_label(
    sock_path, event_sock_path
):
    """Same fallback as auto_restart_time -- the time field's own accessible
    label is shared between the two actions, so auto_logoff_time must fall
    back to the Auto Restart label too when the settings dir is already on
    "Auto restart"."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        if (
            request.get("cmd") == "set_text_near_label"
            and request.get("label") == "Set Auto Log Off Time (HH:MM)"
        ):
            return {"ok": False, "error": "not_found", "detail": "boom"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(auto_logoff_time="08:00 AM")
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="auto_logoff_time",
                        value_from_config="auto_logoff_time",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "set_text_near_label",
        "label": "Set Auto Restart Time (HH:MM)",
        "index": 0,
        "value": "08:00",
    } in calls


async def test_apply_settings_auto_logoff_time_none_skips_entry(
    sock_path, event_sock_path
):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(auto_logoff_time=None)
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="auto_logoff_time",
                        value_from_config="auto_logoff_time",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    assert calls == []


async def test_apply_settings_auto_logoff_time_bad_value_is_logged_and_skipped(
    sock_path, event_sock_path, tmp_path
):
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(auto_logoff_time="not a time")
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="auto_logoff_time",
                        value_from_config="auto_logoff_time",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert "hh:mm AM or hh:mm PM" in (tmp_path / "test.log").read_text()


async def test_apply_settings_multiple_entries_across_different_tree_paths(
    sock_path, event_sock_path
):
    """The one previously-untested gap (TODO.md, 2026-09-06): every prior test
    applied exactly one entry. This applies two entries -- `read_only_api`
    (API/Settings) and `auto_restart_time` (Lock and Exit) -- in one
    `settings_file`/one dialog session, confirming `expand_tree` is called
    again for each entry's own `tree_path`, in order, not just once up front."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config(read_only_api=False, auto_restart_time="08:00 AM")
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Settings",
                        action="toggle",
                        label_ref="read_only_api",
                        value_from_config="read_only_api",
                    ),
                    SettingEntry(
                        tree_path="Lock and Exit",
                        action="auto_restart_time",
                        value_from_config="auto_restart_time",
                    ),
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()

    tree_paths_in_order = [c["path"] for c in calls if c.get("cmd") == "expand_tree"]
    assert tree_paths_in_order == ["API/Settings", "Lock and Exit"]
    assert {"cmd": "set_checkbox", "target": "Read-Only API", "checked": False} in calls
    assert {"cmd": "set_checkbox", "target": "AM", "checked": True} in calls


async def test_apply_settings_unresolvable_label_ref_is_logged_and_skipped(
    sock_path, event_sock_path, tmp_path
):
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Settings",
                        action="toggle",
                        label_ref="no_such_control",
                        value=True,
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert "no_such_control" in (tmp_path / "test.log").read_text()


async def test_apply_settings_unresolvable_value_from_config_is_logged_and_skipped(
    sock_path, event_sock_path, tmp_path
):
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Settings",
                        action="toggle",
                        label_ref="read_only_api",
                        value_from_config="no_such_field",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert "no_such_field" in (tmp_path / "test.log").read_text()


async def test_apply_settings_value_from_config_cannot_reach_credentials(
    sock_path, event_sock_path, tmp_path
):
    """`_resolve_value`'s allow-list (`_SETTINGS_ELIGIBLE_FIELDS`), not a bare
    `hasattr` check -- a `value_from_config` naming a real but credential
    `Config` field (`userid`/`password`, `Secret`-wrapped) is rejected the same
    way as any other non-eligible field, by design, not by `Secret` merely
    failing a later `isinstance` check."""
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Settings",
                        action="type_text",
                        label_ref="read_only_api",
                        value_from_config="userid",
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert "userid" in (tmp_path / "test.log").read_text()


async def test_apply_settings_unsupported_action_is_logged_and_skipped(
    sock_path, event_sock_path, tmp_path
):
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Settings",
                        action="click",
                        label="Reset API order ID sequence",
                        value=True,
                    )
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert "unsupported settings action" in (tmp_path / "test.log").read_text()


async def test_apply_settings_entry_needs_label_or_label_ref_is_logged_and_skipped(
    sock_path, event_sock_path, tmp_path
):
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(tree_path="API/Settings", action="toggle", value=True)
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert "label" in (tmp_path / "test.log").read_text()


async def test_apply_settings_entry_failure_does_not_block_later_entries(
    sock_path, event_sock_path, tmp_path
):
    """Per-entry isolation (2026-09-12, issue #1) -- the real, live-caught bug:
    an entry referencing a nonexistent Config field used to abort every entry
    after it in the same file (see the module docstring's own note on this).
    Here the first entry is exactly that shape; the second entry's action
    must still run."""
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            config = _config()
            settings_file = SettingsFile(
                settings=[
                    SettingEntry(
                        tree_path="API/Precautions",
                        action="toggle",
                        label_ref="api_precautions_bypass_order_precautions",
                        value_from_config="api_precautions_bypass_order_precautions",
                    ),
                    SettingEntry(
                        tree_path="API/Precautions",
                        action="toggle",
                        label="Bypass Bond warning for API Orders.",
                        value=True,
                    ),
                ]
            )
            await apply_settings_from_file(dispatcher, LABELS, config, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert (
        "api_precautions_bypass_order_precautions"
        in (tmp_path / "test.log").read_text()
    )
    assert {
        "cmd": "set_checkbox",
        "target": "Bypass Bond warning for API Orders.",
        "checked": True,
    } in calls


# --- open_settings_dialog / close_settings_dialog ------------------------------


async def test_open_settings_dialog_navigates_and_waits_for_configuration_window(
    sock_path, event_sock_path
):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            task = asyncio.ensure_future(
                open_settings_dialog(dispatcher, LABELS.settings, timeout=5.0)
            )
            await asyncio.sleep(0.02)
            dispatcher.window_events.emit(
                _window_event(
                    1,
                    "window_opened",
                    "feature.configure.ai",
                    "DU123 Trader Workstation Configuration (Simulated Trading)",
                    window_id="w9",
                )
            )
            window_id = await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert {"cmd": "navigate_menu", "path": "Configure/Settings"} in calls
    assert window_id == "w9"


async def test_open_settings_dialog_uses_tws_classic_menu_path(
    sock_path, event_sock_path
):
    """Real, live-caught bug (2026-09-09): TWS has no `Configure` menu at all --
    `navigate_menu("Configure/Settings")` (Gateway's own path) fails with
    `not_found` on a real TWS install. `program="tws"` must select
    `labels.settings.tws_menu_path_classic` (`"Edit/Global
    Configuration..."`) instead. Classic-only for now -- Mosaic fallback was
    tried during #37 but dropped from that fix's scope, tracked separately
    as #39."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            task = asyncio.ensure_future(
                open_settings_dialog(
                    dispatcher,
                    LABELS.settings,
                    program="tws",
                    timeout=5.0,
                )
            )
            await asyncio.sleep(0.02)
            dispatcher.window_events.emit(
                _window_event(
                    1,
                    "window_opened",
                    "feature.configure.ai",
                    "DU123 Trader Workstation Configuration (Simulated Trading)",
                    window_id="w9",
                )
            )
            window_id = await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "navigate_menu",
        "path": "Edit/Global Configuration...",
    } in calls
    assert {"cmd": "navigate_menu", "path": "File/Global Configuration..."} not in calls
    assert {"cmd": "navigate_menu", "path": "Configure/Settings"} not in calls
    assert window_id == "w9"


async def test_open_settings_dialog_retries_missing_menu_item_before_failing(
    sock_path, event_sock_path
):
    """#37: a menu item missing because TWS hasn't finished populating its
    menubar yet (not because the path is wrong) must be retried, not raised
    immediately -- `navigate_menu` (actions.py) now treats
    `ElementNotFoundError` as retriable, same as a disabled item."""
    calls: list[dict] = []
    attempts = {"n": 0}

    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
        if (
            request.get("cmd") == "navigate_menu"
            and request.get("path") == "Configure/Settings"
        ):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return {"ok": False, "error": "not_found", "detail": "boom"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            task = asyncio.ensure_future(
                open_settings_dialog(dispatcher, LABELS.settings, timeout=5.0)
            )
            await asyncio.sleep(0.02)
            dispatcher.window_events.emit(
                _window_event(
                    1,
                    "window_opened",
                    "feature.configure.ai",
                    "DU123 Trader Workstation Configuration (Simulated Trading)",
                    window_id="w9",
                )
            )
            window_id = await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert attempts["n"] == 3
    assert window_id == "w9"


async def test_open_settings_dialog_cleans_up_armed_wait_on_navigate_failure(
    sock_path, event_sock_path
):
    """#38: when `navigate_menu` ultimately raises (the TWS Classic path
    exhausted here), the dialog-open `wait_for_event` future armed earlier
    must be cancelled and drained, not left running to its own `timeout` --
    that leak is what produced #38's "Task exception was never retrieved"
    warning. Confirmed here by asserting no task from this call is still
    pending immediately after it raises."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
        if request.get("cmd") == "navigate_menu":
            return {"ok": False, "error": "not_found", "detail": "boom"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        before = asyncio.all_tasks()
        try:
            with pytest.raises(ElementNotFoundError):
                await open_settings_dialog(
                    dispatcher,
                    LABELS.settings,
                    program="tws",
                    timeout=0.6,
                )
        finally:
            await dispatcher.stop()

    assert asyncio.all_tasks() - before == set()


async def test_open_settings_dialog_waits_for_splash_screen_to_close_first(
    sock_path, event_sock_path
):
    """Regression test for a real, live-caught bug (2026-09-05): calling
    navigate_menu("Configure/Settings") right after login returned successfully
    but never opened the dialog -- IBC's own SessionManager.awaitReady() blocks
    on the splash frame ("Starting application...") closing first. Confirms
    open_settings_dialog doesn't touch the menu until that happens, when the
    splash is still open at the time it's called (not just the already-closed
    case the other test above covers)."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            return {"ok": True, "components": [{"class": "javax.swing.JFrame"}]}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            task = asyncio.ensure_future(
                open_settings_dialog(dispatcher, LABELS.settings, timeout=5.0)
            )
            await asyncio.sleep(0.02)
            assert {
                "cmd": "navigate_menu",
                "path": "Configure/Settings",
            } not in calls, "must not touch the menu before the splash closes"

            dispatcher.window_events.emit(
                _window_event(
                    1, "window_closed", "some.splash.class", "Starting application..."
                )
            )
            await asyncio.sleep(0.02)
            dispatcher.window_events.emit(
                _window_event(
                    2,
                    "window_opened",
                    "feature.configure.ai",
                    "DU123 Trader Workstation Configuration (Simulated Trading)",
                )
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert {"cmd": "navigate_menu", "path": "Configure/Settings"} in calls


async def test_close_settings_dialog_clicks_ok(sock_path, event_sock_path):
    """Regression test for a real, live-caught bug (2026-09-06): this used to
    click "Apply", which commits changes but never actually closes the
    dialog -- confirmed against IBC's own source that "OK" (not "Apply") is
    the real commit-and-close button, matching every one of IBC's own
    `ConfigureXXXTask`s."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await close_settings_dialog(dispatcher, LABELS.settings)
        finally:
            await dispatcher.stop()

    assert {"cmd": "click", "target": "OK"} in calls


async def test_close_settings_dialog_scopes_click_when_window_id_given(
    sock_path, event_sock_path
):
    """Without this, the "OK" click here is exactly the kind of unscoped
    search that caused the *other* real bug found the same day: a declarative
    dismiss rule's own "OK" click landing on this dialog's button instead of
    the popup it meant to dismiss, because both were open at once."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await close_settings_dialog(dispatcher, LABELS.settings, window_id="w9")
        finally:
            await dispatcher.stop()

    assert {"cmd": "click", "target": "OK", "window_id": "w9"} in calls
    assert {"cmd": "click", "target": "Apply"} not in calls


# --- load_builtin_settings_file / merge_settings_files -------------------------


async def test_load_builtin_settings_file_has_read_only_api_and_auto_restart_time():
    """The bundled data/builtin_settings.toml is real, shipped package data --
    not the same file as ibkr_settings.toml.example (that one intentionally
    stays inert, see cli.py's ensure_config_scaffold)."""
    builtin = await load_builtin_settings_file()
    by_key = {e.value_from_config: e for e in builtin.settings}
    assert by_key.keys() == {"read_only_api", "auto_restart_time", "auto_logoff_time"}
    assert by_key["read_only_api"].tree_path == "API/Settings"
    assert by_key["read_only_api"].label_ref == "read_only_api"
    assert by_key["auto_restart_time"].tree_path == "Lock and Exit"
    assert by_key["auto_logoff_time"].tree_path == "Lock and Exit"


async def test_load_builtin_settings_file_applies_auto_logoff_before_auto_restart():
    """Locks in the ordering the shared-radio-group precedence depends on
    (see builtin_settings.toml's own comment) -- auto_restart_time must win
    when both fields are set, which requires it to be applied last."""
    builtin = await load_builtin_settings_file()
    actions_in_order = [e.action for e in builtin.settings]
    assert actions_in_order.index("auto_logoff_time") < actions_in_order.index(
        "auto_restart_time"
    )


def test_merge_settings_files_returns_builtins_only_when_no_user_file():
    builtin = SettingsFile(
        settings=[
            SettingEntry(
                tree_path="API/Settings",
                action="toggle",
                label_ref="read_only_api",
                value_from_config="read_only_api",
            )
        ]
    )
    merged = merge_settings_files(builtin, None)
    assert merged == builtin.settings


def test_merge_settings_files_is_additive_for_non_overlapping_entries():
    builtin = SettingsFile(
        settings=[
            SettingEntry(
                tree_path="API/Settings",
                action="toggle",
                label_ref="read_only_api",
                value_from_config="read_only_api",
            )
        ]
    )
    user_entry = SettingEntry(
        tree_path="API/Precautions",
        action="toggle",
        label_ref="api_precautions_bypass_bond_warning",
        value=True,
    )
    user = SettingsFile(settings=[user_entry])

    merged = merge_settings_files(builtin, user)

    assert merged == [builtin.settings[0], user_entry]


def test_merge_settings_files_user_entry_replaces_builtin_with_same_value_from_config():
    """The real case this exists for: a future TWS/Gateway release moves
    read_only_api's control to a different tree_path/label -- a user's own
    entry, sharing the same value_from_config, replaces the built-in one
    rather than both firing."""
    builtin_entry = SettingEntry(
        tree_path="API/Settings",
        action="toggle",
        label_ref="read_only_api",
        value_from_config="read_only_api",
    )
    override_entry = SettingEntry(
        tree_path="API/Settings/Read-Only",
        action="toggle",
        label="Read-Only API Access",
        value_from_config="read_only_api",
    )
    builtin = SettingsFile(settings=[builtin_entry])
    user = SettingsFile(settings=[override_entry])

    merged = merge_settings_files(builtin, user)

    assert merged == [override_entry]
    assert builtin_entry not in merged


def test_merge_settings_files_preserves_builtin_then_user_order():
    builtin_a = SettingEntry(
        tree_path="A", action="toggle", label="a", value_from_config="a"
    )
    builtin_b = SettingEntry(
        tree_path="B", action="toggle", label="b", value_from_config="b"
    )
    override_a = SettingEntry(
        tree_path="A2", action="toggle", label="a2", value_from_config="a"
    )
    user_extra = SettingEntry(tree_path="C", action="toggle", label="c", value=True)
    builtin = SettingsFile(settings=[builtin_a, builtin_b])
    user = SettingsFile(settings=[override_a, user_extra])

    merged = merge_settings_files(builtin, user)

    assert merged == [override_a, builtin_b, user_extra]
