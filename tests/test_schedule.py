"""Unit tests for schedule.py -- pure math, no asyncio/I/O needed."""

from __future__ import annotations

from datetime import datetime, time
from tempfile import gettempdir

import attrs
import pytest
from typed_settings.types import Secret

from ibcontroller.config import Config, TradingMode
from ibcontroller.schedule import (
    ScheduledAction,
    ScheduleError,
    _next_occurrence,
    _parse_hhmm,
    next_scheduled_shutdown,
    parse_closedown_at,
)


def _config(**overrides) -> Config:
    base = Config(
        program="tws",
        tws_version="10.50",
        trading_mode=TradingMode.PAPER,
        userid=Secret("u"),
        password=Secret("p"),
        log_dir=f"{gettempdir()}/log",
    )
    return attrs.evolve(base, **overrides)


# -- _parse_hhmm --


def test_parse_hhmm_accepts_valid_24_hour_time():
    assert _parse_hhmm("07:05", setting_name="cold_restart_time") == time(7, 5)


def test_parse_hhmm_rejects_am_pm_format():
    with pytest.raises(ScheduleError, match="cold_restart_time"):
        _parse_hhmm("07:05 AM", setting_name="cold_restart_time")


def test_parse_hhmm_rejects_garbage():
    with pytest.raises(ScheduleError):
        _parse_hhmm("not a time", setting_name="closedown_at")


# -- parse_closedown_at --


def test_parse_closedown_at_daily_format():
    weekday, target = parse_closedown_at("22:00")
    assert weekday is None
    assert target == time(22, 0)


def test_parse_closedown_at_weekly_format():
    weekday, target = parse_closedown_at("Friday 22:00")
    assert weekday == 4  # Monday=0 .. Friday=4, matching datetime.weekday()
    assert target == time(22, 0)


def test_parse_closedown_at_weekday_name_case_insensitive():
    weekday, _target = parse_closedown_at("friday 22:00")
    assert weekday == 4


def test_parse_closedown_at_rejects_bad_weekday_name():
    with pytest.raises(ScheduleError, match="closedown_at"):
        parse_closedown_at("Someday 22:00")


def test_parse_closedown_at_rejects_bad_time():
    with pytest.raises(ScheduleError):
        parse_closedown_at("Friday 25:99")


# -- _next_occurrence --


def test_next_occurrence_daily_later_today():
    now = datetime(2026, 9, 12, 6, 0)  # Saturday
    result = _next_occurrence(time(7, 5), None, now)
    assert result == datetime(2026, 9, 12, 7, 5)


def test_next_occurrence_daily_already_passed_wraps_to_tomorrow():
    now = datetime(2026, 9, 12, 8, 0)  # Saturday
    result = _next_occurrence(time(7, 5), None, now)
    assert result == datetime(2026, 9, 13, 7, 5)


def test_next_occurrence_weekly_later_this_week():
    now = datetime(2026, 9, 8, 6, 0)  # Tuesday
    result = _next_occurrence(time(22, 0), 4, now)  # next Friday
    assert result == datetime(2026, 9, 11, 22, 0)


def test_next_occurrence_weekly_wraps_to_next_week_when_passed():
    now = datetime(2026, 9, 12, 23, 0)  # Saturday, after Friday 22:00 already
    result = _next_occurrence(time(22, 0), 4, now)  # next Friday
    assert result == datetime(2026, 9, 18, 22, 0)


def test_next_occurrence_weekly_same_day_not_yet_passed():
    now = datetime(2026, 9, 11, 6, 0)  # Friday, before 22:00
    result = _next_occurrence(time(22, 0), 4, now)
    assert result == datetime(2026, 9, 11, 22, 0)


# -- next_scheduled_shutdown --


def test_next_scheduled_shutdown_none_when_neither_set():
    config = _config()
    assert next_scheduled_shutdown(config, datetime(2026, 9, 12, 6, 0)) is None


def test_next_scheduled_shutdown_gateway_applies_both():
    config = _config(program="gateway", cold_restart_time="07:05", closedown_at="08:00")
    now = datetime(2026, 9, 12, 6, 0)  # Saturday
    result = next_scheduled_shutdown(config, now)
    assert result is not None
    assert result.action is ScheduledAction.TIDY_CLOSEDOWN
    assert result.at == _next_occurrence(time(8, 0), None, now)


def test_next_scheduled_shutdown_picks_earliest():
    config = _config(cold_restart_time="07:05", closedown_at="08:00")
    now = datetime(2026, 9, 12, 6, 0)  # Saturday
    result = next_scheduled_shutdown(config, now)
    assert result is not None
    assert result.action is ScheduledAction.TIDY_CLOSEDOWN
    assert result.at == _next_occurrence(time(8, 0), None, now)


def test_next_scheduled_shutdown_ties_break_toward_cold_restart():
    # cold_restart_time only fires on Sunday; pick a "now" where both the
    # weekly cold-restart slot and a daily closedown_at slot land on the same
    # instant.
    now = datetime(2026, 9, 12, 6, 0)  # Saturday
    config = _config(cold_restart_time="07:05", closedown_at="07:05")
    cold_at = _next_occurrence(time(7, 5), 6, now)  # next Sunday
    closedown_config = _config(closedown_at=cold_at.strftime("%A %H:%M"))
    config = attrs.evolve(config, closedown_at=closedown_config.closedown_at)
    result = next_scheduled_shutdown(config, now)
    assert result is not None
    assert result.at == cold_at
    assert result.action is ScheduledAction.COLD_RESTART


def test_next_scheduled_shutdown_invalid_field_is_skipped_not_fatal(caplog):
    config = _config(cold_restart_time="garbage", closedown_at="22:00")
    with caplog.at_level("ERROR"):
        result = next_scheduled_shutdown(config, datetime(2026, 9, 12, 6, 0))
    assert result is not None
    assert result.action is ScheduledAction.TIDY_CLOSEDOWN
    assert "invalid cold_restart_time" in caplog.text
