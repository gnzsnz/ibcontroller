# IBController Configuration

`ibcontroller` reads a single TOML file, `ibcontroller.toml`, found in the config
directory. Every key is optional except the credentials, which are
environment-variable-only (see below).

## File layout

One table per concern:

| Table | Holds | Environment |
| ----- | ----- | ----------- |
| `[ibcontroller]` | ibcontroller's own settings, every field documented below | `IBC_<FIELD>` |
| `[ibkr]` | values for IBKR settings and dialogs, by name | `IBKR_<NAME>` |
| `[[setting]]`, `[[dialog]]` | custom setting and dialog entries | none |

The file is rejected at startup if it has a top-level key outside a table (a file
from before the tables existed) or any table other than the four above.
`IBKR_<NAME>` also accepts a `_FILE` variant, an empty value counts as missing, and
the startup environment dump lists these variables with the same masking as `IBC_*`.

### Dialogs

ibcontroller handles the pop-ups listed in its bundled catalog (`catalog.toml`).
Each has a name and modes; a mode is what ibcontroller does to the window, and a
mode with no steps leaves the dialog to the user. `[ibkr]` picks the mode by the
dialog's name, for example `blind_trading = "on"` or `IBKR_BLIND_TRADING=on`.

- A boolean means `on` or `off`. In the environment `true`, `false`, `yes` and
  `no` (any case) are booleans. A string in the file is always a mode name.
- Mode names ignore case. An unknown mode, a boolean on a dialog without `on` and
  `off` modes, or any other type stops startup with an error naming the dialog.
- Without a value the dialog runs in its default mode. Startup logs one line per
  dialog with its mode and where the mode came from (`default`, `file`, `env`).
- `[[dialog]]` adds a dialog the catalog lacks, with `name`, `default`, `match` and
  `[dialog.modes]`, in the same shape as the catalog. A custom entry is checked
  before catalog entries, and cannot reuse a catalog name.
- Login outcome and exit progress windows are always handled first and cannot be
  overridden.
- `[ibkr]` keys that name no dialog are read by the settings (below). A key that names
  neither a setting nor a dialog is logged as a warning and ignored; a name shared by a
  setting and a dialog stops startup.

Precedence, highest first:

1. Environment variables (`IBC_*`)
2. Config file
3. Built-in defaults

Schema is closed: any key in `[ibcontroller]` not declared below is rejected at
startup with a clean error, never silently ignored.

Every config-field also has an environment-variable spelling:
`IBC_` + the field name upper-cased and in snake-case, e.g.
`accept_incoming_connections` -> `IBC_ACCEPT_INCOMING_CONNECTIONS`.
Environment variables override the config file.

Credentials are never accepted from the config file -- `userid`/`password` in the
file are rejected at startup. Only the environment variables work, and each also
accepts a `_FILE`-suffixed variant that reads the value from a file instead
(Docker/Compose secrets): `IBC_USERID` / `IBC_PASSWORD`,
`IBC_USERID_FILE` / `IBC_PASSWORD_FILE`.

The startup `IBC_*` environment dump masks any variable whose name contains
`PASSWORD`, `USERID`, `SECRET`, `TOKEN`, `TOTP` or `KEY` (e.g.
`IBC_PASSWORD_PAPER`), except `_FILE` variants, which only hold a path.

## IBController specific settings

Settings that drive the behaviour of `ibcontroller` itself.

| TOML key | Env var | Description | Default |
| --- | --- | --- | --- |
| `instance` | `IBC_INSTANCE` | Instance name; separate log/trace files per instance | `"{program}-{trading_mode}"` (e.g. `"gateway-paper"`) |
| `program` | `IBC_PROGRAM` | `"gateway"` or `"tws"` (any letter case); anything else is a config error | `"gateway"` |
| `tws_version` | `IBC_TWS_VERSION` | Installed TWS/Gateway version, e.g. `"10.50"`. Omit to auto-detect from the real install (greatest version wins, narrowed by `tws_channel`) | `None` (auto-detected) |
| `tws_channel` | `IBC_TWS_CHANNEL` | `"stable"`/`"latest"`; narrows auto-detection to installs carrying this update channel. Ignored when `tws_version` is set explicitly | `"stable"` |
| `tws_path` | `IBC_TWS_PATH` | Override the install-path inference | `None` |
| `tws_settings_path` | `IBC_TWS_SETTINGS_PATH` | Where TWS/Gateway stores its own settings (not the install dir); per instance | `None` (per-instance default) |
| (rejected) | `IBC_USERID` | Login user id; environment-variable-only, never from the config file. Also accepts `IBC_USERID_FILE` | (required) |
| (rejected) | `IBC_PASSWORD` | Login password; environment-variable-only, never from the config file. Also accepts `IBC_PASSWORD_FILE` | (required) |
| `trace_enabled` | `IBC_TRACE_ENABLED` | Verbose raw wire trace (`cmd-{instance}.jsonl` / `events-{instance}.jsonl` next to the log file) | `false` |
| (resolved) | `IBC_LOG_DIR` | Where ibcontroller's own log file lives; always resolved at startup | platform default |
| `log_level` | `IBC_LOG_LEVEL` | Logging level for ibcontroller's own log: `debug`/`info`/`warning`/`error` | `info` |
| `log_sink` | `IBC_LOG_SINK` | `"std"` (console only) or `"file"` (only `ibcontroller-{instance}.log`/`gateway-{instance}.log`, under `log_dir`) -- exclusive, not both. Also covers Gateway/TWS's own console output, not just ibcontroller's own log. Doesn't affect the wire trace above, which stays file-only regardless | `"std"` |
| `java_heap_size` | `IBC_JAVA_HEAP_SIZE` | JVM heap for TWS/Gateway at launch, e.g. `"1024m"`/`"4g"`. Overrides the `-Xmx` line in the installed `.vmoptions` file; bare size, no `-Xmx` prefix | `None` (unchanged) |
| `diagnostic_scope` | `IBC_DIAGNOSTIC_SCOPE` | Which windows get a structure dump logged: `"known"` (recognised by a built-in/declarative recogniser), `"unknown"`, or `"all"` -- see "Diagnostics" below | `"known"` |
| `diagnostic_when` | `IBC_DIAGNOSTIC_WHEN` | When to log a structure dump: `"open"`, `"openclose"`, or `"never"` (off) -- see "Diagnostics" below | `"never"` |
| `diagnostic_settings_sweep` | `IBC_DIAGNOSTIC_SETTINGS_SWEEP` | Log a dump of every Global Configuration tree node in `labels.settings.sweep_paths` -- see "Diagnostics" below | `false` |

## TWS/ibgateway specific settings

Settings related to TWS/ibgateway.

| TOML key | Env var | Description | Default |
| --- | --- | --- | --- |
| `trading_mode` | `IBC_TRADING_MODE` | `"live"` or `"paper"`; which account to authenticate as | `"paper"` |
| `read_only_login` | `IBC_READ_ONLY_LOGIN` | TWS only (Gateway logs a warning and logs in normally). Clicks "Enter Read Only" on the 2FA frame instead of waiting for the push; no effect without 2FA (paper) | `false` |
| `accept_incoming_connections` | `IBC_ACCEPT_INCOMING_CONNECTIONS` | How to handle incoming API connection dialogs -- see below | `"manual"` |
| `existing_session_action` | `IBC_EXISTING_SESSION_ACTION` | What to do when an existing session is detected -- see below | `"manual"` |
| `time_zone` | `IBC_TIME_ZONE` | IANA zone name, e.g. `"Europe/Zurich"`, written to `jts.ini`'s `TimeZone` -- see below | auto-detected |

### Accept Incoming Connection

**key:** `accept_incoming_connections`
**Environment variable:** `IBC_ACCEPT_INCOMING_CONNECTIONS`

If set to 'accept', ibcontroller automatically accepts incoming API connection
dialogs. If set to 'reject', ibcontroller automatically rejects them. If set to
'manual', the user must decide whether to accept or reject each incoming API
connection dialog. The default is 'manual'.

NB: it is recommended to set this to 'reject', and to explicitly configure which
IP addresses can connect to the API in TWS's API configuration page, as this is
much more secure (in this case, no incoming API connection dialogs will occur for
those IP addresses).

### Existing Session Detected Action

**key:** `existing_session_action`
**Environment variable:** `IBC_EXISTING_SESSION_ACTION`

When a user logs on to an IBKR account for trading purposes by any means, the
IBKR account server checks to see whether the account is already logged in
elsewhere. If so, a dialog is displayed to both users that enables them to
determine what happens next. This setting instructs ibcontroller how to proceed
when it displays this dialog:

- If the new TWS session is set to 'secondary', the existing session continues
  and the new session terminates. Thus a secondary TWS session can never
  override any other session.

- If the existing TWS session is set to 'primary', the existing session
  continues and the new session terminates (even if the new session is also
  set to primary). Thus a primary TWS session can never be overridden by
  any new session.

- If both the existing and the new TWS sessions are set to 'primaryoverride',
  the existing session terminates and the new session proceeds.

- If the existing TWS session is set to 'manual', the user must handle the
  dialog.

The difference between 'primary' and 'primaryoverride' is that a
'primaryoverride' session can be overridden by a new 'primary' session, but a
'primary' session cannot be overridden by any other session. When set to
'primary', if another TWS session is started and manually told to end the
'primary' session, the 'primary' session is automatically reconnected. The
default is 'manual'.

What ibcontroller does at each dialog, by value and by the role of this
session. The *new* session is the one still logging in; the *existing* session
is the one already logged in when the other one connects.

| Dialog | Role | `manual` | `secondary` | `primary` | `primaryoverride` |
| --- | --- | --- | --- | --- | --- |
| Existing session detected | new | user decides | cancel, session ends | first time: continue; if the dialog returns: cancel, session ends | same as `primary` |
| Existing session detected | existing | user decides | cancel, session ends | continue | cancel, session ends |
| Re-login is required | new | user decides | cancel, session ends | click Re-login | click Re-login |
| Re-login is required | existing | user decides | cancel, session ends | click Re-login | click Re-login |
| Trading Login Handoff (2FA accounts) | new | user decides | invalid state, session ends | click Disconnect Other Session | invalid state, session ends |

Notes:
- "Re-login is required" is the dialog an IBKR session shows after the other
  session took over. Clicking Re-login makes the session reconnect, which in
  turn shows the other session its own Re-login dialog. The exchange stops when
  one side ends. A `secondary` session never reconnects, because that would
  displace the other session.
- "Session ends" means ibcontroller cancels the dialog and exits with code `0`
  (`stopped: SESSION_CEDED`), without relaunching: relaunching would take the
  session back. Cancelling a dialog by hand is a manual decision; ibcontroller
  does not react to it.
- "Invalid state" is IBC's own rule: the Trading Login Handoff only appears
  after this session chose to continue, which `secondary` and `primaryoverride`
  never do. ibcontroller exits with code `76` without relaunching.

### Time Zone

**key:** `time_zone`
**Environment variable:** `IBC_TIME_ZONE`

IANA zone name, e.g. `"Europe/Zurich"`, written to `jts.ini`'s `TimeZone` key
(under `[Logon]`) before every launch. No setup needed in the common case:
auto-detected from the container's own `TZ` environment variable first, then
from `/etc/localtime`'s zoneinfo symlink target; set `IBC_TIME_ZONE`
explicitly only when the desired TWS/Gateway session time zone should differ
from the host/container's own. Never overwrites a `TimeZone` line already
present in `jts.ini` -- a value found there was set deliberately, either by
you or by an earlier run of ibcontroller itself. Leaves the key untouched if
nothing resolves.

### Cold Restart Time

**key:** `cold_restart_time`
**Environment variable:** `IBC_COLD_RESTART_TIME`

TWS and Gateway alike (ported from IBC's `IbcTws.java`, the shared base class
both programs use). `"HH:MM"`, 24-hour, local system time. Unlike the `[ibkr]`
settings, this is not a Global Configuration GUI setting -- it's a self-scheduled
action. Every Sunday at this time, ibcontroller closes the instance tidily and
relaunches it with a full fresh login (no restart hash, deliberately not a
silent relogin, and via ibcontroller's own `launch_instance`, not IBC's
native-launcher `File > Restart`), forcing the weekly full reauth IBKR
requires around Sunday 01:00 US/Eastern token invalidation. Omit this key (the
default) to disable it. If `closedown_at` is also set, whichever occurs first
wins.

### Closedown At

**key:** `closedown_at`
**Environment variable:** `IBC_CLOSEDOWN_AT`

TWS and Gateway alike, same reasoning as `cold_restart_time` above. `"HH:MM"`
(every day) or `"<Weekday> HH:MM"` (one day a week -- `Weekday` a full English
name, `Monday`..`Sunday`), 24-hour, local system time. Like `cold_restart_time`
above, this is a self-scheduled action, not a GUI setting: at this time,
ibcontroller closes the instance tidily with no relaunch -- you're responsible
for restarting it yourself. Omit this key (the default) to disable it. If
`cold_restart_time` is also set, whichever occurs first wins.

## Login / 2FA timeouts

ibcontroller's own wait policy during login -- these are not written to
TWS/Gateway itself.

| TOML key | Env var | Description | Default |
| --- | --- | --- | --- |
| `login_dialog_display_timeout` | `IBC_LOGIN_DIALOG_DISPLAY_TIMEOUT` | Seconds to wait for the login dialog to appear | `60.0` |
| `mfa_timeout` | `IBC_MFA_TIMEOUT` | IB's own limit in seconds for an unanswered 2FA push (IB closed an unanswered dialog after 200 s and 352 s on Gateway 10.51). A 2FA dialog closing at or after it counts as timed out | `180.0` |
| `relogin_after_mfa_timeout` | `IBC_RELOGIN_AFTER_MFA_TIMEOUT` | `true`: log in again 5 s after a timed-out push, and enforce `mfa_exit_interval` after an approved one. `false`: keep waiting for the push | `false` |
| `mfa_exit_interval` | `IBC_MFA_EXIT_INTERVAL` | With relogin on: seconds to wait for login to complete after the push is approved, then `mfa_timeout_action` applies | `60.0` |
| `mfa_timeout_action` | `IBC_MFA_TIMEOUT_ACTION` | What to do when `mfa_exit_interval` expires after an approved push: `exit` (exit 75) or `restart` (relaunch with a fresh login) | `exit` |
| `restart_cap` | `IBC_RESTART_CAP` | Max consecutive relaunches that never reach READY (login frame timeout, transient login failure, TWS logout, crash during login). A lap that reaches READY resets the count; past the cap the run stops with exit 70 | `3` |

Login has no overall deadline, as in IBC: with no network, an unanswered push and relogin off, or an unrecognised dialog blocking login, `ibcontroller run` keeps waiting. Monitor it from outside.

## Diagnostics

Off by default (`diagnostic_when="never"`). When enabled, logs a component
structure dump (`AgentClient.dump`, the same primitive `settings.py`/
`recognisers.py` use to locate controls) for each matching window event --
a built-in replacement for poking the agent socket directly with
`socat`/`nc`. `diagnostic_scope` picks which windows qualify:
`"known"` (recognised by a built-in or declarative recogniser),
`"unknown"` (not recognised by any), or `"all"`. `diagnostic_when` picks
which events trigger a dump: `"open"`, `"openclose"`, or `"never"`.
Dumps land in `ibcontroller-{instance}.log` at `INFO` level.

`diagnostic_settings_sweep` (default `false`) is independent of those two:
while the Global Configuration dialog is open for declarative settings, it
selects each tree node listed in `labels.json` `settings.sweep_paths` and logs
that panel's component dump (`settings sweep:` lines, same log). Read-only.
A node the running TWS/Gateway build lacks is logged as `not found`. The tree
cannot be enumerated, so extend `sweep_paths` to probe more nodes. Use it to
learn a panel's layout before writing a declarative entry, and after a
TWS/Gateway upgrade.

## Declarative configuration

Two kinds of behaviour are described in data files rather than code: Global Configuration
**settings** applied to TWS/Gateway at startup, and **dialogs** (pop-ups) that ibcontroller
recognises and answers. Each has its own section below.

### Settings

A setting is an entry of the bundled catalog (`data/catalog.toml`) with a name, the
`tree_path` of its Global Configuration section, an `action` and the control's label. The
entry carries no value: `[ibkr]` gives it one by name (`read_only_api = true`, or
`IBKR_READ_ONLY_API=true` in the environment, which wins over the file). A setting with no
value is left as it is in Gateway/TWS (leave-unchanged, as IBC's empty value).

| Name | Value | Notes |
| --- | --- | --- |
| `auto_logoff_time` | `"hh:mm AM/PM"`, e.g. `"08:00 AM"` | Daily logoff, under Lock and Exit. |
| `auto_restart_time` | `"hh:mm AM/PM"`, e.g. `"08:00 AM"` | Daily restart, under Lock and Exit. Shares one radio pair with `auto_logoff_time`: if both have a value, `auto_restart_time` wins (applied last, as IBC specifies). |
| `read_only_api` | `true`/`false` | API/Settings "Read-Only API". |
| `socket_port` | number or text | API/Settings "Socket port". IBKR defaults: 7496 TWS live, 7497 TWS paper, 4001 Gateway live, 4002 Gateway paper. On TWS, changing it opens a confirmation dialog after OK, see `socket_port_confirmation`. |
| `master_api_client_id` | number or text | API/Settings "Master API client ID". |
| `send_market_data_in_lots_us_stocks` | `true`/`false` | API/Settings, lots for US stocks. Do not set `true` yet: it opens a confirmation window that is not handled and blocks the settings step. |
| `api_precautions_bypass_order_precautions` | `true`/`false` | API/Precautions: bypass the order precautions for API orders. |
| `api_precautions_bypass_bond_warning` | `true`/`false` | API/Precautions: bypass the bond warning for API orders. |
| `api_precautions_bypass_negative_yield_to_worst` | `true`/`false` | API/Precautions: bypass the negative yield to worst confirmation for API orders. |
| `api_precautions_bypass_called_bond_warning` | `true`/`false` | API/Precautions: bypass the called bond warning for API orders. |
| `api_precautions_bypass_same_action_pair_trade` | `true`/`false` | API/Precautions: bypass the "same action pair trade" warning for API orders. |
| `api_precautions_bypass_price_based_volatility` | `true`/`false` | API/Precautions: bypass the price-based volatility risk warning for API orders. |
| `api_precautions_bypass_redirect_order_warning` | `true`/`false` | API/Precautions: bypass the redirect order warning (stock orders) for API orders. |
| `api_precautions_bypass_no_overfill_protection` | `true`/`false` | API/Precautions: bypass the no overfill protection precaution for API orders. |
| `api_precautions_bypass_route_marketable_to_bbo` | `true`/`false` | API/Precautions: bypass the route marketable to BBO warning for API orders. Do not set `true` yet: it opens a confirmation window that is not handled and blocks the settings step. |

- Values are checked at startup; a wrong one stops the run (exit 78) with an error naming
  the setting. A `toggle` takes a boolean (in the environment `true`, `false`, `yes`, `no`
  in any case); a time takes `"hh:mm AM/PM"` with one space; a text setting takes a string or
  a number.
- Settings are applied in catalog order, after login, in one Global Configuration session.
  Each applied setting is logged with its value and source (`file` or `env`).
- `[[setting]]` adds a setting the catalog lacks: `name`, `tree_path`, `action` (`toggle`,
  `type_text`, `type_text_near_label`) and the control by `label` (literal) or `label_ref` (a
  name in `labels.json`'s `settings.controls`). `type_text_near_label` also takes
  `field_index` (0-based field below the label) for a field with no label of its own. The
  entry's value comes from `[ibkr]` like any other; a custom setting with no value
  is logged as a warning at startup and left untouched. A custom name cannot reuse a catalog name.
- **Moved:** `read_only_api`, `auto_restart_time` and `auto_logoff_time` are no longer
  `[ibcontroller]` keys, and `IBC_READ_ONLY_API`, `IBC_AUTO_RESTART_TIME` and
  `IBC_AUTO_LOGOFF_TIME` are no longer read (ignored silently). `settings_file`,
  `IBC_SETTINGS_FILE` and `ibkr_settings.toml` are gone.

When no setting has a value (the default configuration), the Global Configuration dialog is
not opened. If one entry fails (a missing control, an agent error), the others are still
applied and "OK" commits them. If the dialog closes under the program, the step ends without
clicking "OK". If the dialog is still open a few seconds after "OK", Gateway/TWS rejected a
value and nothing was committed: an error is logged with the text of the error window (for
example `The value must be between 1024 and 65535 in Socket port`), the window is left open and
startup goes on.

Only the TWS Mosaic layout is supported: the dialog is opened with
`File > Global Configuration...`. On the TWS Classic layout that menu item does not exist, no
setting is applied, and the attempt waits for `mfa_timeout` (default 180s) before giving up
and logging the failure. Gateway is not affected.

### Dialogs

Pop-ups that are always handled the same simple way are entries of the bundled dialog
catalog (`data/catalog.toml`). An entry names a dialog, says how to recognise it and what to
do in each mode. They are checked on every window that opens, after the login-outcome and
exit-progress dialogs.

| Entry | Default | Recognised by | Does |
| --- | --- | --- | --- |
| `non_brokerage_account` | on | text "This is not a brokerage account" | clicks "I understand and accept" |
| `auto_restart_confirmation` | on | text "trading platform restart automatically" | clicks "OK" |
| `password_expiry_warning` | off | title "Password Notice" | clicks "OK" |
| `blind_trading` | off | "Order Preview" with "blind trading", or the confirmation text | clicks "Override and Transmit", else "Yes" |
| `socket_port_confirmation` | off | text "apply the new socket port setting immediately" (TWS only, after the port changed) | `on` clicks "Yes" (port applies at once), `no` clicks "No" (applies at the next GUI restart) |

- Matching ignores case. Message text is compared with HTML tags removed and entities
  decoded, so a phrase can be copied as it shows on screen.
- Candidate buttons are tried in order and the first one present is clicked.
- An entry in a mode with no steps (`off`) leaves the dialog on screen for you.
- `password_expiry_warning` and `blind_trading` come from IBC's source and have not been
  checked against a live window.
- Entries are fixed to their default mode for now. Switching an entry and adding your own
  arrive with the `ibcontroller.toml` sections for declarative configuration.
- A malformed catalog is a startup error (exit 78) that names the file and the entry.
- `dismiss_rules` in `labels.json` is no longer read: it is logged as an unknown key and
  ignored.

**Order.** Login-outcome dialogs ("Login failed", "Unrecognized Username or Password", the
connection and too-many-attempts warnings) and the "Shutdown progress" / "Restart in
progress" windows are always handled first. A catalog entry that matches one of them never
runs, so a credential rejection still exits with code 77 and a restart is still recognised
as one. Catalog entries run before the other built-in handlers (existing session, re-login,
incoming API connections).
