"""Unit tests for settings.py (L5's Settings domain) -- no live Gateway needed, same
fake-socket-server pattern the rest of this project's tests use."""

from __future__ import annotations

import asyncio
import logging
import types
from tempfile import gettempdir

import attrs
import pytest
from typed_settings.types import Secret

from ibcontroller import settings
from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentError,
    AgentEventConnection,
    ElementNotFoundError,
    ProtocolError,
    WindowEvent,
    WindowGoneError,
    WindowInfo,
)
from ibcontroller.config import Config, Program, TradingMode
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import load_labels
from ibcontroller.logging_setup import configure_logging, stop_logging
from ibcontroller.settings import (
    SettingCatalog,
    SettingChoice,
    SettingEntry,
    SettingsError,
    _parse_time_ampm,
    apply_declarative_settings,
    apply_settings,
    close_settings_dialog,
    load_setting_catalog,
    open_settings_dialog,
    parse_custom_settings,
    resolve_settings,
)
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio

LABELS = load_labels()


def _config(**overrides) -> Config:
    base = Config(
        program=Program.GATEWAY,
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
        if request.get("cmd") == "navigate_menu":
            return {"ok": True, "clicked": True}
        return {"ok": True}

    return responder


def _choice(
    *,
    tree_path: str,
    action: str,
    value: bool | str,
    name: str = "setting",
    source: str = "file",
    **fields: object,
) -> SettingChoice:
    entry = SettingEntry(name=name, tree_path=tree_path, action=action, **fields)  # type: ignore[arg-type]
    return SettingChoice(entry, value, source)


_ONE_CHOICE = [
    SettingChoice(
        SettingEntry(
            name="read_only_api",
            tree_path="API/Settings",
            action="toggle",
            label_ref="read_only_api",
        ),
        False,
        "file",
    )
]


def _choices(*, settings: list[SettingChoice]) -> list[SettingChoice]:
    return settings


# --- apply_settings -----------------------------------------------------------


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
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="API/Settings",
                        action="toggle",
                        label_ref="read_only_api",
                        value=False,
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
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
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="API/Precautions",
                        action="toggle",
                        label="Bypass Order Precautions for API Orders",
                        value=True,
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
        finally:
            await dispatcher.stop()

    assert {"cmd": "expand_tree", "path": "API/Precautions"} in calls
    assert {
        "cmd": "set_checkbox",
        "target": "Bypass Order Precautions for API Orders",
        "checked": True,
    } in calls


async def test_apply_settings_logs_each_entry_with_value_and_source(
    sock_path, event_sock_path, tmp_path
):
    """The log shows which setting was written, with what and where the value
    came from. File-based, not `caplog`, matching `test_recognisers.py`'s own
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
            await apply_settings(
                dispatcher,
                LABELS,
                [
                    _choice(
                        tree_path="API/Precautions",
                        action="toggle",
                        label="Bypass Order Precautions for API Orders",
                        value=True,
                        name="bypass_orders",
                        source="env",
                    )
                ],
            )
        finally:
            await dispatcher.stop()

    # Records reach the file via configure_logging's listener thread;
    # draining it is what guarantees they've been written before we read.
    stop_logging()
    log_text = (tmp_path / "test.log").read_text()
    assert "applying bypass_orders = True (env)" in log_text


async def test_apply_settings_scopes_every_action_to_the_given_window_id(
    sock_path, event_sock_path
):
    """`window_id` -- `apply_declarative_settings`
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
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="API/Precautions",
                        action="toggle",
                        label="Bypass Order Precautions for API Orders",
                        value=True,
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file, window_id="w9")
        finally:
            await dispatcher.stop()

    assert {"cmd": "expand_tree", "path": "API/Precautions", "window_id": "w9"} in calls
    assert {
        "cmd": "set_checkbox",
        "target": "Bypass Order Precautions for API Orders",
        "checked": True,
        "window_id": "w9",
    } in calls


async def test_apply_settings_type_text_action(sock_path, event_sock_path):
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="Lock and Exit",
                        action="type_text",
                        label="Some Time Field",
                        value="09:30 PM",
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
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
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="Lock and Exit",
                        action="type_text_near_label",
                        label_ref="auto_restart_time_label",
                        field_index=0,
                        value="09:30",
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
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
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="Lock and Exit",
                        action="auto_restart_time",
                        value="11:59 PM",
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
        finally:
            await dispatcher.stop()

    # The action's own label is tried first and the fake answers "ok", so the
    # Auto Log Off fallback is never sent.
    assert {
        "cmd": "set_text_near_label",
        "label": "Set Auto Restart Time (HH:MM)",
        "index": 0,
        "value": "11:59",
    } in calls
    assert not any(
        call.get("label") == "Set Auto Log Off Time (HH:MM)" for call in calls
    )
    assert {"cmd": "set_checkbox", "target": "PM", "checked": True} in calls
    assert {"cmd": "set_checkbox", "target": "Auto restart", "checked": True} in calls
    assert {"cmd": "expand_tree", "path": "Lock and Exit"} in calls


async def test_apply_settings_auto_restart_time_falls_back_to_logoff_label(
    sock_path, event_sock_path
):
    """A fresh settings dir defaults to "Auto logoff", so the time field reads
    "Set Auto Log Off Time (HH:MM)" until "Auto restart" is selected -- the
    restart label doesn't resolve, so the logoff label must be tried next."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        if (
            request.get("cmd") == "set_text_near_label"
            and request.get("label") == "Set Auto Restart Time (HH:MM)"
        ):
            return {"ok": False, "error": "not_found", "detail": "boom"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="Lock and Exit",
                        action="auto_restart_time",
                        value="08:00 AM",
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "set_text_near_label",
        "label": "Set Auto Log Off Time (HH:MM)",
        "index": 0,
        "value": "08:00",
    } in calls


async def test_apply_settings_auto_restart_time_bad_value_is_logged_and_skipped(
    sock_path, event_sock_path, tmp_path
):
    """Per-entry isolation: a malformed entry no longer
    aborts the whole file -- it's logged and skipped, matching every other
    `SettingsError`/`ElementNotFoundError` case below."""
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="Lock and Exit",
                        action="auto_restart_time",
                        value="not a time",
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
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
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="Lock and Exit",
                        action="auto_logoff_time",
                        value="11:59 PM",
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
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
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="Lock and Exit",
                        action="auto_logoff_time",
                        value="08:00 AM",
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "set_text_near_label",
        "label": "Set Auto Restart Time (HH:MM)",
        "index": 0,
        "value": "08:00",
    } in calls


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
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="Lock and Exit",
                        action="auto_logoff_time",
                        value="not a time",
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert "hh:mm AM or hh:mm PM" in (tmp_path / "test.log").read_text()


async def test_apply_settings_multiple_entries_across_different_tree_paths(
    sock_path, event_sock_path
):
    """Two entries, `read_only_api` (API/Settings) and `auto_restart_time`
    (Lock and Exit), applied in one `settings_file`/one dialog session:
    `expand_tree` is called for each entry's own `tree_path`, in order, not
    just once up front."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="API/Settings",
                        action="toggle",
                        label_ref="read_only_api",
                        value=False,
                    ),
                    _choice(
                        tree_path="Lock and Exit",
                        action="auto_restart_time",
                        value="08:00 AM",
                    ),
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
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
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="API/Settings",
                        action="toggle",
                        label_ref="no_such_control",
                        value=True,
                    )
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert "no_such_control" in (tmp_path / "test.log").read_text()


async def test_apply_settings_entry_failure_does_not_block_later_entries(
    sock_path, event_sock_path, tmp_path
):
    """Per-entry isolation: an entry naming a missing control must not abort
    every entry after it. Here the first entry is exactly that shape; the
    second entry's action must still run."""
    configure_logging(log_dir=tmp_path, filename="test.log", sink="file")
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            _config()
            settings_file = _choices(
                settings=[
                    _choice(
                        tree_path="API/Precautions",
                        action="toggle",
                        label_ref="no_such_control",
                        value=True,
                    ),
                    _choice(
                        tree_path="API/Precautions",
                        action="toggle",
                        label="Bypass Bond warning for API Orders.",
                        value=True,
                    ),
                ]
            )
            await apply_settings(dispatcher, LABELS, settings_file)
        finally:
            await dispatcher.stop()
    stop_logging()
    assert "no_such_control" in (tmp_path / "test.log").read_text()
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
            dispatcher._window_events.emit(
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


async def test_open_settings_dialog_uses_tws_mosaic_menu_path(
    sock_path, event_sock_path
):
    """TWS has no `Configure` menu at all --
    `navigate_menu("Configure/Settings")` (Gateway's own path) fails with
    `not_found` on TWS. `program=Program.TWS` must select
    `labels.settings.tws_menu_path` (`"File/Global Configuration..."`, Mosaic
    layout) instead; `tws_menu_path_classic` is not used."""
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
                    program=Program.TWS,
                    timeout=5.0,
                )
            )
            await asyncio.sleep(0.02)
            dispatcher._window_events.emit(
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
        "path": "File/Global Configuration...",
    } in calls
    assert {"cmd": "navigate_menu", "path": "Edit/Global Configuration..."} not in calls
    assert {"cmd": "navigate_menu", "path": "Configure/Settings"} not in calls
    assert window_id == "w9"


async def test_open_settings_dialog_retries_missing_menu_item_before_failing(
    sock_path, event_sock_path
):
    """A menu item missing because TWS hasn't finished populating its
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
        if request.get("cmd") == "navigate_menu":
            return {"ok": True, "clicked": True}
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
            dispatcher._window_events.emit(
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
    """When `navigate_menu` ultimately raises (the TWS Classic path
    exhausted here), the dialog-open `wait_for_event` future armed earlier
    must be cancelled and drained, not left running to its own `timeout` --
    that leak produces a "Task exception was never retrieved"
    warning. Asserts no task from this call is still
    pending immediately after it raises."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
        if request.get("cmd") == "navigate_menu":
            return {"ok": False, "error": "not_found", "detail": "boom"}
        if request.get("cmd") == "navigate_menu":
            return {"ok": True, "clicked": True}
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
                    program=Program.TWS,
                    timeout=0.6,
                )
        finally:
            await dispatcher.stop()

    assert asyncio.all_tasks() - before == set()


async def test_open_settings_dialog_waits_for_splash_screen_to_close_first(
    sock_path, event_sock_path
):
    """Regression test: calling
    navigate_menu("Configure/Settings") right after login returned successfully
    but never opened the dialog -- IBC's own SessionManager.awaitReady() blocks
    on the splash frame ("Starting application...") closing first. Confirms
    open_settings_dialog doesn't touch the menu until that happens, when the
    splash is still open at the time it's called (not just the already-closed
    case the other test above covers)."""
    calls: list[dict] = []

    def responder(request):
        calls.append(request)
        if request.get("cmd") == "navigate_menu":
            return {"ok": True, "clicked": True}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(
            event_sock_path,
            [
                {
                    "type": "snapshot",
                    "windows": [
                        {
                            "class": "some.splash.class",
                            "title": "Starting application...",
                            "window_id": "w1",
                        }
                    ],
                }
            ],
        ),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            while "w1" not in dispatcher.windows:  # snapshot seeded
                await asyncio.sleep(0.01)
            task = asyncio.ensure_future(
                open_settings_dialog(dispatcher, LABELS.settings, timeout=5.0)
            )
            await asyncio.sleep(0.02)
            assert {
                "cmd": "navigate_menu",
                "path": "Configure/Settings",
            } not in calls, "must not touch the menu before the splash closes"

            dispatcher._window_events.emit(
                _window_event(
                    1,
                    "window_closed",
                    "some.splash.class",
                    "Starting application...",
                    "w1",
                )
            )
            await asyncio.sleep(0.02)
            dispatcher._window_events.emit(
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
    """Regression test: "Apply" commits changes but never closes the dialog,
    so "OK" must be clicked. "OK" is the commit-and-close button in IBC's own
    source, matching every one of IBC's `ConfigureXXXTask`s."""
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


# --- apply_declarative_settings ------------------------------------------------


def _tracking_responder(calls: list[dict]):
    def responder(request):
        calls.append(request)
        if request.get("cmd") == "dump":
            return {"ok": True, "components": []}
        if request.get("cmd") == "navigate_menu":
            return {"ok": True, "clicked": True}
        return {"ok": True}

    return responder


async def test_apply_declarative_settings_applies_a_choice(sock_path, event_sock_path):
    """A choice alone, with no file of its own, opens the Configuration dialog,
    writes the value and clicks "OK"."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)
    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = Dispatcher(
            AgentCommandConnection(sock_path), AgentEventConnection(event_sock_path)
        )
        await dispatcher.start()
        session = types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: False)
        config = _config()
        try:
            task = asyncio.ensure_future(
                apply_declarative_settings(
                    dispatcher,
                    LABELS,
                    config,
                    session,  # type: ignore[arg-type]
                    _ONE_CHOICE,
                )
            )
            await asyncio.sleep(0.02)
            dispatcher._window_events.emit(
                WindowEvent(
                    seq=1,
                    kind="window_opened",
                    window=WindowInfo(
                        class_="feature.configure.ai",
                        title="DU123 Trader Workstation Configuration "
                        "(Simulated Trading)",
                        window_id="w9",
                    ),
                )
            )
            await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert {
        "cmd": "set_checkbox",
        "target": "Read-Only API",
        "checked": False,
        "window_id": "w9",
    } in calls


async def test_apply_declarative_settings_closes_dialog_even_when_apply_raises(
    sock_path, event_sock_path, monkeypatch
):
    """A `finally` around
    `apply_settings` now guarantees `close_settings_dialog` runs even
    when applying entries fails outright -- before this fix, the Configuration
    dialog was left open on the live Gateway for good. Forces an exception past
    `apply_settings`'s own per-entry isolation (which only catches
    `SettingsError`/`ElementNotFoundError`) to exercise the guaranteed-close
    path specifically, not the per-entry-skip path already covered in
    `test_settings.py`."""
    calls: list[dict] = []
    responder = _tracking_responder(calls)

    async def _raise(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(settings, "apply_settings", _raise)

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = Dispatcher(
            AgentCommandConnection(sock_path), AgentEventConnection(event_sock_path)
        )
        await dispatcher.start()
        session = types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: False)
        config = _config()
        try:
            task = asyncio.ensure_future(
                apply_declarative_settings(
                    dispatcher,
                    LABELS,
                    config,
                    session,  # type: ignore[arg-type]
                    _ONE_CHOICE,
                )
            )
            await asyncio.sleep(0.02)
            dispatcher._window_events.emit(
                WindowEvent(
                    seq=1,
                    kind="window_opened",
                    window=WindowInfo(
                        class_="feature.configure.ai",
                        title="DU123 Trader Workstation Configuration "
                        "(Simulated Trading)",
                        window_id="w9",
                    ),
                )
            )
            with pytest.raises(RuntimeError, match="boom"):
                await asyncio.wait_for(task, timeout=5.0)
        finally:
            await dispatcher.stop()

    assert {"cmd": "click", "target": "OK", "window_id": "w9"} in calls


async def test_apply_declarative_settings_passes_second_factor_timeout(
    monkeypatch,
):
    """`open_settings_dialog`'s own `timeout` default (180.0)
    must not be relied on silently -- it needs to track
    `Config.mfa_timeout` (the same field
    `login.py` already reads), so a deployment that changes one also
    changes the other.

    Also covers `main_window_id` (`LoginManager.main_window_id`) must
    reach `open_settings_dialog` unchanged, so `navigate_menu` scopes to the
    already-validated main window instead of a global search."""
    captured: dict[str, object] = {}

    async def _fake_open_settings_dialog(*_args, **kwargs):
        captured.update(kwargs)
        return "w9"

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "open_settings_dialog", _fake_open_settings_dialog)
    monkeypatch.setattr(settings, "apply_settings", _noop)
    monkeypatch.setattr(settings, "close_settings_dialog", _noop)

    session = types.SimpleNamespace(main_window_id="w5", is_logged_in=lambda: False)
    config = _config(mfa_timeout=42.0)
    await apply_declarative_settings(
        None,  # type: ignore[arg-type]
        LABELS,
        config,
        session,  # type: ignore[arg-type]
        _ONE_CHOICE,
    )

    assert captured["timeout"] == 42.0
    assert captured["main_window_id"] == "w5"


async def test_apply_declarative_settings_runs_after_apply_before_close(monkeypatch):
    """`after_apply` gets the dialog's `window_id` while the dialog is open."""
    order: list[str] = []

    async def _open(*_args, **_kwargs):
        return "w9"

    async def _apply(*_args, **_kwargs):
        order.append("apply")

    async def _close(*_args, **_kwargs):
        order.append("close")

    async def _after(window_id):
        order.append(f"after:{window_id}")

    monkeypatch.setattr(settings, "open_settings_dialog", _open)
    monkeypatch.setattr(settings, "apply_settings", _apply)
    monkeypatch.setattr(settings, "close_settings_dialog", _close)

    session = types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: False)
    await apply_declarative_settings(
        None,  # type: ignore[arg-type]
        LABELS,
        _config(),
        session,  # type: ignore[arg-type]
        [],
        after_apply=_after,
    )

    assert order == ["apply", "after:w9", "close"]


def _patch_dialog_steps(monkeypatch) -> list[str]:
    """Replaces the open/apply/close steps with recorders; returns the call log."""
    order: list[str] = []

    async def _open(*_args, **_kwargs):
        order.append("open")
        return "w9"

    async def _apply(*_args, **_kwargs):
        order.append("apply")

    async def _close(*_args, **_kwargs):
        order.append("close")

    monkeypatch.setattr(settings, "open_settings_dialog", _open)
    monkeypatch.setattr(settings, "apply_settings", _apply)
    monkeypatch.setattr(settings, "close_settings_dialog", _close)
    return order


async def test_apply_declarative_settings_skips_dialog_when_no_choices(
    monkeypatch, caplog
):
    """With no choices the dialog is never opened."""
    order = _patch_dialog_steps(monkeypatch)
    session = types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: False)

    with caplog.at_level(logging.INFO, logger="ibcontroller.settings"):
        await apply_declarative_settings(
            None,  # type: ignore[arg-type]
            LABELS,
            _config(),
            session,  # type: ignore[arg-type]
            [],
        )

    assert order == []
    assert "no settings to apply" in caplog.text


async def test_apply_declarative_settings_opens_dialog_for_after_apply_hook(
    monkeypatch,
):
    """The `after_apply` hook needs the dialog open even with nothing to set."""
    order = _patch_dialog_steps(monkeypatch)
    session = types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: False)

    async def _after(_window_id):
        order.append("after")

    await apply_declarative_settings(
        None,  # type: ignore[arg-type]
        LABELS,
        _config(),
        session,  # type: ignore[arg-type]
        [],
        after_apply=_after,
    )

    assert order == ["open", "apply", "after", "close"]


def _two_toggle_entries() -> list[SettingChoice]:
    return _choices(
        settings=[
            _choice(tree_path="API/Settings", action="toggle", label="A", value=True),
            _choice(tree_path="API/Settings", action="toggle", label="B", value=True),
        ]
    )


async def test_apply_settings_agent_error_on_one_entry_skips_only_that_entry(
    monkeypatch,
):
    """An exception inside the agent for one widget costs that entry only."""
    toggled: list[str] = []

    async def _toggle(_dispatcher, label, _value, **_kwargs):
        if label == "A":
            raise AgentError("boom")
        toggled.append(label)

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "toggle", _toggle)
    monkeypatch.setattr(settings, "expand_tree", _noop)

    await apply_settings(
        None,  # type: ignore[arg-type]
        LABELS,
        _two_toggle_entries(),
    )

    assert toggled == ["B"]


async def test_apply_settings_window_gone_ends_the_loop(monkeypatch):
    """A vanished dialog stops the remaining entries and propagates."""
    toggled: list[str] = []

    async def _toggle(_dispatcher, label, _value, **_kwargs):
        toggled.append(label)
        raise WindowGoneError("gone")

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings, "toggle", _toggle)
    monkeypatch.setattr(settings, "expand_tree", _noop)

    with pytest.raises(WindowGoneError):
        await apply_settings(
            None,  # type: ignore[arg-type]
            LABELS,
            _two_toggle_entries(),
        )

    assert toggled == ["A"]


@pytest.mark.parametrize("error", [WindowGoneError("gone"), ProtocolError("closed")])
async def test_apply_declarative_settings_skips_ok_when_dialog_or_agent_gone(
    monkeypatch, error
):
    """The original error reaches the caller; no "OK" click is attempted on a
    window or connection that no longer exists."""
    order = _patch_dialog_steps(monkeypatch)

    async def _apply(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(settings, "apply_settings", _apply)
    session = types.SimpleNamespace(main_window_id=None, is_logged_in=lambda: False)

    with pytest.raises(type(error)):
        await apply_declarative_settings(
            None,  # type: ignore[arg-type]
            LABELS,
            _config(),
            session,  # type: ignore[arg-type]
            _ONE_CHOICE,
        )

    assert order == ["open"]


# --- entries, catalog and values ----------------------------------------------


def _toggle(name="read_only_api", **kw) -> dict:
    return {
        "name": name,
        "tree_path": "API/Settings",
        "action": "toggle",
        "label": "Read-Only API",
        **kw,
    }


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"action": "press"}, "unknown action 'press'"),
        ({"label": None}, "exactly one of 'label'/'label_ref'"),
        ({"label_ref": "read_only_api"}, "exactly one of 'label'/'label_ref'"),
        (
            {"action": "auto_restart_time"},
            "takes no 'label' or 'label_ref'",
        ),
    ],
)
def test_setting_entry_rejects_a_malformed_shape(fields, match):
    """A bad entry fails at construction, so it is a startup error and not a
    skipped entry at apply time."""
    with pytest.raises(SettingsError, match=match):
        SettingEntry(**{**_toggle(), **fields})


def test_time_entry_needs_no_label():
    entry = SettingEntry(
        name="auto_restart_time", tree_path="Lock and Exit", action="auto_restart_time"
    )
    assert entry.label is None


def test_parse_custom_settings_builds_entries():
    entries = parse_custom_settings(
        [_toggle(), _toggle("lots", label="Lots", field_index=1)],
        source="ibcontroller.toml",
    )
    assert [e.name for e in entries] == ["read_only_api", "lots"]
    assert entries[1].field_index == 1


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ([{**_toggle(), "colour": "red"}], "unknown key 'colour'"),
        ([{**_toggle(), "label": 3}], "expected a string"),
        ([_toggle(), _toggle()], "duplicate setting name 'read_only_api'"),
        ([_toggle(action="press")], "unknown action"),
    ],
)
def test_parse_custom_settings_errors_name_the_source(raw, match):
    with pytest.raises(SettingsError, match=match) as info:
        parse_custom_settings(raw, source="ibcontroller.toml")
    assert "ibcontroller.toml" in str(info.value)


def test_bundled_catalog_lists_the_settings_in_apply_order():
    """`auto_logoff_time` is written before `auto_restart_time`: both write the
    same radio pair and the last one applied wins, as IBC specifies."""
    names = [e.name for e in load_setting_catalog().setting]
    assert names.index("auto_logoff_time") < names.index("auto_restart_time")
    assert "read_only_api" in names


def test_bundled_catalog_label_refs_exist_in_labels():
    for entry in load_setting_catalog().setting:
        if entry.label_ref is not None:
            assert entry.label_ref in LABELS.settings.controls, entry.name


def _catalog() -> SettingCatalog:
    return load_setting_catalog()


def _resolve(ibkr, sources=None, custom=()):
    return resolve_settings(_catalog(), custom, ibkr, sources or {})


def test_resolve_settings_leaves_out_entries_without_a_value():
    assert _resolve({}) == []
    assert _resolve({"blind_trading": True}) == [], "dialog names are not ours"


def test_resolve_settings_keeps_catalog_order_then_custom():
    custom = parse_custom_settings([_toggle("zzz", label="Z")], source="x")
    choices = _resolve(
        {
            "zzz": True,
            "read_only_api": True,
            "auto_restart_time": "08:00 AM",
            "auto_logoff_time": "09:00 PM",
        },
        custom=custom,
    )
    assert [c.entry.name for c in choices] == [
        "auto_logoff_time",
        "auto_restart_time",
        "read_only_api",
        "zzz",
    ]


def test_resolve_settings_records_the_source():
    choices = _resolve({"read_only_api": False}, {"read_only_api": "env"})
    (choice,) = choices
    assert (choice.value, choice.source) == (False, "env")
    assert _resolve({"read_only_api": False})[0].source == "file"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("YES", True), ("False", False), (" no ", False)],
)
def test_resolve_settings_env_toggle_words_are_booleans(raw, expected):
    (choice,) = _resolve({"read_only_api": raw}, {"read_only_api": "env"})
    assert choice.value is expected


@pytest.mark.parametrize(
    ("raw", "source"),
    [("true", "file"), ("maybe", "env"), (1, "file"), (None, "file")],
)
def test_resolve_settings_toggle_rejects_a_non_boolean(raw, source):
    """A string from the file is not a boolean (only the environment spells it)."""
    with pytest.raises(SettingsError, match="read_only_api") as info:
        _resolve({"read_only_api": raw}, {"read_only_api": source})
    assert source in str(info.value)


def test_resolve_settings_time_is_checked_at_startup():
    (choice,) = _resolve({"auto_restart_time": "11:45 PM"})
    assert choice.value == "11:45 PM"
    with pytest.raises(SettingsError, match="auto_restart_time") as info:
        _resolve({"auto_restart_time": "23:45"})
    assert "hh:mm AM or hh:mm PM" in str(info.value)
    with pytest.raises(SettingsError, match="expected a time string"):
        _resolve({"auto_logoff_time": 2345})


def _text_entry():
    return parse_custom_settings(
        [
            {
                "name": "port",
                "tree_path": "API/Settings",
                "action": "type_text",
                "label": "Socket port",
            }
        ],
        source="x",
    )


@pytest.mark.parametrize(("raw", "expected"), [("4002", "4002"), (4002, "4002")])
def test_resolve_settings_text_takes_a_string_or_a_number(raw, expected):
    (choice,) = _resolve({"port": raw}, custom=_text_entry())
    assert choice.value == expected


@pytest.mark.parametrize("raw", [True, 1.5, ["4002"]])
def test_resolve_settings_text_rejects_other_types(raw):
    with pytest.raises(SettingsError, match="port"):
        _resolve({"port": raw}, custom=_text_entry())


def test_resolve_settings_rejects_a_custom_name_of_a_catalog_entry():
    custom = parse_custom_settings([_toggle()], source="x")
    with pytest.raises(SettingsError, match="read_only_api"):
        _resolve({}, custom=custom)
