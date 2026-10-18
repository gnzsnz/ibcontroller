# Development environment

How to get `ibcontroller` itself, and the Java agent it drives, running locally.

## Target platform

The supported platforms are macOS and Linux.

## Python toolchain

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # if uv isn't already installed
cd ibcontroller
uv sync                                           # installs the dev dependency group too
```

Requires Python 3.12+ (pinned in `.python-version`). Day-to-day commands:

```bash
uv run pytest                # test suite (pytest-asyncio, pytest-aiohttp already in the dev group)
uv run ruff check .          # lint
uv run ruff format .         # format
uv run pyrefly check         # type check
uv run bandit -c pyproject.toml -r ibcontroller/   # security lint
```

## pre-commit

`.pre-commit-config.yaml` (project root) runs ruff (lint + format), bandit, pyrefly, and basic
hygiene hooks (trailing whitespace, end-of-file, yaml/toml checks, large-file check) on every
commit — the same checks as the `uv run` commands above, wired to run automatically instead of by
hand.

```bash
uv run pre-commit install    # one-time per clone: installs the git hook
uv run pre-commit run --all-files   # run every hook against the whole tree, not just staged files
```

CI runs the same config, so a clean local `pre-commit run --all-files` is a reliable predictor of
CI passing.

## Java toolchain

The agent needs to build against, and run compatibly with, the JRE TWS/Gateway bundles.

**Local dev JDK, via SDKMAN:** a real Azul account/API token is needed for the exact SA-tier build
above; without one, the closest public match is Zulu's CA (Certified Availability) tier at the
same JDK version. SDKMAN manages that install and pins the version per project:

One-time install (adds SDKMAN's init to your shell rc automatically):

```bash
curl -s "https://get.sdkman.io" | bash
# install
sdk install java 17.0.15-zulu
```

The pinned Java version lives in `.sdkmanrc` at the project root (checked in):

```env
java=17.0.15-zulu
```

Activate it per clone — SDKMAN then sets `JAVA_HOME` for the current shell:

```bash
sdk env use   # reads .sdkmanrc; installs the pinned JDK on first use
java -version
openjdk version "17.0.15" 2025-04-15 LTS
OpenJDK Runtime Environment Zulu17.58+21-CA (build 17.0.15+6-LTS)
OpenJDK 64-Bit Server VM Zulu17.58+21-CA (build 17.0.15+6-LTS, mixed mode, sharing)
```

Enable auto-env (`sdkman_auto_env=true` in `~/.sdkman/etc/config`) to have `JAVA_HOME` set
automatically when entering the project directory, instead of running `sdk env use` in every new
terminal. Either way, `JAVA_HOME` is the contract: the `Makefile` reads it (`JDK ?= $(JAVA_HOME)`),
and CI's `actions/setup-java` (zulu 17) sets the same variable, so local and CI build with the
same pin.

This JDK is for *compiling* only — its own version doesn't need to match whatever Gateway/TWS
happens to bundle locally . The `Makefile`'s `--release 17` tells `javac` to target Java 17
bytecode/API regardless of which JDK compiles it, so the output runs correctly on whatever
JRE TWS/gateway is using.

The agent is small, so keep the build as plain
as it can be: `javac` + `jar`, wired through a small `Makefile`.

## TWS / IBKR Gateway itself

Download from IBKR directly [gateway](https://www.interactivebrokers.com/en/trading/ibgateway-latest.php) or [TWS](https://www.interactivebrokers.com/en/trading/download-tws.php). Then install.

After the first run, `ibcontroller` renames the gateway/TWS script. This is required to manage restarts.

## Building and attaching the agent

Java agent file structure:

```text
agent/src/main/java/ibcontroller/agent/
  AgentMain.java         — entry point: both sockets, EventBridge installed before Gateway launches
  Protocol.java          — command accept loop, hand-rolled JSON codec, full command dispatch
  ComponentLookup.java   — read-only Swing tree walk, dump/get_text
  WriteOps.java          — EDT-dispatched writes, set_text/set_checkbox/click
  EventBridge.java       — AWTEventListener install + NDJSON event forwarding
  Security.java          — password redaction, the credential-field boundary
agent/src/test/java/ibcontroller/agent/
  AgentTests.java        — dependency-free unit tests (parser depth, accept loop, deferred click)
```

Build using`Makefile`:

```bash
make build    # build/ibcontroller-agent.jar, then copies it into ibcontroller/ (for uv/pyproject packaging)
make test       # build, run the Java unit tests, then `uv run pytest -n auto` (parallel, pytest-xdist)
make test-java  # compile and run the agent's unit tests (plain javac + java, no test framework)
make dist     # build the jar, then uv build the Python package (jar is required package-data, see Makefile)
make clean    # rm -rf build dist
```

Day-to-day cycle: `make build` and `make test-java` after any agent-side change, `uv run pytest` for the
Python side (serial; add `-n auto` to run in parallel, as `make test` and CI do;
`tests/test_agent_jar.py` drives the built jar through `agent_client` and skips without a JDK or jar),
`pre-commit run --all-files` before pushing. `make test` runs all three test steps.

## Running ibcontroller

```bash
# from the git repo
source .venv/bin/activate
ibcontroller run --dotenv=.env-paper --trading-mode=paper --program=gateway
```

## Architecture

`ibcontroller` follows a layered architecture with separation of concerns between layers.
Any contribution must follow this separation of concerns.

![ibcontroller schematic architecture](Architecture.svg)

### Rules for every layer

- A layer talks only to the layer directly below it. The exceptions are listed per layer.
- L3's schema types (`WindowEvent`, `WindowInfo`, `Component`, …) and exceptions are the shared
  vocabulary of every layer above L3. Changing them is a cross-layer change.
- Everything runs on one asyncio loop. No blocking call on the loop: file and process work goes
  through `anyio.to_thread` or the logging listener threads.
- One object graph per running instance (`Dispatcher`, `LoginManager`, recogniser registry, sockets,
  settings directory). No module-level state shared between instances.
- Every user-facing string (window titles, button and menu labels) comes from `labels.json` through
  `labels.py`. Never a Python literal.
- Credentials stay `Secret` until L3 builds the request line. Nothing logs or traces a credential
  value.

### L1 Process (`launcher.py`)

`launcher.py` starts one TWS/Gateway instance and hands it over attached. It resolves the install,
channel, JRE, JVM options and per-instance settings directory, then spawns the JVM with the Java
agent as its entry point (L2), drains the JVM's output, and waits for the agent to answer `ping`.
It returns a running instance with a started `Dispatcher` (L4). `control_loop.py` (L6) owns the
relaunch decision.

- **Owns:** TWS/Gateway install discovery (macOS and Linux, channel filtering), JRE selection, the
  JVM command line, the per-instance settings directory (including reading Gateway/TWS's
  `autorestart` marker) and socket paths, native-restart prevention, spawning the JVM with the
  agent, draining its stdout, and readiness (`ping`).
- **Provides:** `LaunchedInstance`: the process, a started `Dispatcher` (L4), socket paths,
  settings directory.
- **Rules:** one launch per call. Relaunch policy belongs to L6. L1 may construct L3 and L4 objects
  ("spawn and attach"). A launch that fails or is cancelled after the spawn terminates the JVM
  before raising; it never leaves an orphan. Readiness is bounded by `ready_timeout`, each probe
  by its own timeout. `build_launch_plan` only reads; `prepare_installation` makes the disk
  changes (directories, `jts.ini`, native launcher rename), and any `OSError` there becomes a
  `LauncherError`. Problems detectable before that (socket path too long) raise before the
  install is touched. Synchronous filesystem work runs through `anyio.to_thread`. L1 does not
  import L5 or L5-1.

### L2 Java agent (`agent/`)

The agent is a small Java component that runs inside the TWS/Gateway JVM, loaded by L1, which makes
`AgentMain` the JVM's `main()`. It installs an AWT window listener, binds two Unix sockets, then
starts the real TWS/Gateway entry point. It gives Python the same reach IBC has, `setText()` and
`doClick()` on the live Swing components and every window as it opens, and decides nothing: all login,
dialog and lifecycle logic stays in Python. Every command in the protocol below has a typed
`Request` class in L3, and every error code has an exception class.

- **Owns:** everything inside the TWS/Gateway JVM: starting the entry point, the AWT window
  listener and window-ID registry, the command socket (request/response, one client at a time),
  the event socket (`hello`, `snapshot`, `window_opened`/`window_closed`, `keepalive`, `overflow`),
  running every UI read and write on the EDT, and credential redaction (`dump` redacts, `get_text`
  refuses).
- **Provides:** the wire protocol to L3.
- **Rules:** the agent reports and executes; it makes no login, dialog or lifecycle decisions. Any
  protocol change updates L3 in the same change. The event stream is `hello`, `snapshot`, then only
  events newer than the snapshot; the snapshot lists exactly the windows in the window-ID registry
  (between `WINDOW_OPENED` and `WINDOW_CLOSED`).

**Command socket.** One JSON request line, one JSON response line, one client at a time. A response
is `{"ok": true, ...}` or `{"ok": false, "error": "<code>", "detail": "..."}`. `window_id` is
optional wherever it is listed and scopes the lookup to one window; a closed window answers
`window_gone`.

| Command               | Request fields                                   | Response (besides `ok`)          | EDT dispatch                       |
| --------------------- | ------------------------------------------------ | -------------------------------- | ---------------------------------- |
| `ping`                | none                                             | `version`, `uptime_s`            | none                               |
| `dump`                | `window` (title filter), `window_id`             | `components`                     | wait                               |
| `get_text`            | `target`, `window_id`                            | `value`                          | wait; refuses a password field     |
| `set_text`            | `target`, `value`, `window_id`                   | none                             | wait                               |
| `set_checkbox`        | `target`, `checked`, `window_id`                 | none                             | wait; clicks only if state differs |
| `click`               | `target`, `window_id`                            | none                             | click deferred; effect unconfirmed |
| `navigate_menu`       | `path` (`File/Exit`), `window_id`                | `clicked`                        | click deferred; disabled item: `clicked` false |
| `expand_tree`         | `path`, `window_id`                              | none                             | wait                               |
| `set_text_near_label` | `label`, `index`, `value`, `window_id`           | none                             | wait                               |
| `menu_item_exists`    | `path`, `window_id` (**required**)               | `exists`, `enabled`              | wait; never clicks                 |

Errors on the command socket:

| Code                       | L3 exception             | Meaning                                                                 |
| -------------------------- | ------------------------ | ----------------------------------------------------------------------- |
| `not_found`                | `ElementNotFoundError`   | No component or menu path matches the target.                           |
| `refused_credential_field` | `CredentialRefusedError` | `get_text` on a password field.                                         |
| `bad_request`              | `BadRequestError`        | Malformed JSON or a missing or wrongly typed field; the connection stays usable. |
| `unknown_command`          | `UnknownCommandError`    | The `cmd` is not in the table above.                                    |
| `agent_error`              | `AgentError`             | Any other failure inside the agent. L3 maps an unrecognised code here too. |
| `window_gone`              | `WindowGoneError`        | The `window_id` no longer resolves: the window closed after the caller learned of it. |

**Event socket.** Push only, one JSON line per message. Ids never repeat, so a client applies events
idempotently.

| Message    | Shape                                                    | When                                                                      |
| ---------- | -------------------------------------------------------- | ------------------------------------------------------------------------- |
| `hello`    | `{"type":"hello","protocol_version":1}`                  | On connect. Logged, never checked.                                        |
| `snapshot` | `{"type":"snapshot","windows":[...]}`                    | Right after `hello`: the windows in the registry.                         |
| `event`    | `{"type":"event","seq":N,"kind":"window_opened","window":{...}}` | Each window open or close. `kind` is `window_opened` or `window_closed`. |
| `overflow` | `{"type":"overflow","from_seq":N}`                       | The agent's queue dropped events. Never silent.                           |
| `keepalive`| `{"type":"keepalive","ts":...}`                          | Every 30 s, to tell a quiet agent from a dead one.                        |

### L3 Agent client (`agent_client.py`)

L3 is the Python mirror of L2's wire protocol. Each L2 command has one `Request` class, each L2 error
code has one exception class, and each event socket message has one schema type. The two connections
turn the agent's sockets into typed values and typed errors once, so the layers above never handle
raw JSON dicts or error strings. L3 does framing, serialization and error mapping only; ordering and
fan-out are L4's job.

- **Owns:** the two socket connections, JSON line framing, the typed message schemas
  (`attrs`/`cattrs`), and the typed exceptions mapping L2's error codes.
- **Provides:** one frozen `attrs` `Request` class per agent command (fields are the wire fields,
  class-level `cmd` and `timeout`, `parse()` for the response), `request()` to send one, `ping()`
  for the launcher probe; an async iterator over event messages.
- **Rules:** no scheduling, no fan-out, no imports from other `ibcontroller` modules. A request
  serializes itself: `Request.wire()` is the only place a `Secret` is unwrapped, `trace()` redacts
  it. A new command is a new `Request` class, never a method. The wire has no request IDs, so
  a command connection that fails mid-request (transport error, malformed line, cancellation) is
  marked broken and every later request raises `ProtocolError`; there is no reconnect. Each
  request's wire round trip is bounded (30 s, 60 s for `Dump`; constants, not config) and a timeout
  breaks the connection the same way. Transport
  and response-shape failures are `ProtocolError`; only `connect()` raises `OSError`.

### L4 Dispatcher (`dispatch.py`)

L4 is the single gateway between the layers above and one running agent. One `Dispatcher` per
instance owns both L3 connections and serves the two directions. Commands go out through one FIFO
worker, one on the wire at a time, because the wire has no request IDs. Events come in through one
reader that keeps a registry of the open windows and fans each event out to ordered, buffered
subscriptions. L4 also writes the optional wire trace and reports a lost connection to L6. It routes
and records but never interprets: what a command or an event means is decided above.

- **Owns:** one `Dispatcher` per instance: both L3 connections, command serialization (one FIFO
  worker, one command on the wire at a time), event fan-out, and the optional wire trace.
- **Provides:** `send(request)`, `windows` (read-only registry of the open windows
  by `window_id`), `subscribe()` (an eager, ordered, buffered stream of `WindowEvent`s with
  `wait(kind, matches, timeout)`), `tasks` (for L6 to detect a lost connection: the
  event reader ends on an event connection loss, the command worker on a broken command
  connection), `start`/`stop`.
- **Rules:** never interprets commands or events beyond routing them and keeping `windows`
  (idempotent updates keyed by `window_id`; a `snapshot` replaces it). An `overflow` event ends the
  event reader with `ProtocolError` (events were lost, so `windows` can't be trusted). `stop()` never raises and
  closes both connections. When the worker ends (stop or broken connection) every queued and
  in-flight caller, and any later `send`, gets `ProtocolError`.

### L5-1 Actions (`actions.py`, `labels.py`)

L5-1 is the vocabulary the domains use to act on the UI. Each action is a named, async operation
(`click`, `type_text`, `navigate_menu`, ...) that builds the matching L3 `Request`, sends it through the
`Dispatcher`, and adds only the policy that belongs above the wire: `click` tries candidate labels in
order, `navigate_menu` retries a disabled item. `labels.py` supplies the matching UI text from
`labels.json`, so no domain holds a literal. Domains never talk to L4 directly; the actions are their only
way to the UI.

- **Owns:** the action vocabulary (`click`, `type_text`, `toggle`, `navigate_menu`, `expand_tree`,
  `dump`, …), `wait_for_window` and the label data.
- **Rules:** the only L5 code that calls `Dispatcher.send`; each action builds its L3 `Request`.
  L3 exceptions propagate unchanged.
  `wait_for_window(matches, closed=, timeout)` is the level wait (open or closed now, or later).
  Edge waits (the next window to open after an action) hold a `Dispatcher.subscribe()` across the
  action. Nothing outside L4 touches the raw event stream except through `subscribe()`. Actions
  take a `window_id` wherever the agent supports one. Callers pass it whenever they hold one: the
  unscoped lookups pick the first matching window, which may be the wrong one (#40, #76).
  `navigate_menu` retries a disabled item for `timeout` and a path that does not resolve for only
  `not_found_timeout` (5s), so a wrong path fails fast.
  `load_labels` is strict: a wrong type or an empty candidate list raises `LabelsError`, a
  `ConfigError` (exit 78) naming the file and field; unknown keys are logged and ignored.

**Action semantics, as callers rely on them.** What "the action returned" guarantees:

| Action                              | Agent side (L2)                                    | Returns when                                               |
| ----------------------------------- | -------------------------------------------------- | ---------------------------------------------------------- |
| `type_text`, `type_text_near_label` | `setText` on the EDT, awaited                      | value set                                                  |
| `toggle`                            | `doClick()` on the EDT, awaited, only if state differs | state set                                              |
| `click`, `dismiss`                  | lookup awaited, `doClick()` deferred               | agent accepted the command; the effect has not happened yet |
| `navigate_menu`                     | as `click`; `clicked` is false for a disabled item | accepted; Python retries a disabled item and an unresolved path |
| `expand_tree`                       | selection on the EDT, awaited                      | panel swapped                                              |
| `dump`, `menu_item_exists`, `read_text` | read on the EDT, awaited                       | value read                                                 |
| `wait_for_window`                   | none (L4 registry and subscription)                | a matching window is open (or closed) now, or after waiting |

After `click`, `dismiss` or `navigate_menu`, a step that depends on the effect waits for it with
`wait_for_window`.

### L5 Domains

The domains are where ibcontroller decides what to do on the TWS/Gateway UI: log in, handle windows nobody
is waiting for, apply settings, shut down. Each domain acts on the UI only through L5-1 and reports its
outcome to L6 as a return value or a typed exception. L6 decides what happens next. Domains do not
call each other.

| Domain     | Code                                                          | Role                                                                          |
| ---------- | ------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Login      | `login.py`, login-outcome recognisers in `recognisers.py`     | Fill credentials, detect success, MFA and failure, report the outcome.         |
| Dialogs    | `RecognizerRegistry`, `watch_for_unprompted_windows`          | Recognise and handle windows nobody is waiting for.                          |
| Settings   | `settings.py`, `builtin_settings.toml`, `ibkr_settings.toml`  | Apply declared Global Configuration settings.                                |
| Scheduling | `schedule.py`                                                  | Wall-clock maths for cold restart and close-down. No I/O.                     |
| Diagnostic | `diagnostics.py`                                               | Dump window structure for development and support.                           |
| Management | `management.py`                                                | Runtime operations on a running instance: graceful shutdown today; restart and data-farm reconnect when built. Decides how; callers (L6, later L7) decide when. |

- **Rules:**
  - Act on the UI only through L5-1.
  - Report outcomes to L6 as return values or typed exceptions. L6 decides relaunch, exit or
    continue.
  - Outcome exceptions derive from `IBControllerError` (`errors.py`). Each exit-code group is a
    base class carrying its `exit_code`, so a new exception picks its exit code by base class (the
    codes are listed under L6). The login-outcome exceptions live in `login.py`, wherever the
    detecting recogniser lives.
  - Login waits for its outcome with no overall deadline (as IBC); only `login_dialog_display_timeout`
    and the `mfa_exit_interval` watchdog raise. A retry (too-many-attempts cooldown, late 2FA close)
    only resubmits credentials, the original wait continues, and `run()` cancels a pending retry
    when it ends.
  - Domains do not call each other; L6 wires them together. L6 builds the registry
    (`build_default_registry`) and runs the settings sequence (`apply_declarative_settings`), one
    call per domain. A step that belongs to another domain (the diagnostic settings sweep) is
    passed in by L6 as a hook (`after_apply`).
  - A domain reads the login session only through the read-only `SessionState` protocol (see
    Session state below), never `LoginManager` itself.
  - A recogniser acts only on the window it matched (`window_id`). The dialogs registry never acts
    on a window with a visible credential field.
  - The registry dispatches to the first matching recogniser: login-outcome and exit progress
    recognisers, then declarative `dismiss_rules`, then the other built-ins. A declarative rule
    cannot shadow a dialog that carries a login outcome or announces an exit.
- **Known deviations:**
  - `recognisers.py` and `diagnostics.py` iterate `Dispatcher.subscribe()` directly instead of going
    through L5-1.
  - Scheduled shutdown timing lives in L6 (`schedule.py` maths, `control_loop.py` trigger); the
    shutdown itself is Management's `clean_shutdown`.

**Session state.** `LoginManager` owns the session facts. Other domains and L6 see only the
`SessionState` protocol (`login.py`):

- `is_logged_in()`: true once login has completed.
- `main_window_id`: the id of the main frame, the scope for settings and shutdown. TWS takes it from the
  window that shows `File/Lock Application`, Gateway from the non-login "IBKR Gateway" frame that shows
  `Help/About Gateway` (a menu probe, because the class names are obfuscated). It is captured when the
  splash closes and is `None` when no frame matches, and consumers then search unscoped.
- `LoginState` is login-sequence progress only. `LOGGED_IN` is terminal for the cycle: a later session
  loss does not change it, and failures leave as exceptions.

### L6 Control loop (`control_loop.py`)

L6 supervises one instance. It runs one cycle per JVM: launch, login with the dialogs watcher, settings,
ready, shutdown. It races every phase against process exit and connection loss, so a dying agent never
leaves a cycle waiting. When a cycle ends, L6 classifies why (`ShutdownCause`) and decides whether to
relaunch or stop. Cancellation is the only stop request, and every exit path reaches `clean_shutdown`, so
a stopped cycle does not leave a JVM running.

- **Owns:** one instance's lifecycle, one cycle per JVM: launch → login (with the dialogs watcher) →
  settings → ready → shutdown. Races every phase against process exit and connection loss,
  classifies why the cycle ended (`ShutdownCause`), applies the relaunch policy, and owns every
  task started during a cycle.
- **Provides:** `run_control_loop`, cancelled to request a graceful stop. `cli.py` maps its result
  and any `IBControllerError` to its `exit_code`.
- **Rules:** the only layer that decides relaunch or stop. Cancellation is the only stop request.
  The "Shutdown progress" and "Restart in progress" windows (`ExitProgressRecognizer`) classify a
  clean exit: shutdown stops without polling for the `autorestart` marker; restart with the marker
  relaunches with its hash; restart without it on TWS is a File > Logout and relaunches with a
  fresh login. No window seen falls back to the marker poll. Relaunches of laps that never reach
  READY are capped by `restart_cap`.

**Relaunch policy.** `run_control_loop` decides after each cycle:

| Cycle ends with                                                              | Relaunch      | Next login                    |
| ---------------------------------------------------------------------------- | ------------- | ----------------------------- |
| `PROCESS_CLOSED` or `PROCESS_EXITED`, `autorestart` marker present          | yes           | silent, `-Drestart=<hash>`    |
| `PROCESS_CLOSED`, "Restart in progress" window, no marker, TWS (File > Logout) | yes         | full fresh login              |
| `COLD_RESTART` (`cold_restart_time`)                                         | yes           | full fresh login              |
| `LoginFrameTimeoutError` (the login frame never appeared)                    | yes           | keeps the current hash        |
| `TransientLoginError` ("Login failed" or an unclassified connection failure) | yes          | full fresh login              |
| `MfaTimeoutError` with `mfa_timeout_action=restart`                          | yes           | full fresh login              |
| "Shutdown progress" window seen before the exit                              | no, stop      | —                             |
| `REQUESTED`, `TIDY_CLOSEDOWN`                                                | no, stop      | —                             |
| `CONNECTION_LOST`; process gone with no marker and no restart window         | no, stop      | —                             |
| `MfaTimeoutError` with `mfa_timeout_action=exit` (default)                   | no, re-raised | —                             |

A relaunch counts toward `restart_cap` and the count resets when a lap reaches READY. Every stop maps
to an exit code in the table below.

**Exit codes.** `cli.py` reads the exit code off the exception that ended the run, or off the
`ShutdownCause` when none was raised (BSD sysexits; a supervisor decides retries from them):

| Code | Source                                                     | Meaning                                              | Retry |
| ---- | ---------------------------------------------------------- | ---------------------------------------------------- | ----- |
| 0    | `REQUESTED`, `TIDY_CLOSEDOWN`, `PROCESS_CLOSED`, `SessionCededError` | Stopped on request, at the scheduled close-down, by File > Exit, or by yielding to another session | no |
| 69   | `TransientError`, `PROCESS_EXITED`, `CONNECTION_LOST`      | Runtime failure: the agent, its connection, or the login flow | yes |
| 70   | `RestartCapExceededError`                                  | `restart_cap` relaunches in a row never reached READY | no |
| 75   | `MfaTimeoutError`                                          | Login did not complete after 2FA                     | per policy |
| 76   | `InvalidSessionStateError`                                 | A dialog the `existing_session_action` rules out     | no |
| 77   | `CredentialsRejectedError`                                 | Credentials rejected                                 | no |
| 78   | `ConfigError`                                              | Deployment error (config, `labels.json`, launcher)   | no |

Any other exception keeps its traceback and exits 1.

### L7 Control API (planned, not built)

The contract below is provisional: it fixes the boundaries, not the interface.

L7 is the planned way to reach a running instance from outside the process. It would translate an
external request (status, stop, restart, reconnect) into a call on L6 or on a Management operation,
and return the instance state. It adds no behaviour of its own: L6 stays the only owner of the cycle,
and the UI is reached only through Management. Nothing is built, and of the candidate operations
below only stop exists today, as Management's graceful shutdown.

- **Purpose:** let something outside the process observe a running instance and request lifecycle
  operations on it: an operator, a supervisor, a container health check.
- **Owns:** the external interface: transport, access control, request validation, response
  format, and mapping each request to an L6 or Management operation.
- **Consumes:** L6, for lifecycle requests and instance state (startup state, login state, last
  `ShutdownCause`), and Management (L5) operations, through L6.
- **Rules:**
  - Never reaches L4, L3 or L2 directly, and never drives the UI through L5-1 itself. A UI
    operation is a Management operation, so serialization, window scoping and logging stay in L5.
  - Never stops, kills or relaunches the process itself. It asks L6, which stays the only owner of
    the cycle.
  - Requests are intents, not blocking calls: a long operation is accepted, and its progress is
    observable through state.
  - Scoped to one instance, like every other layer.
  - Local-only by default. Remote access needs explicit configuration, like IBC's `BindAddress` and
    `ControlFrom`.
  - Never returns credentials. Diagnostic output follows the same redaction as `dump`.

**Candidate operations, not committed**, aiming at parity with IBC's `CommandServer`:

| Operation                         | Description                                                                                                  |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Stop                              | Management's graceful shutdown works today. L6 stops by task cancellation only, so L7 needs an L6 entry point that requests it. |
| Restart, cold restart             | Scheduled cold restart exists in L6. On demand needs a live check that `File/Restart...` exists on both programs and an L6 outcome for it. |
| Reconnect data, reconnect account | Not built. Needs a key-event primitive in L2, L3 and L5-1, and a handler for the confirmation dialog.        |
| Enable API                        | Not built, TWS only. Needs a decision: Management operation or Settings entry.                               |
| Status                            | Not built. Startup state, login state and `main_window_id` are available; a session lost after login needs a new agent event. |
| Diagnostic dump                   | `diagnostics.py` exists. Needs a Management wrapper so L7 does not reach L5-1.                               |

**Open questions:** transport and access control; which IBC commands to support; whether
user-authored rules and plugins attach at L7 or at L5.
