"""Unit tests for dialogs.py: entry validation and the step executor -- same
fake-socket-server pattern as test_actions.py."""

from __future__ import annotations

import pytest

from ibcontroller.agent_client import (
    AgentCommandConnection,
    AgentEventConnection,
    ElementNotFoundError,
)
from ibcontroller.config import ConfigError
from ibcontroller.dialogs import (
    ClickStep,
    DialogChoice,
    DialogEntry,
    DialogsError,
    MatchGroup,
    load_catalog,
    parse_catalog,
    parse_custom_dialogs,
    resolve_dialogs,
    run_steps,
)
from ibcontroller.dispatch import Dispatcher
from tests.fakes import FakeCommandServer, FakeEventServer

pytestmark = pytest.mark.asyncio


async def _start(cmd_sock, event_sock) -> Dispatcher:
    dispatcher = Dispatcher(
        AgentCommandConnection(cmd_sock), AgentEventConnection(event_sock)
    )
    await dispatcher.start()
    return dispatcher


def _entry(**overrides) -> DialogEntry:
    base = dict(
        name="blind_trading",
        default="off",
        match=[MatchGroup(title="Order Preview", text="blind trading")],
        modes={"off": [], "on": [ClickStep(["Override and Transmit", "Yes"])]},
    )
    base.update(overrides)
    return DialogEntry(**base)


def test_dialogs_error_is_a_config_error():
    assert issubclass(DialogsError, ConfigError)


def test_valid_entry_is_accepted():
    entry = _entry()
    assert entry.modes["off"] == []
    assert entry.modes["on"] == [ClickStep(["Override and Transmit", "Yes"])]


def test_match_group_needs_at_least_one_field():
    with pytest.raises(DialogsError, match=r"title.*text"):
        MatchGroup()


def test_match_group_text_also_alone_is_not_enough():
    with pytest.raises(DialogsError):
        MatchGroup(text_also="Are you sure")


def test_entry_needs_at_least_one_match_group():
    with pytest.raises(DialogsError, match=r"blind_trading.*match"):
        _entry(match=[])


def test_entry_default_must_be_a_known_mode():
    with pytest.raises(DialogsError, match=r"blind_trading.*'maybe'.*off.*on"):
        _entry(default="maybe")


def test_entry_needs_at_least_one_mode():
    with pytest.raises(DialogsError, match=r"blind_trading.*modes"):
        _entry(modes={})


@pytest.mark.parametrize("click", [[], ""])
def test_click_step_rejects_an_empty_label_or_list(click):
    with pytest.raises(DialogsError, match="click"):
        ClickStep(click)


async def test_run_steps_clicks_in_order_scoped_to_the_window(
    sock_path, event_sock_path
):
    seen = []

    def responder(request):
        seen.append((request["cmd"], request["target"], request.get("window_id")))
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await run_steps(
                dispatcher, [ClickStep("First"), ClickStep("Second")], window_id="w3"
            )
        finally:
            await dispatcher.stop()

    assert seen == [("click", "First", "w3"), ("click", "Second", "w3")]


async def test_run_steps_falls_back_to_the_next_candidate(sock_path, event_sock_path):
    attempts = []

    def responder(request):
        attempts.append(request["target"])
        if request["target"] == "Override and Transmit":
            return {"ok": False, "error": "not_found", "detail": "no such button"}
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await run_steps(
                dispatcher,
                [ClickStep(["Override and Transmit", "Yes"])],
                window_id=None,
            )
        finally:
            await dispatcher.stop()

    assert attempts == ["Override and Transmit", "Yes"]


async def test_run_steps_stops_at_the_first_failing_step(sock_path, event_sock_path):
    attempts = []

    def responder(request):
        attempts.append(request["target"])
        return {"ok": False, "error": "not_found", "detail": "gone"}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(ElementNotFoundError):
                await run_steps(
                    dispatcher,
                    [ClickStep("First"), ClickStep("Second")],
                    window_id=None,
                )
        finally:
            await dispatcher.stop()

    assert attempts == ["First"]  # the second step never ran


async def test_run_steps_with_no_steps_sends_nothing(sock_path, event_sock_path):
    attempts = []

    def responder(request):
        attempts.append(request)
        return {"ok": True}

    async with (
        FakeCommandServer(sock_path, responder),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            await run_steps(dispatcher, [], window_id="w1")
        finally:
            await dispatcher.stop()

    assert attempts == []


async def test_run_steps_rejects_an_unknown_step_type(sock_path, event_sock_path):
    async with (
        FakeCommandServer(sock_path, lambda _req: {"ok": True}),
        FakeEventServer(event_sock_path, []),
    ):
        dispatcher = await _start(sock_path, event_sock_path)
        try:
            with pytest.raises(TypeError, match="step"):
                await run_steps(dispatcher, ["click"], window_id=None)  # type: ignore[list-item]
        finally:
            await dispatcher.stop()


_VALID = """
[[dialog]]
name = "warn"
default = "on"
match = [ { title = "Warning", text = "some phrase" }, { text = "other" } ]
[dialog.modes]
off = []
on = [ { click = ["OK", "Yes"] }, { click = "Close" } ]
"""


def test_parse_catalog_builds_entries():
    catalog = parse_catalog(_VALID, source="test.toml")
    (entry,) = catalog.dialog
    assert entry.name == "warn"
    assert entry.default == "on"
    assert entry.match == [
        MatchGroup(title="Warning", text="some phrase"),
        MatchGroup(text="other"),
    ]
    assert entry.modes == {
        "off": [],
        "on": [ClickStep(["OK", "Yes"]), ClickStep("Close")],
    }


def test_parse_catalog_rejects_invalid_toml():
    with pytest.raises(DialogsError, match=r"test.toml.*invalid TOML"):
        parse_catalog("[[dialog", source="test.toml")


@pytest.mark.parametrize(
    "toml",
    [
        _VALID + 'extra = "x"\n',  # unknown key on the entry
        _VALID.replace('{ text = "other" }', '{ text = "other", regex = "x" }'),
        _VALID.replace('{ click = "Close" }', '{ click = "Close", wait = 1 }'),
        _VALID.replace('default = "on"', "default = 5"),
        _VALID.replace('{ click = "Close" }', "{ click = 5 }"),
        _VALID.replace('{ click = "Close" }', "{ click = [1] }"),
    ],
    ids=[
        "entry-key",
        "group-key",
        "step-key",
        "default-type",
        "click-int",
        "click-list",
    ],
)
def test_parse_catalog_rejects_unknown_keys_and_wrong_types(toml):
    with pytest.raises(DialogsError, match=r"test\.toml"):
        parse_catalog(toml, source="test.toml")


def test_parse_catalog_names_the_unknown_key():
    toml = _VALID.replace('{ text = "other" }', '{ text = "other", regex = "x" }')
    with pytest.raises(DialogsError, match=r"unknown key.*regex.*match\[1\]"):
        parse_catalog(toml, source="test.toml")


def test_parse_catalog_reports_the_entry_that_fails_validation():
    toml = _VALID.replace('default = "on"', 'default = "maybe"')
    with pytest.raises(DialogsError, match=r"dialog 'warn'.*'maybe'"):
        parse_catalog(toml, source="test.toml")


def test_parse_catalog_rejects_duplicate_names():
    with pytest.raises(DialogsError, match=r"duplicate.*'warn'"):
        parse_catalog(_VALID + _VALID, source="test.toml")


def test_parse_catalog_accepts_an_empty_file():
    assert parse_catalog("", source="test.toml").dialog == []


def test_bundled_catalog_loads_with_the_expected_entries():
    catalog = load_catalog()
    entries = {e.name: e for e in catalog.dialog}
    assert list(entries) == [
        "non_brokerage_account",
        "auto_restart_confirmation",
        "password_expiry_warning",
        "blind_trading",
    ]
    assert {n: e.default for n, e in entries.items()} == {
        "non_brokerage_account": "on",
        "auto_restart_confirmation": "on",
        "password_expiry_warning": "off",  # nosec B105
        "blind_trading": "off",
    }


def test_bundled_always_on_entries_keep_the_old_rules():
    entries = {e.name: e for e in load_catalog().dialog}
    brokerage = entries["non_brokerage_account"]
    assert brokerage.match == [MatchGroup(text="This is not a brokerage account")]
    assert brokerage.modes["on"] == [ClickStep(["I understand and accept"])]
    restart = entries["auto_restart_confirmation"]
    assert restart.match == [MatchGroup(text="trading platform restart automatically")]
    assert restart.modes["on"] == [ClickStep(["OK"])]


def test_bundled_blind_trading_has_both_ibc_variants_and_candidate_buttons():
    entries = {e.name: e for e in load_catalog().dialog}
    blind = entries["blind_trading"]
    assert blind.match == [
        MatchGroup(title="Order Preview", text="blind trading"),
        MatchGroup(
            text="blind trading",
            text_also="Are you sure you want to submit this order?",
        ),
    ]
    assert blind.modes["on"] == [ClickStep(["Override and Transmit", "Yes"])]
    assert blind.modes["off"] == []


def test_every_bundled_entry_has_an_off_and_an_on_mode():
    for entry in load_catalog().dialog:
        assert {"off", "on"} <= set(entry.modes), entry.name


# --- custom entries and mode resolution ---------------------------------------

_RAW_CUSTOM = {
    "name": "my_warning",
    "default": "on",
    "match": [{"title": "Warning", "text": "some phrase"}],
    "modes": {"off": [], "on": [{"click": ["OK", "Yes"]}]},
}


def _resolve(ibkr=None, sources=None, custom=()):
    ibkr = ibkr or {}
    sources = sources or dict.fromkeys(ibkr, "file")
    return resolve_dialogs(load_catalog(), custom, ibkr, sources)


def _by_name(choices):
    return {c.entry.name: c for c in choices}


def test_parse_custom_dialogs_builds_entries():
    (entry,) = parse_custom_dialogs([_RAW_CUSTOM], source="ibcontroller.toml")
    assert entry.name == "my_warning"
    assert entry.modes["on"] == [ClickStep(["OK", "Yes"])]


def test_parse_custom_dialogs_empty_is_empty():
    assert parse_custom_dialogs([], source="ibcontroller.toml") == []


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d.update(bogus=1), "unknown key"),
        (lambda d: d.update(default="nope"), "default"),
        (lambda d: d.update(match=[]), "match group"),
        (lambda d: d.update(modes={}), "modes"),
        (lambda d: d.update(name=3), "string"),
    ],
)
def test_parse_custom_dialogs_errors_name_the_source(mutate, match):
    raw = {**_RAW_CUSTOM}
    mutate(raw)
    with pytest.raises(DialogsError, match=rf"ibcontroller\.toml.*{match}"):
        parse_custom_dialogs([raw], source="ibcontroller.toml")


def test_parse_custom_dialogs_duplicate_names_rejected():
    with pytest.raises(DialogsError, match="duplicate"):
        parse_custom_dialogs([_RAW_CUSTOM, _RAW_CUSTOM], source="ibcontroller.toml")


def test_resolve_defaults_when_nothing_given():
    choices = _by_name(_resolve())
    blind = choices["blind_trading"]
    assert (blind.mode, blind.source) == ("off", "default")
    assert choices["non_brokerage_account"].mode == "on"


def test_resolve_keeps_catalog_order_after_custom_entries():
    (custom,) = parse_custom_dialogs([_RAW_CUSTOM], source="x")
    names = [c.entry.name for c in _resolve(custom=[custom])]
    assert names[0] == "my_warning"
    assert names[1:] == [e.name for e in load_catalog().dialog]


@pytest.mark.parametrize(
    "value, mode", [(True, "on"), (False, "off"), ("on", "on"), ("off", "off")]
)
def test_resolve_file_value(value, mode):
    choice = _by_name(_resolve({"blind_trading": value}))["blind_trading"]
    assert (choice.mode, choice.source) == (mode, "file")


@pytest.mark.parametrize(
    "value, mode",
    [
        ("true", "on"),
        ("YES", "on"),
        ("false", "off"),
        ("No", "off"),
        ("ON", "on"),
        ("Off", "off"),
    ],
)
def test_resolve_env_value(value, mode):
    choices = _resolve({"blind_trading": value}, {"blind_trading": "env"})
    choice = _by_name(choices)["blind_trading"]
    assert (choice.mode, choice.source) == (mode, "env")


def test_resolve_file_string_true_is_a_mode_name_not_a_boolean():
    with pytest.raises(DialogsError, match=r"blind_trading.*'true'"):
        _resolve({"blind_trading": "true"})


def test_resolve_unknown_mode_lists_the_known_ones():
    with pytest.raises(DialogsError, match=r"blind_trading.*'maybe'.*off, on"):
        _resolve({"blind_trading": "maybe"})


def test_resolve_wrong_type_is_an_error():
    with pytest.raises(DialogsError, match=r"blind_trading.*int"):
        _resolve({"blind_trading": 3})


def test_resolve_boolean_needs_on_and_off_modes():
    raw = {
        **_RAW_CUSTOM,
        "modes": {"manual": [], "accept": [{"click": "OK"}]},
        "default": "manual",
    }
    (custom,) = parse_custom_dialogs([raw], source="x")
    with pytest.raises(DialogsError, match=r"my_warning.*boolean.*on.*off"):
        _resolve({"my_warning": True}, custom=[custom])


def test_resolve_custom_entry_with_catalog_name_is_rejected():
    raw = {**_RAW_CUSTOM, "name": "blind_trading"}
    (custom,) = parse_custom_dialogs([raw], source="x")
    with pytest.raises(DialogsError, match=r"blind_trading.*catalog"):
        _resolve(custom=[custom])


def test_resolve_ignores_keys_that_are_not_dialogs():
    choices = _resolve({"read_only_api": True, "auto_restart_time": "23:45"})
    assert all(c.source == "default" for c in choices)


def test_dialog_choice_fields():
    entry = load_catalog().dialog[0]
    choice = DialogChoice(entry, "on", "file")
    assert (choice.entry, choice.mode, choice.source) == (entry, "on", "file")
