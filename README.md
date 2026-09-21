# ibcontroller

An ibgateway/TWS automation tool.

Heavily inspired by [IBC](https://github.com/IbcAlpha/IBC) project, now archived.

## What it does?

- Finds ibgateway/TWS installation, JRE and launchs it.
- Automates user/password entry, waits for MFA.
- Automates ibgatewa/TWS settings.
- Manages pop-ups and automatically accept.

- Manages ibgateway/TWS restarts.

It provides a "**declarative**" engine, so settings and pop-ups can be "declared" by configuration entries.

## How to use it

```bash
# 1. Build the Java agent jar and install the package (editable, for a source checkout)
make dist
uv sync

# 2. Scaffold a starter config directory (platformdirs default, or $IBCONTROLLER_APP_DIR)
uv run ibcontroller init

# 3. Export IBCONTROLLER_USERID / IBCONTROLLER_PASSWORD (see Credentials below) -- the
#    only settings actually required. Everything else in the base ibcontroller.toml
#    is commented out and already safe to run as-is: trading_mode defaults to "paper",
#    and tws_version is auto-detected from standard tws_path.

# 4. Run it -- Ctrl-C for a graceful shutdown
uv run ibcontroller run
```

`ibcontroller run` scaffolds the config directory automatically the first time
`ibcontroller.toml` is missing (same effect as running `init` first), so it can also be
run directly on a fresh install -- it will still fail with a clear error naming whichever
setting is actually missing (in practice, just the two credential env vars).

`ibcontroller version` prints the installed package version.

### CLI parameters

You can use the following cli parameters:

```text
 Usage: ibcontroller [OPTIONS] COMMAND [ARGS]...

 Login/launch controller for TWS and IBKR Gateway.

╭─ Options ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ --install-completion          Install completion for the current shell.                                                                 │
│ --show-completion             Show completion for the current shell, to copy it or customize the installation.                          │
│ --help                        Show this message and exit.                                                                               │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ init     Scaffold the config directory with starter ibcontroller.toml/                                                                  │
│          ibkr_settings.toml.example/labels.json.example files. Never overwrites a file                                                  │
│          that already exists unless --force is given.                                                                                   │
│ run      Run one ibcontroller instance until it stops (Ctrl-C for a graceful shutdown,                                                  │
│          or Gateway/TWS exiting or restarting on its own). Scaffolds the config directory                                               │
│          first if it's missing, same as `ibcontroller init`.                                                                            │
│ version  Print the installed ibcontroller version.                                                                                      │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

And the `ibcontroller run` parameters:

```text
 Usage: ibcontroller run [OPTIONS]

 Run one ibcontroller instance until it stops (Ctrl-C for a graceful shutdown, or Gateway/TWS exiting or restarting on its own). Scaffolds the config directory first if it's
 missing, same as `ibcontroller init`.

 `--trading-mode`/`--tws-path`/`--tws-channel`/`--program`/`--tws-settings-path`/
 `--instance` let one invocation pick these `Config` fields directly, overriding
 TOML/env for this run only -- e.g. `ibcontroller run --trading-mode=live
 --dotenv=.env-live` and `ibcontroller run --trading-mode=paper
 --dotenv=.env-paper` run side by side from one shared config.

╭─ Options ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ --dotenv                   <path>        Optional .env file to load into the environment before reading config (only fills variables not already set).                          │
│ --app-dir                  <path>        Override IBCONTROLLER_APP_DIR for this invocation -- config/log/run all move under {app_dir}/{config,log,run} instead of the platform  │
│                                          default.                                                                                                                               │
│ --trading-mode             <live|paper>  Override Config.trading_mode ('live'/'paper') for this invocation.                                                                     │
│ --tws-path                 <path>        Override Config.tws_path (the TWS/Gateway install-path inference) for this invocation.                                                 │
│ --tws-channel              <str>         Override Config.tws_channel ('stable'/'latest', filters install auto-detection) for this invocation.                                   │
│ --program                  <str>         Override Config.program ('gateway'/'tws') for this invocation.                                                                         │
│ --tws-settings-path        <path>        Override Config.tws_settings_path (where TWS/Gateway stores its own settings) for this invocation.                                     │
│ --instance                 <str>         Override Config.instance (per-instance log/trace/socket names) for this invocation. Defaults to '{program}-{trading_mode}', so         │
│                                          --trading-mode alone is usually enough to keep paper/live instances apart.                                                             │
│ --help                                   Show this message and exit.                                                                                                            │
╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

### Running paper and live in parallel

To run parallel instances in `live`and `paper`trading mode, we need to pass credentials and trading mode:

```bash
# terminal 1
ibcontroller run --trading-mode=live --dotenv=.env-live
# terminal 2
ibcontroller run --trading-mode=paper --dotenv=.env-paper
```

`--app-dir` (also accepted by `init`) overrides `IBCONTROLLER_APP_DIR` for one
invocation, for pointing each instance's config/log/run dirs somewhere different too.

## Configuration

`ibcontroller` is configured through a **flat** TOML file (no `[section]` headers) and/or
environment variables, with precedence `defaults < TOML file < .env < environment`. Environment
can be loaded from a `.env` file. Credentials are environment-variable-only and must
never appear in the config file.

A sample file with all settings is provided as an [example](./ibcontroller.env).

`ibcontroller init` (or the first `ibcontroller run`) copies starter templates into the
config directory: `ibcontroller.toml` (every key commented out -- safe to run as-is,
since it changes nothing versus the defaults below), `ibkr_settings.toml.example`
(*extra* declarative Global Configuration settings, on top of the always-applied
built-in ones -- see "Declarative configuration" below), and `labels.json.example` (an
optional, sparse override for window/button labels and dismiss rules, applied once
renamed to `labels.json` -- see `labels.py`'s own docstring for the merge mechanism).
Both `.example` files are deliberately left inert (kept under their `.example` name)
until renamed -- neither auto-applies just by existing. The templates themselves are
bundled inside the installed package (`ibcontroller/data/`), not kept in this
repository.

### Credentials (env only, never in the configuration file)

A single pair is used regardless of `trading_mode`.

| Env var | Description |
| --- | --- |
| `IBCONTROLLER_USERID` | Account user id |
| `IBCONTROLLER_PASSWORD` | Account password |

Each also accepts a `_FILE`-suffixed variant (`IBCONTROLLER_USERID_FILE`,
`IBCONTROLLER_PASSWORD_FILE`) that reads the value from a file instead —
Docker/Compose secrets, so a value never has to sit in the process environment.

### File location

When environment variable `IBCONTROLLER_APP_DIR` is not set, then
`ibcontroller` will use the standard platform directories.

| Location | Directory | Platform |
| --- | --- | --- |
| `config_dir` | `~/.config/ibcontroller` | Linux |
| `config_dir` | `~/Library/Application Support/ibcontroller` | macOS |
| `log_dir` | `~/.local/share/ibcontroller` | Linux |
| `log_dir` | `~/Library/Logs/ibcontroller` | macOS |
| `socket_dir` | `/run/user/<uid>/ibcontroller` | Linux |
| `socket_dir` | `~/Library/Caches/TemporaryItems/ibcontroller` | macOS |

When `IBCONTROLLER_APP_DIR` is set then:

- `config_dir`-> `IBCONTROLLER_APP_DIR/config`
- `log_dir`-> `IBCONTROLLER_APP_DIR/log`
- `socket_dir` -> `IBCONTROLLER_APP_DIR/run`

### Settings

`ibcontroller`ships with sensible defaults.  **Only the two credential env vars below are actually mandatory**
 everything else here has a safe default or is auto-detected.

`None` on a `TWS/Gateway setting` row is a deliberate third state, not just
"unset" yes/no/unset convention: set it explicitly to apply value, or leave
it `None` to leave TWS/Gateway's existing setting alone. This is the actual
enable/disable mechanism for these fields, not a side effect of the type
allowing `None`. `Config`'s schema is closed — an unknown key in
`ibcontroller.toml` fails at startup with a clean error (`ConfigError`).

| TOML key | Env var or CLI flag | Description | Default |
| --- | --- | --- | --- |
| `instance` | `IBCONTROLLER_INSTANCE`, `--instance` | Instance name; separate log/trace files per instance | `"{program}-{trading_mode}"` (e.g. `"gateway-paper"`) |
| `program` | `IBCONTROLLER_PROGRAM`, `--program` | `"gateway"` or `"tws"` | `"gateway"` |
| `tws_version` | `IBCONTROLLER_TWS_VERSION` | Installed TWS/Gateway version, e.g. `"10.50"` | `None` — auto-detected (see below) |
| `tws_channel` | `IBCONTROLLER_TWS_CHANNEL`, `--tws-channel` | `"stable"`/`"latest"`; narrows auto-detection to one update channel | `"stable"` |
| `tws_path` | `IBCONTROLLER_TWS_PATH`, `--tws-path` | Override TWS/gateway install-path inference | `None` |
| `tws_settings_path` | `IBCONTROLLER_TWS_SETTINGS_PATH`, `--tws-settings-path` | TWS/Gateway settings dir (per instance) | `None` |
| `settings_file` | `IBCONTROLLER_SETTINGS_FILE` | Extra `ibkr_settings.toml`, merged on top of the built-in settings -- see "Declarative configuration" below | `None` |
| `trace_enabled` | `IBCONTROLLER_TRACE_ENABLED` | Enable verbose raw wire trace (`cmd-{instance}.jsonl`/`events-{instance}.jsonl`) | `false` |
| `log_dir` | `IBCONTROLLER_LOG_DIR` | Where ibcontroller's own log file lives; always resolved at startup | platform default |
| `log_level` | `IBCONTROLLER_LOG_LEVEL` | `debug`/`info`/`warning`/`error` | `info` |
| `log_sink` | `IBCONTROLLER_LOG_SINK` | `"std"` (console only) or `"file"` (only, under `log_dir`) — exclusive, not both. Covers `ibcontroller-{instance}.log` and `gateway-{instance}.log` only; the Java agent log and the wire trace are always file, unaffected — see "Logging" below | `"std"` |
| `diagnostic_scope` | `IBCONTROLLER_DIAGNOSTIC_SCOPE` | `"known"`/`"unknown"`/`"all"` — which windows get a structure dump logged (see `docs/Configuration.md`'s "Diagnostics") | `"known"` |
| `diagnostic_when` | `IBCONTROLLER_DIAGNOSTIC_WHEN` | `"open"`/`"openclose"`/`"never"` — when to log a structure dump | `"never"` |
| (env only) | `IBCONTROLLER_APP_DIR`, `--app-dir` | Override file locations (config/log/run) for container mode; `--app-dir`/`init`/`run` set the env var for the invocation, same effect |

TWS/ibgateway settings

| TOML key | Env var or CLI flag | Description | Default |
| --- | --- | --- | --- |
| `trading_mode` | `IBCONTROLLER_TRADING_MODE`, `--trading-mode` | `"live"` or `"paper"`; which account to authenticate as | `"paper"` |
| `read_only_login` | `IBCONTROLLER_READ_ONLY_LOGIN` | TWS-only; **not yet wired to any behavior** (loaded, unused — see TODO.md) | `false` |
| `read_only_api` | `IBCONTROLLER_READ_ONLY_API` | `true`/`false` sets it; omit to leave unchanged. Applied to Gateway/TWS automatically -- see "Declarative configuration" below | `None` |
| `accept_incoming_connections` | `IBCONTROLLER_ACCEPT_INCOMING_CONNECTIONS` | `manual`/`accept`/`reject` | `"manual"` |
| `existing_session_action` | `IBCONTROLLER_EXISTING_SESSION_ACTION` | `manual`/`primary`/`primaryoverride`/`secondary` | `"manual"` |
| `login_dialog_display_timeout` | `IBCONTROLLER_LOGIN_DIALOG_DISPLAY_TIMEOUT` | Seconds to wait for the login dialog | `60.0` |
| `second_factor_authentication_timeout` | `IBCONTROLLER_SECOND_FACTOR_AUTHENTICATION_TIMEOUT` | IB's 2FA timeout in seconds | `180.0` |
| `relogin_after_2fa_timeout` | `IBCONTROLLER_RELOGIN_AFTER_2FA_TIMEOUT` | Restart login if 2FA times out | `false` |
| `second_factor_authentication_exit_interval` | `IBCONTROLLER_SECOND_FACTOR_AUTHENTICATION_EXIT_INTERVAL` | Bounds the post-2FA wait when relogin enabled | `60.0` |
| `auto_restart_time` | `IBCONTROLLER_AUTO_RESTART_TIME` | `"hh:mm AM/PM"` daily auto-restart time. Applied to Gateway/TWS automatically -- see "Declarative configuration" below | `None` |
| `auto_logoff_time` | `IBCONTROLLER_AUTO_LOGOFF_TIME` | `"hh:mm AM/PM"` daily auto-logoff time; same "Lock and Exit" radio-button pair as `auto_restart_time` -- if both are set, `auto_restart_time` wins | `None` |
| `cold_restart_time` | `IBCONTROLLER_COLD_RESTART_TIME` | TWS and Gateway. `"HH:MM"` 24-hour local time; every Sunday, ibcontroller closes the instance tidily and relaunches with a full fresh login, forcing IBKR's weekly Sunday 01:00 US/Eastern token-invalidation reauth -- not a GUI setting, see "Scheduled shutdown" below | `None` |
| `closedown_at` | `IBCONTROLLER_CLOSEDOWN_AT` | TWS and Gateway. `"HH:MM"` (daily) or `"<Weekday> HH:MM"` (weekly); closes the instance tidily at that time, no relaunch -- not a GUI setting, see "Scheduled shutdown" below | `None` |

#### `tws_version` auto-detection

When `tws_version` is unset, `launcher._detect_tws_version` scans `tws_path` for every
installed version (a directory with its own `jars/` subfolder) and picks the greatest
one — optionally narrowed to one update channel first, if `tws_channel` is set. A real
install can carry more than one version side by side, each on its own channel: e.g.
`IB Gateway 10.45` (channel `stable`) and `IB Gateway 10.50` (channel `latest`) installed
at once — `tws_channel = "stable"` picks `10.45`; leaving it unset picks `10.50`, the
greatest overall. The channel itself is read per-install from its own
`.install4j/i4jparams.conf`, not guessed from the version number.

TWS requests fall back to Gateway installs when `program = "tws"` and no TWS install matches — no
`Trader Workstation *` directory at all, or none on the requested `tws_channel` — the
Gateway installs (`IB Gateway *`) are used instead, launching the TWS front end
(`jclient.LoginFrame`) from the Gateway jars. `tws_channel` still applies to the
fallback pool strictly: a Gateway install on the *wrong* channel never satisfies a TWS
request (that raises a clear error instead, naming `tws_channel`). The reverse
(Gateway falling back to TWS installs) is deliberately not supported. Applies to a
pinned `tws_version` too — a pinned version absent from TWS installs resolves against
Gateway installs of the same version.

#### Scheduled shutdown: `cold_restart_time` / `closedown_at`

Unlike `read_only_api`/`auto_restart_time`/`auto_logoff_time` (real Global Configuration
GUI settings), these two aren't written to TWS/Gateway at all -- ibcontroller schedules
the action itself, ported from IBC's own `IbcTws.java` (the shared base class both
programs use, so both apply here too). If both are set, whichever occurs first wins; a
malformed value is logged and skipped rather than aborting the other one.

- `cold_restart_time` -- `"HH:MM"`, 24-hour, local time. Every **Sunday** at this time,
  ibcontroller closes the instance tidily and relaunches with a full fresh login forcing the weekly
 reauth IBKR requires around Sunday 01:00 US/Eastern token invalidation.
- `closedown_at` -- `"HH:MM"` (every day) or `"<Weekday> HH:MM"` (one day a week,
  `Weekday` a full English name, `Monday`..`Sunday`). Closes the instance tidily at that
  time, with no relaunch -- you're responsible for restarting it yourself.

Both use the local system clock, not
converted to US/Eastern, so pick a local time that lands after the Sunday 01:00
US/Eastern invalidation window if that's the intent.

## Declarative configuration

Beyond `ibcontroller.toml`, three things let you shape ibcontroller's behavior without
writing any Python: **`labels.json`** (window/button text, plus simple pop-up dismissal
rules) and **`ibkr_settings.toml`** (Global Configuration settings applied to Gateway/TWS
at startup). Each is covered below.

### `labels.json` — window/button text and pop-up dismissal

`{config_dir}/labels.json` (rename `labels.json.example` to activate) overrides one or
more fields of the bundled default, field by field — you only need to include what
you're changing, not the whole file. Use it when a TWS/Gateway release renames a button
or dialog title ibcontroller matches against.

The one part of `labels.json` that isn't just text is `dismiss_rules` — a list of
"if a window matches, click this button" entries, checked on every window that opens.
Each entry needs at least one of `match_title`/`match_text` (both, if given, must
match); `click` is the button's label.

```json
{
  "dismiss_rules": [
    {
      "name": "my_custom_popup",
      "match_title": "Some Notice",
      "click": "OK"
    }
  ]
}
```

`dismiss_rules` is a list, not a map — an override *replaces the whole list*, so if you
want to keep the bundled entries (the non-brokerage-account warning, the auto-restart
confirmation notice) alongside your own, copy them into your override file too rather
than starting from an empty list. Good for any pop-up that's always handled the same
simple way; a dialog needing real logic (parsing a value out of its text, retrying,
raising an error) isn't expressible this way and needs actual code.

### `ibkr_settings.toml` — Global Configuration settings

Global Configuration settings (the "Configure" / "Settings" dialog inside Gateway/TWS)
are applied once, right after login, from two layers combined into one list:

- **Built-in** — `read_only_api`, `auto_restart_time`, and `auto_logoff_time`, applied
  straight from `ibcontroller.toml`/the environment. Set `read_only_api = false` or
  `auto_restart_time = "04:00 AM"` in your config and it's applied on the next run —
  no separate file needed. `auto_restart_time`/`auto_logoff_time` share one
  radio-button pair; if you set both, `auto_restart_time` wins (the built-in file
  applies it last, matching IBC's own stated precedence).
- **Your own `ibkr_settings.toml`** — anything else: any other Global Configuration
  checkbox or text field, activated by pointing `[settings] file = "..."` (or
  `IBCONTROLLER_SETTINGS_FILE`) at a real file (rename `ibkr_settings.toml.example` to
  start from the bundled worked examples, e.g. the `api_precautions_*` toggles).

An entry looks like this:

```toml
[[settings]]
tree_path = "API/Precautions"           # Global Configuration's own tree, section by section
action = "toggle"                       # "toggle" | "type_text" | "type_text_near_label" | "auto_restart_time" | "auto_logoff_time"
label_ref = "api_precautions_bypass_bond_warning"  # a name from labels.json's settings.controls map
# label = "Some Checkbox Label"         # ...or a literal label, for anything not named there yet
value = true                            # a literal value...
# value_from_config = "read_only_api"   # ...or pulled from ibcontroller.toml/env at apply time
```

- `label_ref` looks the label up in `labels.json`'s `settings.controls` map (version-proofed
  against a future rename); `label` is a literal string for anything not worth naming there.
- `value` is a literal; `value_from_config` instead reads an `ibcontroller.toml` field by
  name at apply time — this is how the built-in entries stay driven purely by config. A
  value that resolves to nothing (unset `value`, or a `value_from_config` field left at its
  default `None`) skips that entry entirely — "leave unchanged," not "set to blank." This
  is the actual enable/disable mechanism for `Config`-backed settings (see the `### Settings`
  table above) — `value_from_config` only works for the fixed set of fields `settings.py`
  already knows about (`read_only_api`/`auto_restart_time`/`auto_logoff_time` today);
  `Config`'s own schema is closed, so pointing it at anything else logs a warning and never
  applies — use a literal `value` for anything not in that set.
- `type_text_near_label`, for a field with no accessible label of its own, also takes
  `field_index` (0-based position within the container below the matched label).

**Your file adds to the built-ins, and can also override one.** Every entry you declare
with a *different* `value_from_config` (or none at all, e.g. a literal-`value` entry) is
simply added to what gets applied. An entry with the *same* `value_from_config` as a
built-in *replaces* that built-in entry outright — the two are never both applied. Use
this if a future Gateway/TWS release moves `read_only_api` (or `auto_restart_time`) to a
different screen or renames its control: point your own entry at the new location, keep
`value_from_config = "read_only_api"`, and it takes over from the bundled one.

```toml
# Overrides the built-in read_only_api entry with a new location/label, still driven
# by the same ibcontroller.toml field:
[[settings]]
tree_path = "API/Settings/Read-Only"
action = "toggle"
label = "Read-Only API Access"
value_from_config = "read_only_api"
```

## Logging

ibcontroller writes all of its log/trace files into one shared log directory
(resolved by `app_dirs.py`, or overridden with `IBCONTROLLER_APP_DIR` for
container mode). Every file is named with the `{instance}` name, so two instances
(e.g. `paper` and `live`) can run in parallel in the same directory without ever
interleaving — matched by `tail -f` on the right filename, no subdirectory per
instance.

**`IBCONTROLLER_LOG_SINK`** (`"std"`/`"file"`, default `"std"`) controls where
ibcontroller's own log and TWS/Gateway's own stdout/stderr go — console
(`"std"`) or file (`"file"`), exclusive, not both. This is the setting that
matters for Docker: with the default `"std"`, both streams show up in
`docker logs` and no file is written inside the container. It does **not**
cover the Java agent log or the wire trace below — those two are always file,
regardless of `log_sink`, the same way `trace_enabled`'s files are always
file when it's on: the Java agent log is JVM-side diagnostic detail (not
something you'd want interleaved into `docker logs` at `debug`), and the wire
trace is deliberately verbose NDJSON meant to be tailed, not read as console
scrollback.

| File | Layer | Enabled by | Level |
| --- | --- | --- | --- |
| `ibcontroller-{instance}.log` | ibcontroller app control flow | `log_sink=file` (else console) | `log_level` (default `info`) |
| `gateway-{instance}.log` | TWS/Gateway own stdout/stderr | `log_sink=file` (else console) | TWS/Gateway's own |
| `ibcontroller-java-agent-{instance}.log` | Java agent (in-process JUL) | always file, not affected by `log_sink` | `log_level` (see mapping below) |
| `cmd-{instance}.jsonl` | raw wire: commands sent + results, NDJSON | `trace_enabled`; always file, not affected by `log_sink` | always (DEBUG emit, gated separately) |
| `events-{instance}.jsonl` | raw wire: every event message received, NDJSON | `trace_enabled`; always file, not affected by `log_sink` | always (DEBUG emit, gated separately) |

### Log levels

`IBCONTROLLER_LOG_LEVEL` (`debug`/`info`/`warning`/`error`, default `info`) controls
both the Python app log and (via `-Dibcontroller.log.level`) the Java agent log:

| `IBCONTROLLER_LOG_LEVEL` | Python `logging` | Java JUL |
| --- | --- | --- |
| `debug` | `DEBUG` | `FINE` |
| `info` | `INFO` | `INFO` |
| `warning` | `WARNING` | `WARNING` |
| `error` | `ERROR` | `SEVERE` |

The default `info` is quiet — it records the *actions* taken and problems found,
not the raw event stream. `debug` additionally logs every event message that
arrives on the Python side (below).

`trace_enabled` is a separate, independent toggle: it turns on the structured
raw-wire files and is not coupled to `log_level` — you can trace at `info`, or
read the human-readable arrival lines at `debug` without writing the NDJSON files,
or both.

### What gets logged, per stream

- **`ibcontroller-{instance}.log`** — only written when `log_sink=file`; with
  the default `log_sink=std` this same content goes to the console instead, no
  file is created. The app's own control flow: launching an
  instance, each login state transition (credentials submitted, 2FA in progress, login
  completed/skipped on restart), declarative settings applied (and which entries were
  skipped), and every recogniser action (e.g. `dismissed non-brokerage account
  warning`). Credential *values* are never logged: `Config.userid`/`password` are
  `typed_settings.types.Secret`-wrapped, and a `Secret`'s `__str__`/`__repr__` both
  mask the real value (`*******`), so even a careless `logger.info("login as %s",
  password)` stays safe. At `debug` this same file gets one line per arriving
  event message (IBC's always-on
  `logWindow` posture, mapped to our DEBUG level here) — e.g. `event: window_opened
  class=ibgateway.ax title='IBKR Gateway' seq=1`. Event content is window metadata
  only, never field values.
- **`gateway-{instance}.log`** — same `log_sink=file`/`std` split as above; a plain
  drain of TWS/Gateway's own stdout/stderr, captured for the process's whole life
  either way. Used to diagnose issues *inside* Gateway itself (JWPRINT prints, JVM
  warnings, the occasional `InaccessibleObjectException` and similar). Level/content
  are entirely TWS/Gateway's own; ibcontroller only pipes it to a file or the console.
- **`ibcontroller-java-agent-{instance}.log`** — **always a file, regardless of
  `log_sink`** — the Java agent's `java.util.logging` output, from the JVM that
  actually hosts TWS/Gateway, never routed to the console. At `debug` this is the
  noisiest stream by far (every processed command, logged from inside the JVM
  itself); console output at that level would drown out ibcontroller's own log
  lines, so it stays file-only the same way the wire trace does. It logs connection
  accepts/disconnects, each processed command's name + target/label/path/window_id
  (**never a value** — `set_text`/`set_checkbox` values and therefore credentials can
  never land here, mirroring Python's `Secret` redaction), window register/unregister,
  and an event-queue overflow warning. Its level follows `log_level` via the table
  above — note `debug` maps to JUL `FINE`.
- **`cmd-{instance}.jsonl` / `events-{instance}.jsonl`** — the raw wire protocol,
  one JSON object per line, no formatter prefix (pure NDJSON), primarily for
  debugging what was actually sent and received at the socket boundary.
  `cmd-{instance}.jsonl` records each command as sent, then its result or error,
  correlated by a `seq`. `events-{instance}.jsonl` mirrors **every** event message the
  agent pushes, in order. Files are truncated at startup (per-session only) and are
  deliberately a separate, opt-in stream — they are verbose by design and not meant
  for steady-state operation.

### Ordering / correctness notes

- File writes happen on dedicated listener threads (Python side) and a background
  writer thread (Java side); a slow log volume never blocks the control loop or the
  AWT event dispatch.
- All five files carry the instance name and share one flat directory, so two
  instances (e.g. `paper` and `live`) can run in parallel and each tail its own files
  without interleaving, even if copied or globbed out of their directory.

## License

```text

   Copyright 2023 gnzsnz

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
```
