"""Matches windows that show up unprompted -- a stray pop-up, an
existing-session conflict, a login-failure dialog, a warning dialog -- as
opposed to windows an active sequence (Login, Settings) already expects and
waits for directly via `actions.wait_for_window`.

`RecognizerRegistry` holds an ordered list of `Recognizer`s, checked in
order against each unprompted window; the first one whose `recognises()`
returns `True` has its `handle()` awaited. `DeclarativeDismissRecognizer` is
built from `labels.DismissRule` entries (`build_default_registry`) for
the common "match text/title, click one button" case; the remaining classes
below handle cases with real conditional logic that isn't expressible as
data. Every recogniser acts through `actions.py`'s primitives, the same as
any other caller.

`watch_for_unprompted_windows` runs for the agent's whole lifetime as a
background task, watching `dispatcher._window_events` for `window_opened`
events and routing each one through `handle_window_opened`.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable, Sequence
from enum import Enum, auto
from typing import Protocol

import eventkit

from ibcontroller.actions import click, dump
from ibcontroller.agent_client import (
    AgentClientError,
    Component,
    ElementNotFoundError,
    WindowEvent,
    WindowGoneError,
)
from ibcontroller.config import (
    AcceptIncomingConnections,
    Config,
    ExistingSessionAction,
)
from ibcontroller.dispatch import Dispatcher
from ibcontroller.labels import (
    AcceptIncomingConnectionLabels,
    DismissRule,
    ExistingSessionLabels,
    GatewayConnectionFailedLabels,
    Labels,
    LoginErrorLabels,
    LoginFailedLabels,
    NotCurrentlyAvailableLabels,
    ReloginLabels,
    ShutdownLabels,
    TooManyFailedLoginAttemptsLabels,
    TradingLoginHandoffLabels,
    UnrecognizedCredentialsLabels,
)
from ibcontroller.login import (
    CredentialsRejectedError,
    InvalidSessionStateError,
    SessionCededError,
    SessionState,
    TransientLoginError,
)

logger = logging.getLogger(__name__)

_WAIT_PATTERN = re.compile(
    r"Please wait (?:(\d+) minutes? )?(?:& )?(?:(\d+) seconds?)?"
)

_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")


def _parse_wait_seconds(message: str) -> float:
    """Parses a wait duration out of a "too many failed login attempts"
    message (e.g. "Please wait 4 minutes & 47 seconds"), plus a 3 second
    buffer. Returns just the buffer if `message` doesn't match."""
    match = _WAIT_PATTERN.search(message)
    minutes = int(match.group(1)) if match and match.group(1) else 0
    seconds = int(match.group(2)) if match and match.group(2) else 0
    return float(minutes * 60 + seconds + 3)


def _strip_html(text: str) -> str:
    """Strips HTML tags from an IB `JTextPane` message (always HTML, e.g.
    `<html>...<br><br>...</html>`), collapsing the whitespace left behind."""
    return re.sub(r"\s+", " ", _HTML_TAG_PATTERN.sub(" ", text)).strip()


async def _dismiss(dispatcher: Dispatcher, label: str, window_id: str | None) -> None:
    """Clicks `label`, best-effort: a stale label or an already closed window
    is logged, so the caller's classification still gets raised."""
    try:
        await click(dispatcher, label, window_id=window_id)
    except (ElementNotFoundError, WindowGoneError) as exc:
        logger.warning("IBController > dismiss of %r failed: %s", label, exc)


class Recognizer(Protocol):
    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        """Returns whether this recogniser matches `event`. `components` is
        the result of dumping the window (never `None` -- if the window
        closed before the dump could complete, this recogniser is never
        called)."""
        ...

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        """Acts on a matched window -- dismissing it, clicking a button, or
        otherwise responding to it."""
        ...


def is_credential_entry(components: list[Component]) -> bool:
    """Returns whether `components` includes a live, visible credential
    field. Checked before any recogniser runs -- credential fields are never
    handled by this registry, only by Login's own field-fill."""
    return any(c.credential_field and c.showing for c in components)


class DeclarativeDismissRecognizer:
    """One `Recognizer` per `labels.DismissRule`: matches `rule.match_title`
    against the window's title and/or `rule.match_text` against any
    component's `accessible_name`/`text`, then clicks `rule.click`. If both
    `match_title` and `match_text` are given, both must match. `rule` is
    public so a caller inspecting `RecognizerRegistry.handled` can identify
    which rule fired, by name."""

    def __init__(self, rule: DismissRule) -> None:
        self.rule = rule

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        rule = self.rule
        if rule.match_title is not None and rule.match_title not in (
            event.window.title or ""
        ):
            return False
        if rule.match_text is not None and not any(
            self._text_matches(rule.match_text, value)
            for c in components
            for value in (c.accessible_name, c.text)
            if value is not None
        ):
            return False
        return True

    @staticmethod
    def _text_matches(match_text: str, value: str) -> bool:
        """Raw match first; then HTML-stripped, since IB message panes are HTML
        and a phrase copied from the screen can span a tag or hold an entity
        (`&quot;`)."""
        return match_text in value or match_text in html.unescape(_strip_html(value))

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        """Clicks `rule.click`, scoped to `event.window.window_id`."""
        await click(dispatcher, self.rule.click, window_id=event.window.window_id)
        logger.info("dismissed %s (declarative rule)", self.rule.name)


class TooManyFailedLoginAttemptsRecognizer:
    """Matches the server-side "too many failed login attempts" dialog by
    message text (`text` primarily, `accessible_name` as a fallback).
    Distinct from the 2FA-timeout retry `login.py`'s `LoginManager` handles
    internally (that one is driven by elapsed time, not by recognising a
    window).

    When `relogin_enabled` is `True`, dismisses the dialog and calls
    `schedule_retry` with the wait time (in seconds) parsed from the
    message. When `False`, this state won't resolve itself -- the dialog is
    left on screen (IBC parity) and `CredentialsRejectedError` is raised (exit 77,
    not a retry). `schedule_retry` is a plain, synchronous
    callable -- it's expected to schedule the actual retry as a background
    task, since `handle()` returns immediately without waiting out the
    cooldown."""

    def __init__(
        self,
        labels: TooManyFailedLoginAttemptsLabels,
        relogin_enabled: bool,
        schedule_retry: Callable[[float], None],
    ) -> None:
        self._labels = labels
        self._relogin_enabled = relogin_enabled
        self._schedule_retry = schedule_retry

    def _matched_text(self, components: list[Component]) -> str | None:
        prefix = self._labels.message_prefix
        for c in components:
            if c.text is not None and prefix in c.text:
                return c.text
            if c.accessible_name is not None and prefix in c.accessible_name:
                return c.accessible_name
        return None

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        return self._matched_text(components) is not None

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        if not self._relogin_enabled:
            logger.warning(
                "too many failed login attempts -- relogin disabled, stopping"
            )
            raise CredentialsRejectedError(
                "too many failed login attempts, relogin disabled"
            )
        message = self._matched_text(components) or ""
        wait_seconds = _parse_wait_seconds(message)
        logger.warning(
            "too many failed login attempts -- retrying in %.0fs", wait_seconds
        )
        await _dismiss(dispatcher, self._labels.dismiss_button, event.window.window_id)
        self._schedule_retry(wait_seconds)


class LoginFailedRecognizer:
    """Matches a "Login failed" dialog by window title (ported from IBC's
    `LoginFailedDialogHandler`). IB reuses this title for a "server
    disconnected" condition, not rejected credentials (per IBC's own commit
    history). Dismisses it and raises `TransientLoginError`,
    matching IBC's own unconditional cold-restart for this dialog."""

    def __init__(self, labels: LoginFailedLabels) -> None:
        self._labels = labels

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        return self._labels.title in (event.window.title or "")

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        await _dismiss(dispatcher, self._labels.dismiss_button, event.window.window_id)
        logger.warning("IBController > login failed -- transient, relaunching")
        raise TransientLoginError("login failed; server disconnected")


class LoginErrorRecognizer:
    """Matches a "Login Error" dialog by window title (ported from IBC's
    `LoginErrorDialogHandler`, e.g. "Login failed - Server disconnected,
    please try again") -- always a transient server condition, never a
    credential rejection. Logs every text component (HTML stripped), as IBC does,
    dismisses it, and raises `TransientLoginError`."""

    def __init__(self, labels: LoginErrorLabels) -> None:
        self._labels = labels

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        return self._labels.title in (event.window.title or "")

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        texts = []
        for c in components:
            raw = c.text if c.text is not None else c.accessible_name
            if raw is not None:
                texts.append(_strip_html(raw))
        logger.warning("IBController > login error: %s", " | ".join(texts))
        await _dismiss(dispatcher, self._labels.dismiss_button, event.window.window_id)
        raise TransientLoginError("login error dialog; server disconnected")


class UnrecognizedCredentialsRecognizer:
    """Matches the "Unrecognized Username or Password" dialog by window
    title -- a real rejected login, distinct from `LoginFailedRecognizer`'s
    dialog despite the similar name. No IBC equivalent (IBC never handles
    this one, leaves it on screen). Dismisses it and raises
    `CredentialsRejectedError`."""

    def __init__(self, labels: UnrecognizedCredentialsLabels) -> None:
        self._labels = labels

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        return self._labels.title in (event.window.title or "")

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        await _dismiss(dispatcher, self._labels.dismiss_button, event.window.window_id)
        logger.warning(
            "IBController > unrecognized username or password -- login rejected"
        )
        raise CredentialsRejectedError("unrecognized username or password")


class GatewayConnectionFailedRecognizer:
    """Matches Gateway's generic `JDialog` titled "Gateway" (ported from
    IBC's `GatewayDialogHandler`) whose text, HTML-stripped, starts with
    `labels.message_prefix` -- that title is reused for unrelated dialogs
    too, so the prefixed text is the real signal, not the title alone.

    Always dismisses and logs the text, like IBC. A reason listed in
    `labels.known_reasons` (e.g. "multiple Paper Trading users") is a
    credential/account problem -- raises `CredentialsRejectedError` (exit 77, no
    retry). Any other reason (e.g. a stale restart token, IBC's own known
    cause for this dialog) is transient -- raises `TransientLoginError`."""

    def __init__(self, labels: GatewayConnectionFailedLabels) -> None:
        self._labels = labels

    def _stripped_text(self, components: list[Component]) -> str | None:
        for c in components:
            raw = c.text if c.text is not None else c.accessible_name
            if raw is None:
                continue
            stripped = _strip_html(raw)
            if stripped.startswith(self._labels.message_prefix):
                return stripped
        return None

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        if self._labels.title not in (event.window.title or ""):
            return False
        return self._stripped_text(components) is not None

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        text = self._stripped_text(components) or ""
        logger.warning("IBController > %s", text)
        await _dismiss(dispatcher, self._labels.dismiss_button, event.window.window_id)
        if any(reason in text for reason in self._labels.known_reasons):
            raise CredentialsRejectedError(f"gateway connection failed: {text}")
        raise TransientLoginError(f"gateway connection failed: {text}")


class NotCurrentlyAvailableRecognizer:
    """Matches the "not currently available" login dialog by message text
    (ported from IBC's `NotCurrentlyAvailableDialogHandler`; IBC also requires
    "Login" in the title, not ported so a retitled dialog still matches).
    A server-side outage: dismisses it and raises `TransientLoginError`, which
    relaunches like IBC's close-and-restart."""

    def __init__(self, labels: NotCurrentlyAvailableLabels) -> None:
        self._labels = labels

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        marker = self._labels.message_marker.lower()
        return any(
            marker in _strip_html(raw).lower()
            for c in components
            if (raw := c.text if c.text is not None else c.accessible_name) is not None
        )

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        await _dismiss(dispatcher, self._labels.dismiss_button, event.window.window_id)
        logger.warning("IBController > system not currently available -- transient")
        raise TransientLoginError("login system not currently available")


class SessionRole(Enum):
    """Which side of an existing-session conflict this session is on."""

    NEW = auto()  # still logging in
    EXISTING = auto()  # already logged in when the other session connected


def _role(is_logged_in: Callable[[], bool]) -> SessionRole:
    return SessionRole.EXISTING if is_logged_in() else SessionRole.NEW


async def _cede(
    dispatcher: Dispatcher, buttons: list[str], event: WindowEvent, reason: str
) -> None:
    """Cancels the dialog, then ends the run: cancelling alone leaves the
    login screen up."""
    logger.info("IBController > %s -- ending this session", reason)
    await click(dispatcher, buttons, window_id=event.window.window_id)
    raise SessionCededError(reason)


class ExistingSessionRecognizer:
    """Matches an existing-session-conflict dialog by window title. `action`
    (`ExistingSessionAction`: manual/secondary/primary/primaryoverride) and
    the session's `SessionRole` select the response (table in
    `docs/Configuration.md`); `is_logged_in` tells the roles apart. Ending
    the session raises `SessionCededError`. Stateful: tracks whether a
    previous attempt was already made this process, one instance per
    running agent."""

    def __init__(
        self,
        labels: ExistingSessionLabels,
        action: ExistingSessionAction,
        is_logged_in: Callable[[], bool],
    ) -> None:
        self._labels = labels
        self._action = action
        self._is_logged_in = is_logged_in
        self._attempted = False

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        return self._labels.title in (event.window.title or "")

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        cancel = self._labels.cancel_buttons
        if self._action is ExistingSessionAction.MANUAL:
            logger.info(
                "IBController > existing session detected -- manual, leaving for"
                " the user"
            )
            return  # scenario 1: user must choose -- nothing to do
        if self._action is ExistingSessionAction.SECONDARY:
            # scenario 2: end this session, let the other one proceed
            await _cede(
                dispatcher, cancel, event, "existing session detected -- secondary"
            )
        if _role(self._is_logged_in) is SessionRole.NEW:
            if not self._attempted:
                self._attempted = True
                # scenario 3: don't know session A's type, continue this one
                logger.info(
                    "IBController > existing session detected -- first attempt, "
                    "continuing this session"
                )
                await click(
                    dispatcher,
                    self._labels.continue_buttons,
                    window_id=event.window.window_id,
                )
            else:
                # scenario 4: session A must be primary/primaryoverride
                await _cede(
                    dispatcher,
                    cancel,
                    event,
                    "existing session detected -- second attempt",
                )
        elif self._action is ExistingSessionAction.PRIMARY:
            # scenario 5: we are session A, primary -- continue this, let the other exit
            logger.info(
                "IBController > existing session detected -- primary, continuing this"
                " session"
            )
            await click(
                dispatcher,
                self._labels.continue_buttons,
                window_id=event.window.window_id,
            )
        else:
            # scenario 6: we are session A, primaryoverride -- let the other proceed
            await _cede(
                dispatcher,
                cancel,
                event,
                "existing session detected -- primaryoverride",
            )


class ReloginRecognizer:
    """Matches "Re-login is required" by title (IBC's `ReloginDialogHandler`).
    `primary`/`primaryoverride` click Re-login, which reconnects and hands the
    other session its own dialog; `secondary` ends this session, `manual`
    leaves the dialog. The login state is not touched, so the next
    existing-session dialog still sees this session as `EXISTING`."""

    def __init__(self, labels: ReloginLabels, action: ExistingSessionAction) -> None:
        self._labels = labels
        self._action = action

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        return self._labels.title in (event.window.title or "")

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        if self._action is ExistingSessionAction.MANUAL:
            logger.info(
                "IBController > re-login required -- manual, leaving for the user"
            )
        elif self._action is ExistingSessionAction.SECONDARY:
            await _cede(
                dispatcher,
                self._labels.cancel_buttons,
                event,
                "re-login required -- secondary",
            )
        else:
            logger.info("IBController > re-login required -- re-logging in")
            await click(
                dispatcher,
                self._labels.relogin_buttons,
                window_id=event.window.window_id,
            )


class TradingLoginHandoffRecognizer:
    """Matches "Trading Login Handoff" by title (IBC's
    `TradingLoginHandoffDialogHandler`). It
    only appears after this session chose to continue, so `primaryoverride`
    and `secondary` raise `InvalidSessionStateError`; `primary` disconnects
    the other session; `manual` leaves the dialog."""

    def __init__(
        self, labels: TradingLoginHandoffLabels, action: ExistingSessionAction
    ) -> None:
        self._labels = labels
        self._action = action

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        return self._labels.title in (event.window.title or "")

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        if self._action is ExistingSessionAction.MANUAL:
            logger.info(
                "IBController > trading login handoff -- manual, leaving for the user"
            )
        elif self._action is ExistingSessionAction.PRIMARY:
            logger.info(
                "IBController > trading login handoff -- primary, disconnecting"
                " the other session"
            )
            await click(
                dispatcher,
                self._labels.disconnect_buttons,
                window_id=event.window.window_id,
            )
        else:
            raise InvalidSessionStateError(
                f"trading login handoff with existing_session_action={self._action}"
            )


class AcceptIncomingConnectionsRecognizer:
    """Matches the "Accept incoming connection" dialog TWS/Gateway shows when
    a client connects on the API socket, by window title (ported from IBC's
    own `AcceptIncomingConnectionDialogHandler`; title matching, not IBC's
    body-label search, like every other simple dialog here).
    `action` (`AcceptIncomingConnections`: manual/accept/reject) selects the
    response; `MANUAL` leaves the dialog for the user, matching IBC's own
    handler."""

    def __init__(
        self,
        labels: AcceptIncomingConnectionLabels,
        action: AcceptIncomingConnections,
    ) -> None:
        self._labels = labels
        self._action = action

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        return self._labels.title in (event.window.title or "")

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        if self._action is AcceptIncomingConnections.MANUAL:
            logger.info(
                "IBController > incoming API connection -- manual, leaving for the user"
            )
            return
        if self._action is AcceptIncomingConnections.ACCEPT:
            await click(
                dispatcher,
                self._labels.accept_buttons,
                window_id=event.window.window_id,
            )
            logger.info("IBController > incoming API connection -- accepted")
        else:
            await click(
                dispatcher,
                self._labels.reject_buttons,
                window_id=event.window.window_id,
            )
            logger.info("IBController > incoming API connection -- rejected")


class ExitKind(Enum):
    """What TWS/Gateway is about to do, read from the progress window it shows
    just before its process exits."""

    SHUTDOWN = auto()
    RESTART = auto()


class ExitProgressRecognizer:
    """Matches the "Shutdown progress" and "Restart in progress" windows by title.
    Nothing to click, the window closes on its own; `handle` only logs. The
    control loop reads `kind_of` from `RecognizerRegistry.handled` to classify
    the exit that follows."""

    def __init__(self, labels: ShutdownLabels) -> None:
        self._labels = labels

    def kind_of(self, event: WindowEvent) -> ExitKind | None:
        title = event.window.title or ""
        if self._labels.shutdown_progress_title in title:
            return ExitKind.SHUTDOWN
        if self._labels.restart_progress_title in title:
            return ExitKind.RESTART
        return None

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        return self.kind_of(event) is not None

    async def handle(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> None:
        logger.info("IBController > %s window seen", event.window.title)


class RecognizerRegistry:
    """Ordered list of `Recognizer`s, checked in sequence; `dispatch()` runs
    the first one that matches. A bug in a recogniser propagates unchanged,
    not caught here: there is no plugin/config-rule tier to isolate failures."""

    def __init__(self, registry: Sequence[Recognizer]) -> None:
        self._registry = list(registry)
        self.handled: eventkit.Event = eventkit.Event()
        """Emits `(recognizer, event)` -- two positional args -- right after
        that recognizer's own `handle()` returns successfully. Lets a caller
        know a specific rule has run (e.g. to know a blocking dialog is now
        gone) before proceeding."""

    def recognises(self, event: WindowEvent, components: list[Component]) -> bool:
        """Read-only: whether any registered recogniser matches, without
        running its `handle()`. Used by `diagnostics.watch_for_diagnostics`
        to classify a window as "known"/"unknown" -- the same classification
        `dispatch()` itself uses, exposed without the side effect."""
        return any(r.recognises(event, components) for r in self._registry)

    async def dispatch(
        self,
        event: WindowEvent,
        components: list[Component],
        dispatcher: Dispatcher,
    ) -> bool:
        """Runs the first matching recogniser's `handle()` against
        `event`/`components`, emitting `handled`. Returns whether anything
        matched."""
        for recognizer in self._registry:
            if recognizer.recognises(event, components):
                await recognizer.handle(event, components, dispatcher)
                self.handled.emit(recognizer, event)
                return True
        return False


async def handle_window_opened(
    registry: RecognizerRegistry,
    event: WindowEvent,
    dispatcher: Dispatcher,
) -> bool:
    """Dumps `event.window`'s components, then dispatches: refuses (returns
    `False`) if the window shows a live credential field, otherwise routes it
    through `registry.dispatch`. Returns `False` if the window closed before
    it could be dumped (`WindowGoneError`) or if nothing matched."""
    try:
        components = await dump(
            dispatcher, window=event.window.title, window_id=event.window.window_id
        )
    except WindowGoneError:
        return False
    if is_credential_entry(components):
        return False
    return await registry.dispatch(event, components, dispatcher)


def build_default_registry(
    config: Config,
    labels: Labels,
    session: SessionState,
    schedule_retry: Callable[[float], None],
) -> RecognizerRegistry:
    """Login-outcome recognisers first, then declarative rules, then the other
    built-ins as the fallback. A declarative rule cannot shadow a dialog that
    carries a login outcome. `session` is read-only; `schedule_retry` is
    `TooManyFailedLoginAttemptsRecognizer`'s retry hook."""
    declarative: list[Recognizer] = [
        DeclarativeDismissRecognizer(rule) for rule in labels.dismiss_rules
    ]
    login_outcomes: list[Recognizer] = [
        NotCurrentlyAvailableRecognizer(labels.not_currently_available),
        LoginFailedRecognizer(labels.login_failed),
        LoginErrorRecognizer(labels.login_error),
        UnrecognizedCredentialsRecognizer(labels.unrecognized_credentials),
        GatewayConnectionFailedRecognizer(labels.gateway_connection_failed),
        TooManyFailedLoginAttemptsRecognizer(
            labels.too_many_failed_login_attempts,
            config.relogin_after_mfa_timeout,
            schedule_retry,
        ),
    ]
    return RecognizerRegistry(
        [
            *login_outcomes,
            *declarative,
            ExistingSessionRecognizer(
                labels.existing_session,
                config.existing_session_action,
                session.is_logged_in,
            ),
            ReloginRecognizer(labels.relogin, config.existing_session_action),
            TradingLoginHandoffRecognizer(
                labels.trading_login_handoff, config.existing_session_action
            ),
            AcceptIncomingConnectionsRecognizer(
                labels.accept_incoming_connection,
                config.accept_incoming_connections,
            ),
            ExitProgressRecognizer(labels.shutdown),
        ]
    )


async def watch_for_unprompted_windows(
    registry: RecognizerRegistry,
    dispatcher: Dispatcher,
) -> None:
    """Runs for the agent's whole lifetime: watches `dispatcher.subscribe()`
    for `window_opened` events and routes each one through
    `handle_window_opened`, logging any that go unhandled. A failed action on
    one window (`AgentClientError`) is logged and the watch continues; login
    outcome exceptions propagate. Meant to be
    started via `asyncio.create_task`, not awaited directly -- it never
    returns on its own."""
    with dispatcher.subscribe() as sub:
        async for event in sub:
            if event.kind != "window_opened":
                continue
            try:
                handled = await handle_window_opened(registry, event, dispatcher)
            except AgentClientError as exc:  # one window must not end the session
                logger.warning(
                    "could not handle window %s %r: %s",
                    event.window.class_,
                    event.window.title,
                    exc,
                )
                continue
            if not handled:
                logger.info(
                    "unhandled unprompted window: %s %r",
                    event.window.class_,
                    event.window.title,
                )
