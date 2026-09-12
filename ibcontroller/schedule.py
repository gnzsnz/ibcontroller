"""schedule.py -- pure wall-clock scheduling math for TWS-only self-scheduled
shutdown actions (`Config.cold_restart_time`/`closedown_at`), consumed by
`control_loop.py`. No I/O, no asyncio -- same split discipline as
`launcher.build_launch_plan` vs `launch_instance`: testable against a
synthetic `datetime`, no live Gateway/TWS needed.

Both settings come from IBC's own `config.ini` (`ColdRestartTime`/
`ClosedownAt`) but, unlike `auto_restart_time`/`auto_logoff_time`, are not GUI
settings at all -- IBC implements them itself as a self-scheduled tidy
close-down (`IbcTws.java`'s own `startShutdownTimerIfRequired`/
`getColdRestartTime`/`getShutdownTime`), which this module ports the
scheduling math for. TWS only: confirmed by inspection that neither setting
(nor `AutoLogoffTime`/`AutoRestartTime`, which *are* shared) appears anywhere
in `IbcGateway.java` at all (a 38-line near-empty class) -- `IbcTws.java` is
IBC's one real entry point for both programs, so this really is a TWS-only
carve-out, not a naming artifact. `control_loop.py` owns actually acting on
the result: ending the READY wait so the existing `clean_shutdown` call
performs the real menu-based close, and -- for a cold restart -- forcing a
fresh relaunch with no restart hash."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum, auto

from ibcontroller.config import Config

logger = logging.getLogger(__name__)

_WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
# Index matches datetime.weekday() (Monday=0..Sunday=6) directly.
_WEEKDAY_INDEX = {name: i for i, name in enumerate(_WEEKDAY_NAMES)}
_COLD_RESTART_WEEKDAY = _WEEKDAY_INDEX["Sunday"]


class ScheduleError(Exception):
    """A malformed `cold_restart_time`/`closedown_at` value."""


class ScheduledAction(Enum):
    COLD_RESTART = auto()
    TIDY_CLOSEDOWN = auto()


@dataclass(frozen=True)
class ScheduledShutdown:
    at: datetime
    action: ScheduledAction


def _parse_hhmm(value: str, *, setting_name: str) -> time:
    """Strict 24-hour `"HH:MM"` -- mirrors IBC's own
    `SimpleDateFormat("HH:mm")` strictness (unlike `auto_restart_time`/
    `auto_logoff_time`'s 12-hour `"hh:mm AM/PM"`, these two settings are
    always 24-hour in IBC's own `config.ini`)."""
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M")
    except ValueError:
        raise ScheduleError(
            f'{setting_name} setting must be 24-hour "HH:MM", got {value!r}'
        ) from None
    return parsed.time()


def parse_closedown_at(value: str) -> tuple[int | None, time]:
    """Parses `"HH:MM"` (every day) or `"<Weekday> HH:MM"` (one day a week --
    `Weekday` a full English name, `Monday`..`Sunday`, matched case-
    insensitively). Deliberately NOT locale-dependent (`%A`/`strptime`) --
    IBC's own `config.ini` comment flags exactly this class of bug for
    non-Latin-1 locales (Unicode-escaping a day name); a fixed English table
    sidesteps it entirely rather than porting the same fragility. Returns
    `(weekday index matching datetime.weekday(), time)`, weekday `None` for
    "every day"."""
    parts = value.strip().rsplit(" ", 1)
    if len(parts) == 2:  # noqa: PLR2004
        day_name = parts[0].strip().title()
        if day_name not in _WEEKDAY_INDEX:
            raise ScheduleError(
                'closedown_at setting must be "HH:MM" or "<Weekday> HH:MM" '
                f"(Weekday one of {', '.join(_WEEKDAY_NAMES)}), got {value!r}"
            )
        return _WEEKDAY_INDEX[day_name], _parse_hhmm(
            parts[1], setting_name="closedown_at"
        )
    return None, _parse_hhmm(value, setting_name="closedown_at")


def _next_occurrence(target: time, weekday: int | None, now: datetime) -> datetime:
    """Next wall-clock instant strictly after `now` matching `target`/
    `weekday` (`weekday=None` -- every day). Wraps to the next matching day
    if today's slot has already passed (or is exactly `now`)."""
    candidate = now.replace(
        hour=target.hour, minute=target.minute, second=0, microsecond=0
    )
    if weekday is None:
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    candidate += timedelta(days=(weekday - now.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def next_scheduled_shutdown(config: Config, now: datetime) -> ScheduledShutdown | None:
    """Earliest of `Config.cold_restart_time` (every Sunday) and
    `Config.closedown_at` (daily or weekly) -- both TWS-only. Ignored, with a
    logged warning, when `config.program != "tws"`. A malformed value is
    logged and that one field is skipped (matching `settings.py`'s own
    per-entry error isolation) rather than aborting the other field or
    raising out of this function. Returns `None` if nothing applies."""
    if config.program.lower() != "tws":
        if config.cold_restart_time or config.closedown_at:
            logger.warning(
                "IBController > cold_restart_time/closedown_at are TWS-only "
                "and ignored for program=%s",
                config.program,
            )
        return None

    candidates: list[ScheduledShutdown] = []
    if config.cold_restart_time:
        try:
            target = _parse_hhmm(
                config.cold_restart_time, setting_name="cold_restart_time"
            )
        except ScheduleError:
            logger.exception("IBController > invalid cold_restart_time -- ignoring")
        else:
            candidates.append(
                ScheduledShutdown(
                    at=_next_occurrence(target, _COLD_RESTART_WEEKDAY, now),
                    action=ScheduledAction.COLD_RESTART,
                )
            )
    if config.closedown_at:
        try:
            weekday, target = parse_closedown_at(config.closedown_at)
        except ScheduleError:
            logger.exception("IBController > invalid closedown_at -- ignoring")
        else:
            candidates.append(
                ScheduledShutdown(
                    at=_next_occurrence(target, weekday, now),
                    action=ScheduledAction.TIDY_CLOSEDOWN,
                )
            )
    if not candidates:
        return None
    # Ties break toward cold restart, matching IBC's own
    # `coldRestartTime.before(shutdownTime)` check in
    # startShutdownTimerIfRequired.
    candidates.sort(key=lambda c: (c.at, c.action is not ScheduledAction.COLD_RESTART))
    return candidates[0]
