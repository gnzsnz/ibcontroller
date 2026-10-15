"""Root of the exception hierarchy for failures that end a run or a cycle.

Each exit-code group is a base class carrying its `exit_code` (BSD sysexits, the
contract from gitea #60), so `cli.py` reads the code off the exception instead of
keeping a list of leaf types:

- `ConfigError` (`config.py`): 78, deployment error, do not retry.
- `login.CredentialsRejectedError`: 77, credentials rejected, do not retry.
- `login.MfaTimeoutError`: 75, login did not complete after 2FA, retry per policy.
- `login.InvalidSessionStateError`: 76, a dialog `existing_session_action` rules out,
  do not retry.
- `login.SessionCededError`: 0, this session yielded to another on purpose, no retry.
- `TransientError` (below): 69, transient runtime failure, retry.

The specific cause stays in the logs and in the `stopped: <CAUSE>` line. A subclass
that never reaches `cli.py` keeps the default `exit_code` of 1."""

from typing import ClassVar


class IBControllerError(Exception):
    """Base of every error ibcontroller raises on purpose; anything else that
    escapes is a bug and keeps its traceback."""

    exit_code: ClassVar[int] = 1


class TransientError(IBControllerError):
    """A runtime failure worth retrying: the agent or its connection, or the login
    flow, failed for a reason that is not a configuration or credentials problem."""

    exit_code: ClassVar[int] = 69  # EX_UNAVAILABLE
