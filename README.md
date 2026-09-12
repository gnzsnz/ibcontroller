# ibcontroller

An ibgateway/TWS automation tool.

Heavily inspired by [IBC](https://github.com/IbcAlpha/IBC) project, now archived.

## What it does?

- Finds ibgateway/TWS installation and JRE.
- Automates user/password entry, waits for MFA.
- Automates ibgatewa/TWS settings.
- Manages pop-ups and automatically accept.

It provides a "declarative" engine, so settings and pop-ups can be "declared" by configuration entries.

## How to use it

```bash
# 1. Build the Java agent jar and install the package (editable, for a source checkout)
make
uv sync

# 2. Scaffold a starter config directory (platformdirs default, or $IBCONTROLLER_APP_DIR)
uv run ibcontroller init

# 3. Export IBCONTROLLER_USERID / IBCONTROLLER_PASSWORD (see Credentials below) -- the
#    only settings actually required. Everything else in the printed ibcontroller.toml
#    is commented out and already safe to run as-is: trading_mode defaults to "paper",
#    and tws_version is auto-detected from whatever's installed under tws_path.

# 4. Run it -- Ctrl-C for a graceful shutdown
uv run ibcontroller run
```

`ibcontroller run` scaffolds the config directory automatically the first time
`ibcontroller.toml` is missing (same effect as running `init` first), so it can also be
run directly on a fresh install -- it will still fail with a clear error naming whichever
setting is actually missing (in practice, just the two credential env vars).

`ibcontroller version` prints the installed package version.

## Packaging

The Java agent jar is not built by `uv build`/`pip install` -- it ships via
`[tool.setuptools.package-data]` (same mechanism as `ibcontroller/data/*.json`), which
only picks up files already present in the source tree at build time. `make dist` makes
this explicit: it builds `ibcontroller/ibcontroller-agent.jar` first, then runs
`uv build`. Building the wheel any other way (plain `uv build`, `python -m build`) works
only if the jar has already been built into `ibcontroller/` by a prior `make`/`make run`.

## Configuration

`ibcontroller` is configured through a **flat** TOML file (no `[section]` headers) and/or
environment variables, with precedence `defaults < TOML file < .env < environment`. Environment
can be loaded from a `.env` file. Credentials are environment-variable-only and must
never appear in the config file.

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

### Settings

**Category** column says whether a field configures the real TWS/Gateway application itself
(directly, or via the declarative `settings.py` mechanism) versus ibcontroller's own
operational behavior (never written to TWS/Gateway). **Only the two credential env vars
below are actually mandatory** — everything else here has a safe default or is
auto-detected.

`None` on a `TWS/Gateway setting` row is a deliberate third state, not just "unset" —
matching IBC's own `config.ini` yes/no/unset convention: set it explicitly to apply a
value, or leave it `None` to leave TWS/Gateway's existing setting alone. This is the
actual enable/disable mechanism for these fields, not a side effect of the type
allowing `None`. `Config`'s schema is closed — an unknown key in `ibcontroller.toml`
fails at startup with a clean error (`ConfigError`), not silently ignored — so a new
`TWS/Gateway setting` field always needs a `config.py` code change first, it can't be
added by editing the TOML file alone.

| Config field | TOML key | Env var | Description | Default | Category |
| --- | --- | --- | --- | --- | --- |
| `instance` | `instance` | `IBCONTROLLER_INSTANCE` | Instance name; separate log/trace files per instance | `"{program}-{trading_mode}"` (e.g. `"gateway-paper"`) | ibcontroller config |
| `program` | `program` | `IBCONTROLLER_PROGRAM` | `"gateway"` or `"tws"` | `"gateway"` | ibcontroller config |
| `tws_version` | `tws_version` | `IBCONTROLLER_TWS_VERSION` | Installed TWS/Gateway version, e.g. `"10.50"` | `None` — auto-detected (see below) | ibcontroller config |
| `tws_channel` | `tws_channel` | `IBCONTROLLER_TWS_CHANNEL` | `"stable"`/`"latest"`; narrows auto-detection to one update channel | `"stable"` | ibcontroller config |
| `tws_path` | `tws_path` | `IBCONTROLLER_TWS_PATH` | Override install-path inference | `None` | ibcontroller config |
| `tws_settings_path` | `tws_settings_path` | `IBCONTROLLER_TWS_SETTINGS_PATH` | TWS/Gateway settings dir (per instance) | `None` | ibcontroller config |
| `settings_file` | `settings_file` | `IBCONTROLLER_SETTINGS_FILE` | Extra `ibkr_settings.toml`, merged on top of the built-in settings -- see "Declarative configuration" below | `None` | ibcontroller config |
| `trace_enabled` | `trace_enabled` | `IBCONTROLLER_TRACE_ENABLED` | Enable verbose raw wire trace (`cmd-{instance}.jsonl`/`events-{instance}.jsonl`) | `false` | ibcontroller config |
| `log_dir` | `log_dir` | `IBCONTROLLER_LOG_DIR` | Where ibcontroller's own log file lives; always resolved at startup | platform default | ibcontroller config |
| `log_level` | `log_level` | `IBCONTROLLER_LOG_LEVEL` | `debug`/`info`/`warning`/`error` | `info` | ibcontroller config |
| — | (env only) | `IBCONTROLLER_APP_DIR` | Override file locations (config/log/run) for container mode | (platform default) | ibcontroller config |

TWS/ibgateway settings

| Config field | TOML key | Env var | Description | Default | Category |
| --- | --- | --- | --- | --- | --- |
| `trading_mode` | `trading_mode` | `IBCONTROLLER_TRADING_MODE` | `"live"` or `"paper"`; which account to authenticate as | `"paper"` | TWS/Gateway setting |
| `read_only_login` | `read_only_login` | `IBCONTROLLER_READ_ONLY_LOGIN` | TWS-only; **not yet wired to any behavior** (loaded, unused — see TODO.md) | `false` | TWS/Gateway setting (unwired) |
| `read_only_api` | `read_only_api` | `IBCONTROLLER_READ_ONLY_API` | `true`/`false` sets it; omit to leave unchanged. Applied to Gateway/TWS automatically -- see "Declarative configuration" below | `None` | TWS/Gateway setting |
| `accept_incoming_connections` | `accept_incoming_connections` | `IBCONTROLLER_ACCEPT_INCOMING_CONNECTIONS` | `manual`/`accept`/`reject` | `"manual"` | TWS/Gateway setting |
| `existing_session_action` | `existing_session_action` | `IBCONTROLLER_EXISTING_SESSION_ACTION` | `manual`/`primary`/`primaryoverride`/`secondary` | `"manual"` | TWS/Gateway setting |
| `login_dialog_display_timeout` | `login_dialog_display_timeout` | `IBCONTROLLER_LOGIN_DIALOG_DISPLAY_TIMEOUT` | Seconds to wait for the login dialog | `60.0` | TWS/Gateway setting |
| `second_factor_authentication_timeout` | `second_factor_authentication_timeout` | `IBCONTROLLER_SECOND_FACTOR_AUTHENTICATION_TIMEOUT` | IB's 2FA timeout in seconds | `180.0` | TWS/Gateway setting |
| `relogin_after_2fa_timeout` | `relogin_after_2fa_timeout` | `IBCONTROLLER_RELOGIN_AFTER_2FA_TIMEOUT` | Restart login if 2FA times out | `false` | TWS/Gateway setting |
| `second_factor_authentication_exit_interval` | `second_factor_authentication_exit_interval` | `IBCONTROLLER_SECOND_FACTOR_AUTHENTICATION_EXIT_INTERVAL` | Bounds the post-2FA wait when relogin enabled | `60.0` | TWS/Gateway setting |
| `auto_restart_time` | `auto_restart_time` | `IBCONTROLLER_AUTO_RESTART_TIME` | `"hh:mm AM/PM"` daily auto-restart time. Applied to Gateway/TWS automatically -- see "Declarative configuration" below | `None` | TWS/Gateway setting |
| `auto_logoff_time` | `auto_logoff_time` | `IBCONTROLLER_AUTO_LOGOFF_TIME` | `"hh:mm AM/PM"` daily auto-logoff time; same "Lock and Exit" radio-button pair as `auto_restart_time` -- if both are set, `auto_restart_time` wins | `None` | TWS/Gateway setting |
| `cold_restart_time` | `cold_restart_time` | `IBCONTROLLER_COLD_RESTART_TIME` | TWS only. `"HH:MM"` 24-hour local time; every Sunday, ibcontroller closes TWS tidily and relaunches with a full fresh login, forcing IBKR's weekly Sunday 01:00 US/Eastern token-invalidation reauth -- not a GUI setting, see "Scheduled shutdown" below | `None` | ibcontroller scheduled action |
| `closedown_at` | `closedown_at` | `IBCONTROLLER_CLOSEDOWN_AT` | TWS only. `"HH:MM"` (daily) or `"<Weekday> HH:MM"` (weekly); closes TWS tidily at that time, no relaunch -- not a GUI setting, see "Scheduled shutdown" below | `None` | ibcontroller scheduled action |

#### `tws_version` auto-detection

When `tws_version` is unset, `launcher._detect_tws_version` scans `tws_path` for every
installed version (a directory with its own `jars/` subfolder) and picks the greatest
one — optionally narrowed to one update channel first, if `tws_channel` is set. A real
install can carry more than one version side by side, each on its own channel: e.g.
`IB Gateway 10.45` (channel `stable`) and `IB Gateway 10.50` (channel `latest`) installed
at once — `tws_channel = "stable"` picks `10.45`; leaving it unset picks `10.50`, the
greatest overall. The channel itself is read per-install from its own
`.install4j/i4jparams.conf`, not guessed from the version number.

#### Scheduled shutdown (TWS only): `cold_restart_time` / `closedown_at`

Unlike `read_only_api`/`auto_restart_time`/`auto_logoff_time` (real Global Configuration
GUI settings), these two aren't written to TWS at all -- ibcontroller schedules the
action itself, ported from IBC's own `IbcTws.java`. Both are TWS-only (`IbcGateway.java`
has no equivalent). If both are set, whichever occurs first wins; a malformed value is
logged and skipped rather than aborting the other one.

- `cold_restart_time` -- `"HH:MM"`, 24-hour, local time. Every **Sunday** at this time,
  ibcontroller closes TWS tidily and relaunches with a full fresh login (no restart
  hash, deliberately not a silent relogin) -- forcing the weekly reauth IBKR requires
  around Sunday 01:00 US/Eastern token invalidation.
- `closedown_at` -- `"HH:MM"` (every day) or `"<Weekday> HH:MM"` (one day a week,
  `Weekday` a full English name, `Monday`..`Sunday`). Closes TWS tidily at that time,
  with no relaunch -- you're responsible for restarting it yourself.

Both use the local system clock, matching IBC's own `Calendar.getInstance()` -- not
converted to US/Eastern, so pick a local time that lands after the Sunday 01:00
US/Eastern invalidation window if that's the intent.

### Credentials (env only, never in the config file)

A single pair is used regardless of `trading_mode`.

| Env var | Description |
| --- | --- |
| `IBCONTROLLER_USERID` | Account user id |
| `IBCONTROLLER_PASSWORD` | Account password |

Each also accepts a `_FILE`-suffixed variant (`IBCONTROLLER_USERID_FILE`,
`IBCONTROLLER_PASSWORD_FILE`) that reads the value from a file instead —
Docker/Compose secrets, so a value never has to sit in the process environment.

## Declarative configuration

Beyond `ibcontroller.toml`, three things let you shape ibcontroller's behavior without
writing any Python: **`labels.json`** (window/button text, plus simple pop-up dismissal
rules), **`ibkr_settings.toml`** (Global Configuration settings applied to Gateway/TWS
at startup), and `ibcontroller.toml`'s `existing_session_action` (how to react when a
second login attempt collides with one already running). Each is covered below.

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

### `existing_session_action` — reacting to a collision with another running session

Set in `ibcontroller.toml` (no separate file): `manual` (leave the dialog for a human),
`primary`/`primaryoverride`/`secondary` (IBC's own four-way policy for which session
wins when two logins collide). See the field table above for the env var.

## Logging

ibcontroller writes all of its log/trace files into one shared log directory
(resolved by `app_dirs.py`, or overridden with `IBCONTROLLER_APP_DIR` for
container mode). Every file is named with the `{instance}` name, so two instances
(e.g. `paper` and `live`) can run in parallel in the same directory without ever
interleaving — matched by `tail -f` on the right filename, no subdirectory per
instance.

| File | Layer | Enabled by | Level |
| --- | --- | --- | --- |
| `ibcontroller-{instance}.log` | ibcontroller app control flow | always | `log_level` (default `info`) |
| `gateway-{instance}.log` | TWS/Gateway own stdout/stderr | always | TWS/Gateway's own |
| `ibcontroller-java-agent-{instance}.log` | Java agent (in-process JUL) | always | `log_level` (see mapping below) |
| `cmd-{instance}.jsonl` | raw wire: commands sent + results, NDJSON | `trace_enabled` | always (DEBUG emit, gated separately) |
| `events-{instance}.jsonl` | raw wire: every event message received, NDJSON | `trace_enabled` | always (DEBUG emit, gated separately) |

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

- **`ibcontroller-{instance}.log`** — the app's own control flow: launching an
  instance, each login state transition (credentials submitted, 2FA in progress, login
  completed/skipped on restart), declarative settings applied (and which entries were
  skipped), and every recogniser action (e.g. `dismissed non-brokerage account
  warning`). Credential *values* are never logged: `Config.userid`/`password` are
  `typed_settings.types.Secret`-wrapped, and a `Secret`'s `__str__`/`__repr__` both
  mask the real value (`*******`), so even a careless `logger.info("login as %s",
  password)` stays safe. At `debug`
  this same file gets one line per arriving event message (IBC's always-on
  `logWindow` posture, mapped to our DEBUG level here) — e.g. `event: window_opened
  class=ibgateway.ax title='IBKR Gateway' seq=1`. Event content is window metadata
  only, never field values.
- **`gateway-{instance}.log`** — a plain drain of TWS/Gateway's own stdout/stderr,
  captured for the process's whole life. Used to diagnose issues *inside* Gateway
  itself (JWPRINT prints, JVM warnings, the occasional `InaccessibleObjectException`
  and similar). Level/content are entirely TWS/Gateway's own; ibcontroller only pipes
  it to disk so a run can be audited after the fact.
- **`ibcontroller-java-agent-{instance}.log`** — the Java agent's `java.util.logging`
  output, from the JVM that actually hosts TWS/Gateway. It logs connection
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
