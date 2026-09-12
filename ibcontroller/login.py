"""The Login domain's own state machine. `LoginState` has six states:
`LOGGED_OUT`, `AWAITING_CREDENTIALS`, `LOGGING_IN`, `TWO_FA_IN_PROGRESS`,
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
itself. Its own `wait_for_event` calls are narrowed to only the two outcomes
it actually cares about (2FA, the main window); anything else is left
entirely to the separate, always-on `recognisers.watch_for_unprompted_windows`
background task, so a real dialog occurrence is never handled twice at once.

**Two distinct timeout/retry mechanisms, not one:**

1. A 2FA-timeout watchdog, internal to this module (`_after_2fa_closed_*`):
   measures elapsed time since login started against
   `second_factor_authentication_timeout` (IB's own budget for completing
   2FA). If 2FA closed within that budget and `relogin_after_2fa_timeout` is
   enabled, arms a second, shorter watchdog
   (`second_factor_authentication_exit_interval`) and raises `LoginError` if
   login still hasn't completed by then -- there's no restart primitive at
   this layer yet. If 2FA's own timeout expired while its dialog was still
   open, retries the whole login after a fixed `_IBC_RELOGIN_DELAY_SECONDS`.
2. `recognisers.TooManyFailedLoginAttemptsRecognizer`, a real server-side
   rate-limit dialog, unrelated to the 2FA timing above. Its `handle()`
   calls `LoginManager.schedule_retry`, which re-fills credentials, resubmits,
   and waits for the outcome independently -- it doesn't assume the original
   `run()` call is still alive to notice the result.

**Gateway and TWS need different `LOGGED_IN` signals.** Gateway's main
window already exists, with an enabled menu, before login completes -- so
"main window opened" can't be Gateway's completion signal the way it is for
TWS. `_wait_for_outcome` dispatches on `config.program`:
`_wait_for_outcome_tws` waits for a newly opened window to carry a
`File > Lock Application` menu item; `_wait_for_outcome_gateway` waits for
the splash/"Authenticating..." window's own `WINDOW_CLOSED` event instead,
the one signal that's correct in both trading modes without needing to know
which one it is.

**Not live-validated against real IBKR servers, unlike everything else in
this file.** Triggering IB's real 180-second 2FA timeout, or "too many
failed login attempts", both carry real account-level risk for no safe
benefit -- built and unit-tested against synthetic timing/dialogs instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from enum import Enum, auto
from pathlib import Path

from anyio import Path as AsyncPath

from ibcontroller.actions import (
    click,
    menu_item_exists,
    type_text,
    wait_for_event,
)
from ibcontroller.agent_client import WindowEvent
from ibcontroller.config import Config, TradingMode
from ibcontroller.dispatch import Dispatcher
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
    TWO_FA_IN_PROGRESS = auto()
    LOGGED_IN = auto()
    LOGIN_FAILED = auto()


class LoginError(Exception):
    """The login frame itself was never recognised within the timeout, or some
    other condition this module can't recover from on its own."""


async def find_autorestart_hash(settings_dir: str | Path) -> str | None:
    """Locates Gateway/TWS's own `autorestart` marker file under
    `settings_dir` and returns the account-hash subdirectory name it lives
    in (`<settings_dir>/<hash>/autorestart`) -- the value passed on the JVM
    command line as `-Drestart=<hash>` on relaunch. Returns `None` if zero
    or more than one marker file is found: with more than one, there's no
    way to tell which is the right one, so both are left in place (not
    deleted) and the caller falls back to a full credential fill.

    The recursive scan runs off the event loop (`anyio.Path`, one thread
    hop per call) -- called repeatedly in a tight retry loop
    (`control_loop._is_restart_with_grace`) concurrent with the Dispatcher's
    own event/command tasks, which a blocking `pathlib` scan would stall."""
    found = [p async for p in AsyncPath(settings_dir).rglob("autorestart")]
    if len(found) != 1:
        return None
    return found[0].parent.name


async def is_restart(settings_dir: str | Path) -> bool:
    """Returns whether exactly one `autorestart` marker file exists under
    `settings_dir` (via `find_autorestart_hash`)."""
    return await find_autorestart_hash(settings_dir) is not None


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
        settings_dir: str | Path,
        restart_expected: bool | None = None,
    ) -> None:
        """`config`/`labels`/`dispatcher`: this instance's configuration,
        labels, and `Dispatcher`. `settings_dir`: the Gateway/TWS settings
        directory, checked for the autorestart marker. `restart_expected`:
        `None` (default) checks `is_restart(settings_dir)` directly at
        `run()` time; `True`/`False` overrides that check -- for callers
        (`control_loop.py`) that already know the answer from having
        computed their own restart hash moments earlier, avoiding a race
        against Gateway's own consumption of the marker file."""
        self._config = config
        self._labels = labels
        self._dispatcher = dispatcher
        self._settings_dir = settings_dir
        self._restart_expected = restart_expected
        self.state = LoginState.LOGGED_OUT
        self._login_frame_class: str | None = None
        self._login_frame_window_id: str | None = None
        self._login_start_time: float | None = None
        self._retry_task: asyncio.Task[None] | None = None

    def is_logged_in(self) -> bool:
        """Returns whether `state` is `LOGGED_IN`. Supplied to
        `ExistingSessionRecognizer` (`recognisers.py`) as its
        `is_logged_in` callable -- that recogniser reacts to login state, it
        doesn't own it."""
        return self.state is LoginState.LOGGED_IN

    def _gateway_or_tws_titles(self) -> list[str]:
        return (
            self._labels.login.gateway_titles
            if self._config.program.lower() == "gateway"
            else self._labels.login.tws_titles
        )

    async def run(
        self,
        *,
        login_timeout: float | None = None,
        outcome_timeout: float | None = None,
    ) -> None:
        """Drives the whole Login domain sequence, end to end: waits for the
        login frame, skips credential fill if a restart is already in
        progress, otherwise fills and submits credentials, then waits for
        the outcome. Raises `LoginError`/`recognisers.LoginFailedError`/
        `TimeoutError` on failure; returns normally once `state` is
        `LOGGED_IN`. `login_timeout`/`outcome_timeout` default from
        `Config.login_dialog_display_timeout`/
        `second_factor_authentication_timeout` when not given explicitly."""
        login_timeout = (
            login_timeout
            if login_timeout is not None
            else self._config.login_dialog_display_timeout
        )
        outcome_timeout = (
            outcome_timeout
            if outcome_timeout is not None
            else self._config.second_factor_authentication_timeout
        )
        titles = self._gateway_or_tws_titles()
        try:
            login_event = await wait_for_event(
                self._dispatcher,
                "window_opened",
                lambda e: e.window.title in titles,
                timeout=login_timeout,
            )
        except TimeoutError as exc:
            raise LoginError(
                f"login frame never appeared within {login_timeout}s "
                f"(expected one of {titles})"
            ) from exc
        self._login_frame_class = login_event.window.class_
        self._login_frame_window_id = login_event.window.window_id

        if self._restart_expected is not None:
            restart = self._restart_expected
            reason = "restart_expected flag"
        else:
            restart = await is_restart(self._settings_dir)
            reason = "autorestart marker file"
        if restart:
            logger.info("IBController > %s -- skipping credential fill", reason)
            self.state = LoginState.LOGGING_IN
            self._login_start_time = time.monotonic()
            await self._wait_for_outcome(outcome_timeout)
            return

        await self._fill_credentials_and_submit()
        await self._wait_for_outcome(outcome_timeout)

    async def _fill_credentials(self) -> None:
        """Fills the trading-mode/username/password fields on the login
        frame, scoped to `self._login_frame_window_id` (the login frame's
        FIX Login and IB API sections both carry Username/Password pairs
        with identical accessible names, so an unscoped search would be
        ambiguous). On Gateway, first clicks the API-type toggle to select
        IB API -- TWS has no such control, so that click is skipped there."""
        labels = self._labels.login
        window_id = self._login_frame_window_id
        if self._config.program.lower() == "gateway":
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

    async def retry_login(
        self, wait_seconds: float, *, outcome_timeout: float | None = None
    ) -> None:
        """Waits `wait_seconds`, re-submits credentials, then independently
        waits for the outcome -- doesn't assume whichever `run()` call
        originally started this session is still alive to notice the
        result. Redundant but harmless if the original wait is still alive
        too -- both independently converge on the same `LOGGED_IN` state."""
        await asyncio.sleep(wait_seconds)
        await self._fill_credentials_and_submit()
        await self._wait_for_outcome(
            outcome_timeout
            if outcome_timeout is not None
            else self._config.second_factor_authentication_timeout
        )

    def schedule_retry(self, wait_seconds: float) -> None:
        """Synchronous callback given to `TooManyFailedLoginAttemptsRecognizer`
        (`recognisers.py`): fires `retry_login` as a background task after
        `wait_seconds`, so the caller (running inside
        `watch_for_unprompted_windows`'s own loop) never blocks on the
        cooldown itself. Keeps a reference on `self` so the task isn't
        garbage-collected mid-flight."""
        self._retry_task = asyncio.ensure_future(self.retry_login(wait_seconds))

    async def _wait_for_outcome(self, outcome_timeout: float) -> None:
        """Waits for the login outcome, dispatching on `config.program` --
        Gateway and TWS need different completion signals (see the module
        docstring)."""
        if self._config.program.lower() == "gateway":
            await self._wait_for_outcome_gateway(outcome_timeout)
        else:
            await self._wait_for_outcome_tws()

    async def _wait_for_outcome_tws(self, timeout: float | None = None) -> None:
        """Loops over `window_opened` events (other than the login frame's
        own class) until one carries a `File > Lock Application` menu item
        (the main window, checked via `menu_item_exists`) or is the 2FA
        dialog. On 2FA, waits for it to close, then delegates to
        `_after_2fa_closed_tws`.

        `timeout` defaults to unbounded (`None`) -- TWS's real login flow
        never times out waiting for the main window itself.
        `_after_2fa_closed_tws`'s own exit-interval watchdog is the one case
        that passes an explicit `timeout`, tracked as one deadline across
        every loop iteration.

        Deliberately does *not* fall back to the shared recogniser registry
        for anything else it sees (existing-session, login-failed,
        non-brokerage, a stray pop-up, an intermediate window like
        "Downloading settings from server") -- those are left entirely to
        the separate, always-on `recognisers.watch_for_unprompted_windows`
        background task."""
        twofa_title = self._labels.second_factor_auth.title
        deadline = None if timeout is None else time.monotonic() + timeout

        while True:
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            event = await wait_for_event(
                self._dispatcher,
                "window_opened",
                lambda e: e.window.class_ != self._login_frame_class,
                timeout=remaining,
            )
            if event.window.title == twofa_title:
                self.state = LoginState.TWO_FA_IN_PROGRESS
                logger.info("IBController > second factor authentication in progress")
                await wait_for_event(
                    self._dispatcher,
                    "window_closed",
                    lambda e: e.window.title == twofa_title,
                    timeout=self._config.second_factor_authentication_timeout,
                )
                await self._after_2fa_closed_tws()
                return

            if event.window.window_id is None:
                # No window_id to scope menu_item_exists to (real
                # window_opened events always carry one -- confirmed live --
                # but WindowInfo's own type doesn't guarantee it); can't be
                # the main window without one, keep looping.
                continue

            exists, _enabled = await menu_item_exists(
                self._dispatcher,
                self._labels.login.tws_main_window_menu_item,
                window_id=event.window.window_id,
            )
            if exists:
                self.state = LoginState.LOGGED_IN
                logger.info("IBController > login completed")
                return
            # Not the main window (an intermediate dialog, e.g. "Downloading
            # settings from server") -- keep looping for the next candidate.

    async def _after_2fa_closed_tws(self) -> None:
        """Measures elapsed time since login started against
        `second_factor_authentication_timeout` (IB's own real limit for
        completing 2FA, default 180s).

        If 2FA closed within that budget: waits for the outcome again (an
        unconditional wait if `relogin_after_2fa_timeout` is off), or arms a
        shorter `second_factor_authentication_exit_interval` watchdog and
        raises `LoginError` if login still hasn't completed by then -- there
        is no restart primitive at this layer yet.

        If 2FA's own timeout already expired while its dialog was still
        open: retries the whole login after `_IBC_RELOGIN_DELAY_SECONDS`, or
        keeps waiting for the outcome if retry is disabled."""
        elapsed = time.monotonic() - (self._login_start_time or time.monotonic())

        if elapsed < self._config.second_factor_authentication_timeout:
            # 2FA was handled within IB's own budget -- authentication should
            # be under way.
            if not self._config.relogin_after_2fa_timeout:
                await self._wait_for_outcome_tws()
                return
            # A second, shorter watchdog: if login still hasn't completed by
            # now, we have no restart primitive at this layer (Management/L1,
            # not built), so this raises a clear signal instead.
            try:
                await self._wait_for_outcome_tws(
                    self._config.second_factor_authentication_exit_interval
                )
            except TimeoutError as exc:
                raise LoginError(
                    "login did not complete within "
                    f"{self._config.second_factor_authentication_exit_interval}s "
                    "of second factor authentication completing (IBC's own "
                    "SecondFactorAuthenticationExitInterval watchdog)"
                ) from exc
            return

        # 2FA's own IB-side timeout expired while the dialog was still open --
        # the user answered too slowly.
        if not self._config.relogin_after_2fa_timeout:
            logger.info(
                "IBController > re-login after second factor authentication timeout not"
                " required"
            )
            # Our own run() is a bounded coroutine the caller awaits for a
            # definitive outcome (returns normally once state is LOGGED_IN)
            # -- returning here instead would complete run() while state
            # stays stuck at TWO_FA_IN_PROGRESS, so keep waiting for the
            # outcome, same as the "on-time, relogin disabled" branch above.
            await self._wait_for_outcome_tws()
            return
        logger.info(
            "IBController > re-login after second factor authentication timeout in %ss",
            _IBC_RELOGIN_DELAY_SECONDS,
        )
        await self.retry_login(_IBC_RELOGIN_DELAY_SECONDS)

    async def _wait_for_outcome_gateway(self, outcome_timeout: float) -> None:
        """Waits for the splash/"Authenticating..." window's own
        `WINDOW_CLOSED` event (matched by its final title,
        "Starting application...") -- Gateway's real login-completion
        signal, since its main window already exists, with an enabled menu,
        before login finishes, so "main window opened" can't be used here.

        A 2FA dialog opening/closing along the way only updates `state` and
        arms `_after_2fa_closed_gateway`'s watchdog -- unlike the TWS path,
        it is never itself a terminal signal. Runs two concurrent
        `wait_for_event` waits (splash-closed, 2FA-opened), since they're
        different event kinds (`window_closed` vs `window_opened`); both are
        always cancelled in `finally` before this method returns, whichever
        path was actually taken."""
        splash_title = self._labels.login.starting_application_title
        twofa_title = self._labels.second_factor_auth.title

        splash_closed = asyncio.ensure_future(
            wait_for_event(
                self._dispatcher,
                "window_closed",
                lambda e: e.window.title == splash_title,
                timeout=outcome_timeout,
            )
        )
        twofa_opened = asyncio.ensure_future(
            wait_for_event(
                self._dispatcher,
                "window_opened",
                lambda e: e.window.title == twofa_title,
                timeout=outcome_timeout,
            )
        )
        pending: set[asyncio.Task[WindowEvent]] = {splash_closed, twofa_opened}
        try:
            while splash_closed in pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                if twofa_opened not in done:
                    continue
                twofa_exc = twofa_opened.exception()
                if twofa_exc is not None:
                    if not isinstance(twofa_exc, TimeoutError):
                        raise twofa_exc
                    # 2FA simply never appeared within outcome_timeout (the
                    # normal paper case, and any Gateway login that doesn't
                    # need it) -- keep waiting on splash_closed alone.
                    continue
                self.state = LoginState.TWO_FA_IN_PROGRESS
                logger.info("IBController > second factor authentication in progress")
                await wait_for_event(
                    self._dispatcher,
                    "window_closed",
                    lambda e: e.window.title == twofa_title,
                    timeout=outcome_timeout,
                )
                await self._after_2fa_closed_gateway(splash_closed, outcome_timeout)
            await splash_closed
        finally:
            for task in (twofa_opened, splash_closed):
                if not task.done():
                    task.cancel()
            for task in (twofa_opened, splash_closed):
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self.state = LoginState.LOGGED_IN
        logger.info("IBController > login completed")

    async def _after_2fa_closed_gateway(
        self, splash_closed: asyncio.Task[WindowEvent], outcome_timeout: float
    ) -> None:
        """Gateway variant of `_after_2fa_closed_tws` -- never recurses into
        a fresh wait: `_wait_for_outcome_gateway`'s own `splash_closed`
        future is already pending and will resolve on its own once Gateway
        actually finishes authenticating. Only arms the shorter
        exit-interval watchdog (raising `LoginError` on timeout, if
        `relogin_after_2fa_timeout` is enabled) or schedules a retry.
        `asyncio.shield` on the watchdog wait is deliberate: a timeout there
        must not cancel `splash_closed` itself, since the caller's own loop
        still owns and awaits that future regardless of what this method
        does."""
        elapsed = time.monotonic() - (self._login_start_time or time.monotonic())

        if elapsed < self._config.second_factor_authentication_timeout:
            if not self._config.relogin_after_2fa_timeout:
                return
            try:
                await asyncio.wait_for(
                    asyncio.shield(splash_closed),
                    timeout=self._config.second_factor_authentication_exit_interval,
                )
            except TimeoutError as exc:
                raise LoginError(
                    "login did not complete within "
                    f"{self._config.second_factor_authentication_exit_interval}s "
                    "of second factor authentication completing (IBC's own "
                    "SecondFactorAuthenticationExitInterval watchdog)"
                ) from exc
            return

        if not self._config.relogin_after_2fa_timeout:
            logger.info(
                "IBController > re-login after second factor authentication "
                "timeout not required"
            )
            return
        logger.info(
            "IBController > re-login after second factor authentication timeout in %ss",
            _IBC_RELOGIN_DELAY_SECONDS,
        )
        await self.retry_login(
            _IBC_RELOGIN_DELAY_SECONDS, outcome_timeout=outcome_timeout
        )
