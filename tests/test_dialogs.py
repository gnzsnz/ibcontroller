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
    DialogEntry,
    DialogsError,
    MatchGroup,
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
