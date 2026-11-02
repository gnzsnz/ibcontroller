# Upgrading

## From v0.1.x to v0.2.0

v0.2.0 is a breaking release. There is no compatibility layer and there are no aliases: a name that
moved is not read from its old place. The PyPI package is `py-ib-controller`; the import package and
the command stay `ibcontroller`. The easiest way to migrate is to copy the commented
[`ibcontroller.toml.example`](../ibcontroller/data/ibcontroller.toml.example) and
[`ibcontroller.env`](../ibcontroller.env), uncomment what you use, and check the table below for
renamed keys.

### Breaking changes

| Was | Now | What happens if you do nothing |
| --- | --- | --- |
| Flat `ibcontroller.toml` (keys at top level) | Keys under `[ibcontroller]` | The file is rejected at startup (exit 78): a top-level key outside a table is an error naming `[ibcontroller]`. |
| `read_only_api`, `auto_restart_time`, `auto_logoff_time` as `[ibcontroller]` keys; `IBC_READ_ONLY_API`, `IBC_AUTO_RESTART_TIME`, `IBC_AUTO_LOGOFF_TIME` | `[ibkr]` keys of the same names; `IBKR_READ_ONLY_API`, `IBKR_AUTO_RESTART_TIME`, `IBKR_AUTO_LOGOFF_TIME` | The file keys are rejected by the closed `[ibcontroller]` schema (exit 78). The environment variables are ignored silently, so the setting is no longer applied. |
| `accept_incoming_connections` as an `[ibcontroller]` key (boolean-like); `IBC_ACCEPT_INCOMING_CONNECTIONS` | `accept_incoming_connections` in `[ibkr]` with the modes `manual` (default), `accept` and `reject`; `IBKR_ACCEPT_INCOMING_CONNECTIONS` | The file key is rejected by the closed schema (exit 78). The environment variable is no longer read, so the dialog stays in `manual`. A boolean value for the new key is a startup error. |
| `settings_file`, `IBC_SETTINGS_FILE`, `ibkr_settings.toml` (and the scaffolded `ibkr_settings.toml.example`) | Custom entries as `[[setting]]` and `[[dialog]]` in `ibcontroller.toml` | The `settings_file` key is rejected by the closed schema (exit 78). `IBC_SETTINGS_FILE` is not read and an `ibkr_settings.toml` file is never loaded, so its entries are not applied. `init` no longer writes the example file. |
| `dismiss_rules` and `accept_incoming_connection` keys in `labels.json` | The dialog catalog (`catalog.toml`), switched by name in `[ibkr]` | Logged as an unknown key and ignored; the bundled catalog entries handle those dialogs. |

### Behaviour changes

- Matching ignores case. It only adds matches: every label that matched before still matches. The
  rule is in [Configuration](Configuration.md#matching).
- `IBKR_*` is a reserved prefix. Every environment variable that starts with `IBKR_` is read as an
  `[ibkr]` key; one that names no setting or dialog is logged as a warning and ignored. A search of
  ib-gateway-docker, ibg-controller, ibga, IBC and ibctl found no collision with any existing
  `IBKR_*` variable.

### Not verified live

These entries ship working as designed from IBC's source, without a check against a live window.

| Entry | Reason |
| --- | --- |
| `accept_incoming_connections` | Dialog text from IBC's source. The dialog never opened on Gateway paper, from localhost or from another host; whether Gateway can show it at all is unknown. |
| `crypto_order_confirmation` | Dialog text from IBC's source; needs a cryptocurrency order to trigger it. |
| `password_expiry_warning` | Dialog text from IBC's source; not triggerable on the available accounts. |
| `auto_restart_confirmation` | Dialog text from IBC's source; the popup was not seen on a live run. |
| `blind_trading` | Dialog text from IBC's source; needs a paper order that raises the warning. |
| `auto_logoff_time` | Not applied on a live run yet. Its mechanism is the same as `auto_restart_time`, which was. |

### Not in this release

Left out of v0.2.0 on purpose:

- The dialog `toggle` step and with it the `bid_ask_last_size_update` dialog (IBC's
  `AcceptBidAskLastSizeDisplayUpdateNotification`).
- Settings that open a modal when set: `send_market_data_in_lots_us_stocks = true` and
  `api_precautions_bypass_route_marketable_to_bbo = true` (`false` is safe).
- The `close` step and the NSE compliance notice.
- Lookup of a control by a named row.

IBC `config.ini` keys that are not implemented or only partly (the full status is in the table
below):

| IBC key | Reason |
| --- | --- |
| `FIX`, `FIXLoginId`, `FIXPassword`, `TrustedTwsApiClientIPs` | FIX login is not supported. |
| `SecondFactorDevice` | Needs a list-select agent command (#16). |
| `AcceptBidAskLastSizeDisplayUpdateNotification` | Needs the dialog `toggle` step (#135). |
| `SendMarketDataInLotsForUSstocks` | Partial: `false` works, `true` opens a modal that blocks (#132). |
| `StoreSettingsOnServer`, `MinimizeMainWindow`, `ResetOrderIdsAtStart`, `ConfirmOrderIdReset`, `SaveTwsSettingsAt`, `IncludeStackTraceForExceptions` | Not implemented; low priority. |
| `DismissNSEComplianceNotice` | Needs a `close` step (#126). |
| `CommandServerPort`, `ControlFrom`, `BindAddress` | Command server; not built. |

### Coming from IBC

The table maps IBC's `config.ini` keys to ibcontroller. `[ibkr]` names are also `IBKR_<NAME>`
variables; `[ibcontroller]` keys are also `IBC_<NAME>` variables. Credentials are environment
variables only.

| IBC key | ibcontroller | Status |
| --- | --- | --- |
| `IbLoginId`, `IbPassword` | `IBC_USERID`, `IBC_PASSWORD` (and `_FILE` variants) | Implemented; never from the config file. |
| `FIXLoginId`, `FIXPassword`, `FIX` | none | Not implemented (no FIX support). |
| `SecondFactorDevice` | none | Not implemented (#16). |
| `ReloginAfterSecondFactorAuthenticationTimeout` | `relogin_after_mfa_timeout` | Implemented. |
| `SecondFactorAuthenticationExitInterval` | `mfa_exit_interval` | Implemented. |
| `SecondFactorAuthenticationTimeout` | `mfa_timeout` | Implemented. |
| `ExitAfterSecondFactorAuthenticationTimeout` | `mfa_timeout_action` | Not planned: deprecated in IBC, `mfa_timeout_action = "exit"` covers it. |
| `TradingMode` | `trading_mode`, `--trading-mode` | Implemented. The default is `paper`; IBC's is `live`. |
| `AcceptNonBrokerageAccountWarning` | `non_brokerage_account` | Implemented. |
| `LoginDialogDisplayTimeout` | `login_dialog_display_timeout` | Implemented. |
| `IbDir` | `tws_settings_path`, `--tws-settings-path` | Implemented. |
| `StoreSettingsOnServer` | none | Not implemented (TWS only). |
| `MinimizeMainWindow` | none | Not implemented. |
| `ExistingSessionDetectedAction` | `existing_session_action` | Implemented, all four values. |
| `OverrideTwsApiPort` | `socket_port` (TWS also `socket_port_confirmation`) | Implemented; IBC's extra "Enable ActiveX and Socket Clients" check is not done. |
| `OverrideTwsMasterClientID` | `master_api_client_id` | Implemented. |
| `ReadOnlyLogin` | `read_only_login` | Implemented (TWS only). |
| `ReadOnlyApi` | `read_only_api` | Implemented. |
| `BypassOrderPrecautions`, `BypassBondWarning`, `BypassNegativeYieldToWorstConfirmation`, `BypassCalledBondWarning`, `BypassSameActionPairTradeWarning`, `BypassPriceBasedVolatilityRiskWarning`, `BypassRedirectOrderWarning`, `BypassNoOverfillProtectionPrecaution` | `api_precautions_bypass_order_precautions`, `_bond_warning`, `_negative_yield_to_worst`, `_called_bond_warning`, `_same_action_pair_trade`, `_price_based_volatility`, `_redirect_order_warning`, `_no_overfill_protection` | Implemented. |
| `BypassUSStocksMarketDataInSharesWarning` | none | Not planned: the control is not on the current panel. |
| `AcceptBidAskLastSizeDisplayUpdateNotification` | none | Not implemented (#135). |
| `SendMarketDataInLotsForUSstocks` | `send_market_data_in_lots_us_stocks` | Partial: `false` works, `true` blocks on a modal (#132). |
| `TrustedTwsApiClientIPs` | none | Not implemented (FIX only). |
| `ResetOrderIdsAtStart`, `ConfirmOrderIdReset` | none | Not implemented. |
| `AutoLogoffTime` | `auto_logoff_time` | Implemented; unverified live. |
| `AutoRestartTime` | `auto_restart_time` | Implemented. |
| `ColdRestartTime` | `cold_restart_time` | Implemented. |
| `ClosedownAt` | `closedown_at` | Implemented. |
| `AcceptIncomingConnectionAction` | `accept_incoming_connections` | Implemented; unverified live. |
| `AllowBlindTrading` | `blind_trading` | Implemented; unverified live. |
| `SaveTwsSettingsAt` | none | Not implemented. |
| `ConfirmCryptoCurrencyOrders` | `crypto_order_confirmation` | Implemented; unverified live. |
| `DismissPasswordExpiryWarning` | `password_expiry_warning` | Implemented; unverified live. |
| `DismissNSEComplianceNotice` | none | Not implemented (#126). |
| `CommandServerPort`, `ControlFrom`, `BindAddress` | none | Not implemented (command server). |
| `LogStructureScope` | `diagnostic_scope` | Implemented. |
| `LogStructureWhen` | `diagnostic_when` | Implemented (no `activate` value). |
| `IncludeStackTraceForExceptions` | none | Not implemented as a setting. |

`LogComponents`, `CommandPrompt` and `SuppressInfoMessages` are left out: they are deprecated in IBC
or specific to its telnet command server, and not planned here.
