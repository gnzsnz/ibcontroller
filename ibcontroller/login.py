"""The Login domain's own state machine. `LoginState` has six states:
`LOGGED_OUT`, `AWAITING_CREDENTIALS`, `LOGGING_IN`, `MFA_IN_PROGRESS`,
`LOGGED_IN`, `LOGIN_FAILED`.

**Distinguishing the login frame from the main window, given both can share
the same title.** This module records the login frame's own window *class*
the moment it's first recognised, and treats a later window sharing the same
title but a *different* class as the main window -- a relative comparison,
not a hardcoded class string, since title alone can't tell the two apart.

**Restart check, before anything else.** If Gateway/TWS's own `autorestart`
marker file is present in its settings directory, this module skips straight
to waiting for the outcome instead of filling credentials, matching an
automatic relogin already in progress.

**Existing-session/login-failed/non-brokerage dialogs are `recognisers.py`'s
job, not this module's** -- `LoginManager` never dispatches to that registry
itself. Its own event waits are narrowed to only the two outcomes
it actually cares about (MFA, the main window); anything else is left
entirely to the separate, always-on `recognisers.watch_for_unprompted_windows`
background task, so a real dialog occurrence is never handled twice at once.

**Two distinct timeout/retry mechanisms, not one:**

1. The 2FA close handling, internal to this module (`_after_mfa_closed`).
   The wait for the 2FA dialog to close has no cap (IB closes an unanswered
   dialog by itself, after 200 s to 352 s observed on Gateway 10.51); elapsed time since
   login started is then compared with `mfa_timeout` (IB's own limit). Closed
   before it, with `relogin_after_mfa_timeout` enabled: arms the shorter
   `mfa_exit_interval` watchdog and raises `MfaTimeoutError` if login still
   hasn't completed. Closed at or after it (IB timed out on an unanswered push):
   resubmits the login after a fixed `_IBC_RELOGIN_DELAY_SECONDS`.
2. `recognisers.TooManyFailedLoginAttemptsRecognizer`, a real server-side
   rate-limit dialog, unrelated to the MFA timing above. Its `handle()`
   calls `LoginManager.schedule_retry`, which re-fills credentials and
   resubmits after the cooldown.

Both retries only resubmit: `run()` keeps its single wait for the outcome, and
there is no overall login deadline (IBC has none). A stuck login is detected
from outside. `run()` cancels a pending scheduled retry when it ends.

**Gateway and TWS need different `LOGGED_IN` signals.** Gateway's main
window already exists, with an enabled menu, before login completes -- so
"main window opened" can't be Gateway's completion signal the way it is for
TWS. `_wait_for_outcome` dispatches on `config.program`:
`_wait_for_outcome_tws` waits for a newly opened window to carry a
`File > Lock Application` menu item; `_wait_for_outcome_gateway` waits for
the splash/"Authenticating..." window's own `WINDOW_CLOSED` event instead,
the one signal that's correct in both trading modes without needing to know
which one it is.

**Not live-validated against real IBKR servers:** "too many failed login
attempts" carries real account-level risk for no safe benefit -- built and
unit-tested against synthetic timing/dialogs instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum, auto
from typing import ClassVar, Protocol

from ibcontroller.actions import (
    click,
    menu_item_exists,
    type_text,
    wait_for_window,
)
from ibcontroller.agent_client import WindowGoneError
from ibcontroller.config import Config, Program, TradingMode
from ibcontroller.dispatch import Dispatcher
from ibcontroller.errors import IBControllerError, TransientError
from ibcontroller.labels import Labels

logger = logging.getLogger(__name__)

# IBC's own hardcoded constant (LoginManager.java's
# secondFactorAuthenticationDialogClosed), not a config value -- kept literal
# here for the same reason IBC never exposed it as one.
_IBC_RELOGIN_DELAY_SECONDS = 5.0


class LoginState(Enum):
    LOGGED_OUT = auto()
    AWAITING_CREDENTIALS = auto()
    LOGGING_IN = auto()
    MFA_IN_PROGRESS = auto()
    LOGGED_IN = auto()
    LOGIN_FAILED = auto()


class LoginError(TransientError):
    """The login frame itself was never recognised within the timeout, or some
    other condition this module can't recover from on its own."""


class CredentialsRejectedError(IBControllerError):
    """A real credential/account rejection (wrong password, wrong username for
    the trading mode, a known non-brokerage reason). Exits 77: no retry, because
    of account-lockout risk (gitea #60/#62). Not raised for a bug in a
    recogniser, which propagates as whatever exception it actually is."""

    exit_code: ClassVar[int] = 77  # EX_NOPERM


class TransientLoginError(TransientError):
    """A login-blocking dialog that is a server-side condition, not a credential
    rejection (a "Login failed"/"Login Error" dialog, or a Gateway "Connection to
    server failed" reason not in `known_reasons` -- e.g. a stale restart token).
    `run_control_loop` catches it and relaunches unbounded with a full fresh
    login, the same shape as a scheduled `ShutdownCause.COLD_RESTART`, matching
    IBC's unconditional cold-restart for these dialogs (gitea #62). Never reaches
    `cli.py` under normal operation."""


class LoginFrameTimeoutError(LoginError):
    """The login frame never appeared within `login_dialog_display_timeout`
    -- matches IBC's own error code 1112, always relaunched unconditionally
    by `control_loop.run_control_loop` (not gated by any config field)."""


class MfaTimeoutError(LoginError):
    """Login still hadn't completed `mfa_exit_interval` seconds after 2FA
    closed (IBC's own `SecondFactorAuthenticationExitInterval` watchdog).
    `Config.mfa_timeout_action` decides what `control_loop.run_control_loop`
    does with it: `exit` propagates as-is, `restart` relaunches instead."""

    exit_code: ClassVar[int] = 75  # EX_TEMPFAIL


class SessionState(Protocol):
    """Read-only view of the login session for the other L5 domains.
    `LoginManager` owns the state and satisfies this structurally."""

    main_window_id: str | None

    def is_logged_in(self) -> bool: ...


class LoginManager:
    """One instance per running agent -- this module's own state, not shared
    across instances, matching every other per-instance object in this
    project (`Dispatcher`, the recogniser registry)."""

    def __init__(
        self,
        config: Config,
        labels: Labels,
        dispatcher: Dispatcher,
        *,
        restart_expected: bool,
    ) -> None:
        """`config`/`labels`/`dispatcher`: this instance's configuration,
        labels, and `Dispatcher`. `restart_expected`: whether this launch is
        a scheduled restart (`control_loop` passes `restart_hash is not None`),
        in which case Gateway/TWS relogs in by itself and credentials are not
        filled. Decided by the caller, never re-checked on disk here: Gateway
        can consume the marker before a later check would see it."""
        self._config = config
        self._labels = labels
        self._dispatcher = dispatcher
        self._restart_expected = restart_expected
        self.state = LoginState.LOGGED_OUT
        self._login_frame_class: str | None = None
        self._login_frame_window_id: str | None = None
        self._login_start_time: float | None = None
        self._retry_task: asyncio.Task[None] | None = None
        self.main_window_id: str | None = None

    def is_logged_in(self) -> bool:
        """Returns whether `state` is `LOGGED_IN`. Supplied to
        `ExistingSessionRecognizer` (`recognisers.py`) as its
        `is_logged_in` callable -- that recogniser reacts to login state, it
        doesn't own it."""
        return self.state is LoginState.LOGGED_IN

    def _gateway_or_tws_titles(self) -> list[str]:
        return (
            self._labels.login.gateway_titles
            if self._config.program == Program.GATEWAY
            else self._labels.login.tws_titles
        )

    async def run(self, *, login_timeout: float | None = None) -> None:
        """Drives the whole Login domain sequence, end to end: waits for the
        login frame, skips credential fill if a restart is already in
        progress, otherwise fills and submits credentials, then waits for
        the outcome, without an overall bound. Raises `LoginError`/
        `CredentialsRejectedError`/`MfaTimeoutError` on failure; returns
        normally once `state` is `LOGGED_IN`. `login_timeout` defaults from
        `Config.login_dialog_display_timeout`. A pending scheduled retry is
        cancelled when this ends."""
        login_timeout = (
            login_timeout
            if login_timeout is not None
            else self._config.login_dialog_display_timeout
        )
        titles = self._gateway_or_tws_titles()
        try:
            login_frame = await wait_for_window(
                self._dispatcher,
                lambda w: w.title in titles,
                timeout=login_timeout,
            )
        except TimeoutError as exc:
            raise LoginFrameTimeoutError(
                f"login frame never appeared within {login_timeout}s "
                f"(expected one of {titles})"
            ) from exc
        assert login_frame is not None  # closed=False always returns the window
        self._login_frame_class = login_frame.class_
        self._login_frame_window_id = login_frame.window_id

        try:
            if self._restart_expected:
                logger.info(
                    "IBController > scheduled restart -- skipping credential fill"
                )
                self.state = LoginState.LOGGING_IN
                self._login_start_time = time.monotonic()
            else:
                await self._fill_credentials_and_submit()
            await self._wait_for_outcome()
        finally:
            if self._retry_task is not None:
                self._retry_task.cancel()

    async def _fill_credentials(self) -> None:
        """Fills the trading-mode/username/password fields on the login
        frame, scoped to `self._login_frame_window_id` (the login frame's
        FIX Login and IB API sections both carry Username/Password pairs
        with identical accessible names, so an unscoped search would be
        ambiguous). On Gateway, first clicks the API-type toggle to select
        IB API -- TWS has no such control, so that click is skipped there."""
        labels = self._labels.login
        window_id = self._login_frame_window_id
        if self._config.program == Program.GATEWAY:
            await click(
                self._dispatcher,
                labels.api_type_ib_api,
                window_id=window_id,
            )
        trading_mode_label = (
            labels.trading_mode_live
            if self._config.trading_mode is TradingMode.LIVE
            else labels.trading_mode_paper
        )
        await click(self._dispatcher, trading_mode_label, window_id=window_id)
        await type_text(
            self._dispatcher,
            labels.username_field,
            self._config.userid,
            window_id=window_id,
        )
        await type_text(
            self._dispatcher,
            labels.password_field,
            self._config.password,
            window_id=window_id,
        )

    async def _fill_credentials_and_submit(self) -> None:
        """Fills credentials, then clicks the login button. Factored out of
        `run()` so a retry can redo just this part on an already-open login
        frame, without re-waiting for a fresh login-frame `window_opened`
        event that will never re-fire for a frame that's already open."""
        self.state = LoginState.AWAITING_CREDENTIALS
        await self._fill_credentials()
        self.state = LoginState.LOGGING_IN
        self._login_start_time = time.monotonic()
        await click(
            self._dispatcher,
            self._labels.login.login_buttons,
            window_id=self._login_frame_window_id,
        )
        logger.info("IBController > credentials submitted, awaiting outcome")

    async def retry_login(self, wait_seconds: float) -> None:
        """Waits `wait_seconds`, then re-submits credentials. Does not wait for
        the outcome: the wait already running in `run()` keeps watching."""
        await asyncio.sleep(wait_seconds)
        await self._fill_credentials_and_submit()

    def schedule_retry(self, wait_seconds: float) -> None:
        """Synchronous callback given to `TooManyFailedLoginAttemptsRecognizer`
        (`recognisers.py`): fires `retry_login` as a background task after
        `wait_seconds`, so the caller (running inside
        `watch_for_unprompted_windows`'s own loop) never blocks on the
        cooldown itself. `run()` owns the task and cancels it when it ends."""
        self._retry_task = asyncio.ensure_future(self.retry_login(wait_seconds))

    async def _wait_for_outcome(self) -> None:
        """Waits for the login outcome, dispatching on `config.program` --
        Gateway and TWS need different completion signals (see the module
        docstring). Unbounded: only the `mfa_exit_interval` watchdog raises."""
        if self._config.program == Program.GATEWAY:
            await self._wait_for_outcome_gateway()
        else:
            await self._wait_for_outcome_tws()

    def _exit_interval_error(self) -> MfaTimeoutError:
        return MfaTimeoutError(
            f"login did not complete within {self._config.mfa_exit_interval}s "
            "of second factor authentication completing (IBC's own "
            "SecondFactorAuthenticationExitInterval watchdog)"
        )

    async def _wait_for_outcome_tws(self) -> None:
        """Loops over `window_opened` events (other than the login frame's
        own class) until one carries a `File > Lock Application` menu item
        (the main window, checked via `menu_item_exists`). The MFA dialog
        only updates `state` and, once it closes, may arm the
        `mfa_exit_interval` watchdog (see `_after_mfa_closed`) -- it is never
        itself a terminal signal. The close alone triggers that handling: on
        TWS the login frame turns into the 2FA frame in place, so a
        `window_opened` for it may never arrive (observed live on 10.51),
        while its `window_closed` (with a null `window_id`) does.

        Deliberately does *not* fall back to the shared recogniser registry
        for anything else it sees (existing-session, login-failed,
        non-brokerage, a stray pop-up, an intermediate window like
        "Downloading settings from server") -- those are left entirely to
        the separate, always-on `recognisers.watch_for_unprompted_windows`
        background task."""
        mfa_title = self._labels.mfa.title
        watchdog: float | None = None  # monotonic end of the post-MFA exit interval
        seen: set[str] = set()

        # One subscription spans the loop, so a window opening during the
        # menu_item_exists round trip is buffered. Windows already open when
        # the wait starts come from the registry first.
        with self._dispatcher.subscribe() as sub:
            backlog = [
                w
                for w in self._dispatcher.windows.values()
                if w.class_ != self._login_frame_class
            ]
            while True:
                if backlog:
                    window = backlog.pop(0)
                else:
                    try:
                        async with asyncio.timeout(
                            None
                            if watchdog is None
                            else max(0.0, watchdog - time.monotonic())
                        ):
                            event = await anext(sub)
                    except TimeoutError as exc:
                        raise self._exit_interval_error() from exc
                    if (
                        event.kind == "window_closed"
                        and event.window.title == mfa_title
                    ):
                        self.state = LoginState.MFA_IN_PROGRESS
                        interval = await self._after_mfa_closed()
                        watchdog = (
                            None if interval is None else time.monotonic() + interval
                        )
                        continue
                    if (
                        event.kind != "window_opened"
                        or event.window.class_ == self._login_frame_class
                    ):
                        continue
                    window = event.window
                if window.window_id is not None:
                    if window.window_id in seen:
                        continue
                    seen.add(window.window_id)

                if window.title == mfa_title:
                    self.state = LoginState.MFA_IN_PROGRESS
                    watchdog = None
                    logger.info(
                        "IBController > second factor authentication in progress"
                    )
                    continue

                if window.window_id is None:
                    # No window_id to scope menu_item_exists to (real
                    # window_opened events always carry one -- confirmed live
                    # -- but WindowInfo's own type doesn't guarantee it); can't
                    # be the main window without one, keep looping.
                    continue

                try:
                    exists, _enabled = await menu_item_exists(
                        self._dispatcher,
                        self._labels.login.tws_main_window_menu_item,
                        window_id=window.window_id,
                    )
                except WindowGoneError:
                    continue  # short-lived intermediate window closed already
                if exists:
                    self.state = LoginState.LOGGED_IN
                    # Keep the window_id this same check just validated (#40)
                    # -- Gateway's path below never sets this (no ambiguity
                    # there, see _wait_for_outcome_gateway's docstring), so
                    # consumers must treat None as "no scoping available, fall
                    # back".
                    self.main_window_id = window.window_id
                    logger.info("IBController > login completed")
                    return
                # Not the main window (an intermediate dialog, e.g.
                # "Downloading settings from server") -- keep looping for the
                # next candidate.

    async def _wait_for_outcome_gateway(self) -> None:
        """Waits for the splash/"Authenticating..." window's own
        `WINDOW_CLOSED` event (matched by its final title,
        "Starting application...") -- Gateway's real login-completion
        signal, since its main window already exists, with an enabled menu,
        before login finishes, so "main window opened" can't be used here.

        An MFA dialog opening/closing along the way only updates `state` and
        may arm `_after_mfa_closed`'s exit-interval watchdog -- unlike
        the TWS path, it is never itself a terminal signal. One subscription
        and one loop cover both events."""
        splash_title = self._labels.login.starting_application_title
        mfa_title = self._labels.mfa.title
        watchdog: float | None = None  # monotonic end of the post-MFA exit interval

        with self._dispatcher.subscribe() as sub:
            while True:
                try:
                    async with asyncio.timeout(
                        None if watchdog is None else watchdog - time.monotonic()
                    ):
                        event = await anext(sub)
                except TimeoutError as exc:
                    raise self._exit_interval_error() from exc
                if event.kind == "window_closed" and event.window.title == splash_title:
                    break
                if event.kind == "window_opened" and event.window.title == mfa_title:
                    self.state = LoginState.MFA_IN_PROGRESS
                    watchdog = None
                    logger.info(
                        "IBController > second factor authentication in progress"
                    )
                    # No cap: IB closes the dialog itself, and when decides the branch
                    await sub.wait(
                        "window_closed",
                        lambda e: e.window.title == mfa_title,
                        timeout=None,
                    )
                    interval = await self._after_mfa_closed()
                    if interval is not None:
                        watchdog = time.monotonic() + interval

        self.state = LoginState.LOGGED_IN
        logger.info("IBController > login completed")

    async def _after_mfa_closed(self) -> float | None:
        """Measures elapsed time since login started against `mfa_timeout`
        (IB's own limit for an unanswered 2FA push, default 180s). Returns the
        `mfa_exit_interval` (seconds) the caller must enforce as a watchdog
        when `relogin_after_mfa_timeout` is enabled and 2FA closed within
        `mfa_timeout`, `None` when the caller just keeps waiting. If 2FA
        closed at or after `mfa_timeout` (IB timed out an unanswered push)
        and relogin is enabled, resubmits the login after
        `_IBC_RELOGIN_DELAY_SECONDS`; the caller's wait then continues."""
        elapsed = time.monotonic() - (self._login_start_time or time.monotonic())

        if elapsed < self._config.mfa_timeout:
            if not self._config.relogin_after_mfa_timeout:
                return None
            return self._config.mfa_exit_interval

        if not self._config.relogin_after_mfa_timeout:
            logger.info(
                "IBController > re-login after second factor authentication "
                "timeout not required"
            )
            return None
        logger.info(
            "IBController > re-login after second factor authentication timeout in %ss",
            _IBC_RELOGIN_DELAY_SECONDS,
        )
        await self.retry_login(_IBC_RELOGIN_DELAY_SECONDS)
        return None
