# IBController Configuration

`ibcontroller` reads a single flat TOML file, `ibcontroller.toml` (no `[section]`
headers), found in the config directory. Every key is optional except the
credentials, which are environment-variable-only (see below).

Precedence, highest first:

1. Environment variables (`IBC_*`)
2. Config file
3. Built-in defaults

Schema is closed: any key not declared below is rejected at startup with a clean
error, never silently ignored.

Every config-field also has an environment-variable spelling:
`IBC_` + the field name upper-cased and in snake-case, e.g.
`accept_incoming_connections` -> `IBC_ACCEPT_INCOMING_CONNECTIONS`.
Environment variables override the config file.

Credentials are never accepted from the config file -- `userid`/`password` in the
file are rejected at startup. Only the environment variables work, and each also
accepts a `_FILE`-suffixed variant that reads the value from a file instead
(Docker/Compose secrets): `IBC_USERID` / `IBC_PASSWORD`,
`IBC_USERID_FILE` / `IBC_PASSWORD_FILE`.

## IBController specific settings

Settings that drive the behaviour of `ibcontroller` itself.

| TOML key | Env var | Description | Default |
| --- | --- | --- | --- |
| `instance` | `IBC_INSTANCE` | Instance name; separate log/trace files per instance | `"{program}-{trading_mode}"` (e.g. `"gateway-paper"`) |
| `program` | `IBC_PROGRAM` | `"gateway"` or `"tws"` | `"gateway"` |
| `tws_version` | `IBC_TWS_VERSION` | Installed TWS/Gateway version, e.g. `"10.50"`. Omit to auto-detect from the real install (greatest version wins, narrowed by `tws_channel`) | `None` (auto-detected) |
| `tws_channel` | `IBC_TWS_CHANNEL` | `"stable"`/`"latest"`; narrows auto-detection to installs carrying this update channel. Ignored when `tws_version` is set explicitly | `"stable"` |
| `tws_path` | `IBC_TWS_PATH` | Override the install-path inference | `None` |
| `tws_settings_path` | `IBC_TWS_SETTINGS_PATH` | Where TWS/Gateway stores its own settings (not the install dir); per instance | `None` (per-instance default) |
| `settings_file` | `IBC_SETTINGS_FILE` | Extra declarative settings file, merged on top of the built-in settings -- see "Declarative settings" below | `None` |
| (rejected) | `IBC_USERID` | Login user id; environment-variable-only, never from the config file. Also accepts `IBC_USERID_FILE` | (required) |
| (rejected) | `IBC_PASSWORD` | Login password; environment-variable-only, never from the config file. Also accepts `IBC_PASSWORD_FILE` | (required) |
| `trace_enabled` | `IBC_TRACE_ENABLED` | Verbose raw wire trace (`cmd-{instance}.jsonl` / `events-{instance}.jsonl` next to the log file) | `false` |
| (resolved) | `IBC_LOG_DIR` | Where ibcontroller's own log file lives; always resolved at startup | platform default |
| `log_level` | `IBC_LOG_LEVEL` | Logging level for ibcontroller's own log: `debug`/`info`/`warning`/`error` | `info` |
| `log_sink` | `IBC_LOG_SINK` | `"std"` (console only) or `"file"` (only `ibcontroller-{instance}.log`/`gateway-{instance}.log`, under `log_dir`) -- exclusive, not both. Also covers Gateway/TWS's own console output, not just ibcontroller's own log. Doesn't affect the wire trace above, which stays file-only regardless | `"std"` |
| `java_heap_size` | `IBC_JAVA_HEAP_SIZE` | JVM heap for TWS/Gateway at launch, e.g. `"1024m"`/`"4g"`. Overrides the `-Xmx` line in the installed `.vmoptions` file; bare size, no `-Xmx` prefix | `None` (unchanged) |
| `diagnostic_scope` | `IBC_DIAGNOSTIC_SCOPE` | Which windows get a structure dump logged: `"known"` (recognised by a built-in/declarative recogniser), `"unknown"`, or `"all"` -- see "Diagnostics" below | `"known"` |
| `diagnostic_when` | `IBC_DIAGNOSTIC_WHEN` | When to log a structure dump: `"open"`, `"openclose"`, or `"never"` (off) -- see "Diagnostics" below | `"never"` |

## TWS/ibgateway specific settings

Settings related to TWS/ibgateway.

| TOML key | Env var | Description | Default |
| --- | --- | --- | --- |
| `trading_mode` | `IBC_TRADING_MODE` | `"live"` or `"paper"`; which account to authenticate as | `"paper"` |
| `read_only_login` | `IBC_READ_ONLY_LOGIN` | TWS only. Loaded but not yet wired to any behavior -- reserved | `false` |
| `read_only_api` | `IBC_READ_ONLY_API` | `true`/`false` sets it; omit to leave the existing setting unchanged. Applied automatically -- see "Declarative settings" | `None` (unchanged) |
| `accept_incoming_connections` | `IBC_ACCEPT_INCOMING_CONNECTIONS` | How to handle incoming API connection dialogs -- see below | `"manual"` |
| `existing_session_action` | `IBC_EXISTING_SESSION_ACTION` | What to do when an existing session is detected -- see below | `"manual"` |
| `auto_restart_time` | `IBC_AUTO_RESTART_TIME` | Daily auto-restart time, `"hh:mm AM/PM"` -- see below | `None` (unchanged) |
| `auto_logoff_time` | `IBC_AUTO_LOGOFF_TIME` | Daily auto-logoff time, `"hh:mm AM/PM"` -- see below | `None` (unchanged) |

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

### Auto Restart Time

**key:** `auto_restart_time`
**Environment variable:** `IBC_AUTO_RESTART_TIME`

`"hh:mm AM/PM"` (e.g. `"08:00 AM"`). Sets TWS/Gateway's own daily "Auto restart"
time under Lock and Exit. Omit this key (the default) to leave the existing
setting unchanged. Shares one radio-button pair with Auto Logoff Time below --
if both are set, this one wins (applied last).

### Auto Logoff Time

**key:** `auto_logoff_time`
**Environment variable:** `IBC_AUTO_LOGOFF_TIME`

Same `"hh:mm AM/PM"` format and the same Lock and Exit control as Auto Restart
Time above, just selecting "Auto logoff" instead of "Auto restart". Omit this
key (the default) to leave the existing setting unchanged. If both keys are
set, Auto Restart Time wins -- set only one of the two in practice.

### Cold Restart Time

**key:** `cold_restart_time`
**Environment variable:** `IBC_COLD_RESTART_TIME`

TWS and Gateway alike (ported from IBC's `IbcTws.java`, the shared base class
both programs use). `"HH:MM"`, 24-hour, local system time. Unlike the settings
above, this is not a Global Configuration GUI setting -- it's a self-scheduled
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
| `mfa_timeout` | `IBC_MFA_TIMEOUT` | The real 2FA budget in seconds, mirroring IBKR's own external limit | `180.0` |
| `relogin_after_mfa_timeout` | `IBC_RELOGIN_AFTER_MFA_TIMEOUT` | Restart the login attempt if 2FA times out (instead of giving up) | `false` |
| `mfa_exit_interval` | `IBC_MFA_EXIT_INTERVAL` | Bounds the wait after a timed-out 2FA when relogin is enabled | `60.0` |

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

## Declarative settings

`read_only_api` and `auto_restart_time` (and `auto_logoff_time`) are applied to
TWS/Gateway through built-in declarative settings entries loaded automatically
at startup (see `data/builtin_settings.toml` and `ibkr_settings.toml.example`).
`settings_file` points at an extra declarative file layered on top of the
built-in entries -- the built-ins fire first, then the extra file's entries
override them. The built-ins only act when their own config key is set:
`read_only_api` applies when it's `true`/`false` and is skipped when `None`;
`auto_restart_time`/`auto_logoff_time` apply only when a value is set.
