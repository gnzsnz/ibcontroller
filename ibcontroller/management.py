"""L5 Management: runtime operations on a running TWS/Gateway instance.

Management decides how an operation is done on the UI; its callers decide when
(L6 today, the control API later). Today it holds graceful shutdown. Restart,
data-farm reconnect and similar operations belong here when they are built.
"""

from __future__ import annotations

import asyncio
import logging

from ibcontroller.actions import navigate_menu
from ibcontroller.agent_client import AgentClientError, ProtocolError
from ibcontroller.config import Program
from ibcontroller.labels import ShutdownLabels
from ibcontroller.launcher import LaunchedInstance, terminate_and_wait

logger = logging.getLogger(__name__)


async def clean_shutdown(
    launched: LaunchedInstance,
    *,
    program: Program,
    logged_in: bool,
    labels: ShutdownLabels,
    main_window_id: str | None = None,
    timeout: float = 8.0,  # measured shutdown p99 4.3 s, max 4.9 s (#117)
) -> None:
    """Shutdown is not a process signal if login has completed -- invokes the
    app's own File -> Exit (TWS) / File -> Close (Gateway) menu item first, so
    it can save session state and upload settings. Only hard-terminates
    directly when login never completed (nothing to save), or as a last
    resort if the clean path hangs.

    `labels` supplies the menu path -- see `labels.ShutdownLabels`'s own
    docstring.

    `logged_in` is the caller's own responsibility to know; this layer
    doesn't track login state itself.

    `main_window_id` (`LoginManager.main_window_id`) scopes the menu lookup
    to the main window's own menu bar, matching IBC's `StopTask`. The
    unscoped search can resolve another frame's menu bar, which has no exit
    item (#76). `None` (Gateway) keeps the unscoped search.

    Uses `navigate_menu`, not `click`: "Close"/"Exit" are menu items, not
    ordinary buttons -- `click`'s `findByAccessibleName` lookup can never
    find them, since a `JMenu`'s dropdown lives in a `JPopupMenu`, outside
    the ordinary component tree while the menu is closed. Goes through
    `actions.navigate_menu` rather than a direct `dispatcher.send`
    call, so a shutdown landing while the menu item is momentarily disabled
    still retries instead of failing immediately; a short `timeout` bounds
    that retry so a genuinely stuck menu still falls through to the
    hard-kill fallback promptly.

    Checks `launched.process.returncode` first: a process that already
    exited on its own before this was ever called -- the user manually
    closing Gateway, or a scheduled restart -- has no live command socket
    left to send `navigate_menu` over, and no OS PID left for
    `terminate()` to signal, so the already-exited branch only stops the
    dispatcher. `terminate()` stays reserved for the "never logged in, but
    still running" branch, where the process genuinely is still alive. The
    narrower race of the process dying between the `returncode` check and
    the command being sent surfaces as `ProtocolError` (an
    `AgentClientError`), caught below. Every hard-terminate waits for the
    process to exit (`launcher.terminate_and_wait`) -- `run_control_loop`
    may relaunch immediately, and a still-alive JVM holds the command socket.
    A `ProtocolError` from the menu command skips the wait for exit: the click
    never happened.
    """
    if launched.process.returncode is not None:
        logger.info(
            "IBController > shutting down (process already exited) -- nothing to "
            "terminate"
        )
        await launched.dispatcher.stop()
        return

    if not logged_in:
        logger.info(
            "IBController > shutting down (never logged in) -- terminating directly"
        )
        await terminate_and_wait(launched.process)
        await launched.dispatcher.stop()
        return

    target = (
        labels.gateway_menu_path if program == Program.GATEWAY else labels.tws_menu_path
    )
    try:
        await navigate_menu(
            launched.dispatcher, target, timeout=5.0, window_id=main_window_id
        )
    except ProtocolError as exc:
        # Broken connection: the menu was never clicked, so no exit is coming.
        logger.warning(
            "IBController > clean shutdown via %s failed (%s) -- terminating",
            target,
            exc,
        )
        await terminate_and_wait(launched.process)
        await launched.dispatcher.stop()
        return
    except (AgentClientError, TimeoutError):
        pass  # best-effort -- the wait-then-terminate fallback below is what matters

    try:
        await asyncio.wait_for(launched.process.wait(), timeout=timeout)
        logger.info(
            "IBController > shut down cleanly via %s (returncode=%s)",
            target,
            launched.process.returncode,
        )
    except TimeoutError:
        logger.warning(
            "IBController > clean shutdown via %s did not exit within %ss -- "
            "terminating",
            target,
            timeout,
        )
        await terminate_and_wait(launched.process)
    await launched.dispatcher.stop()
