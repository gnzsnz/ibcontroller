"""Unit tests for labels.py -- no live Gateway needed."""

from __future__ import annotations

import json
import logging

import pytest

from ibcontroller.config import ConfigError
from ibcontroller.labels import DismissRule, Labels, LabelsError, load_labels


def test_bundled_default_loads_and_structures():
    labels = load_labels()
    assert isinstance(labels, Labels)
    assert "IBKR Gateway" in labels.login.gateway_titles
    assert labels.mfa.title == "Second Factor Authentication"
    assert labels.mfa.tws_frame_label == "SECOND FACTOR AUTHENTICATION"
    assert labels.existing_session.title == "Existing session detected"
    assert labels.accept_incoming_connection.title == "Accept incoming connection"
    assert labels.accept_incoming_connection.accept_buttons == ["OK", "Yes"]
    assert labels.accept_incoming_connection.reject_buttons == ["No"]
    assert labels.login_failed.title == "Login failed"
    assert labels.login_error.title == "Login Error"
    assert labels.unrecognized_credentials.title == "Unrecognized Username or Password"
    assert labels.gateway_connection_failed.title == "Gateway"
    assert labels.gateway_connection_failed.message_prefix == (
        "Connection to server failed:"
    )
    assert labels.gateway_connection_failed.known_reasons == [
        "has multiple Paper Trading users associated with it"
    ]
    assert {rule.name for rule in labels.dismiss_rules} == {
        "non_brokerage_account",
        "auto_restart_confirmation",
    }
    non_brokerage = next(
        r for r in labels.dismiss_rules if r.name == "non_brokerage_account"
    )
    assert non_brokerage.match_text == "This is not a brokerage account"
    assert non_brokerage.click == "I understand and accept"


def test_api_settings_controls_are_in_the_bundled_file():
    controls = load_labels().settings.controls
    assert controls["socket_port"] == "Socket port"
    assert controls["master_api_client_id"] == "Master API client ID"
    assert controls["send_market_data_in_lots_us_stocks"] == (
        "Send market data in lots for US stocks for dual-mode API clients "
        "(Not recommended)."
    )


def test_generic_dismiss_buttons_load_from_bundled_file():
    assert load_labels().generic.dismiss_buttons == ["OK", "Close", "Dismiss"]


def test_generic_dismiss_buttons_reject_empty_override(tmp_path):
    (tmp_path / "labels.json").write_text('{"generic": {"dismiss_buttons": []}}')
    with pytest.raises(LabelsError):
        load_labels(config_dir=tmp_path)


def test_dismiss_rule_needs_at_least_one_match_field():
    with pytest.raises(LabelsError):
        DismissRule(name="bad", click="OK")


def _bundled_rule_names():
    return [r.name for r in load_labels(config_dir=None).dismiss_rules]


def test_dismiss_rules_override_appends_new_name(tmp_path):
    (tmp_path / "labels.json").write_text(
        json.dumps(
            {
                "dismiss_rules": [
                    {"name": "custom", "click": "OK", "match_title": "Custom Dialog"}
                ]
            }
        )
    )
    labels = load_labels(config_dir=tmp_path)
    assert [r.name for r in labels.dismiss_rules] == [*_bundled_rule_names(), "custom"]


def test_dismiss_rules_override_replaces_same_name(tmp_path):
    bundled = _bundled_rule_names()
    (tmp_path / "labels.json").write_text(
        json.dumps(
            {
                "dismiss_rules": [
                    {"name": bundled[0], "click": "Nope", "match_title": "Other"}
                ]
            }
        )
    )
    labels = load_labels(config_dir=tmp_path)
    assert [r.name for r in labels.dismiss_rules] == bundled
    assert labels.dismiss_rules[0].click == "Nope"


def test_dismiss_rules_bundled_kept_without_override(tmp_path):
    (tmp_path / "labels.json").write_text(
        json.dumps({"login": {"login_buttons": ["X"]}})
    )
    labels = load_labels(config_dir=tmp_path)
    assert [r.name for r in labels.dismiss_rules] == _bundled_rule_names()


def test_no_config_dir_uses_bundled_default_only():
    labels = load_labels(config_dir=None)
    assert labels.login.login_buttons == ["Log In", "Paper Log In", "Login"]


def test_missing_override_file_falls_back_to_default(tmp_path):
    labels = load_labels(config_dir=tmp_path)
    assert labels.login.gateway_titles == [
        "IBKR Gateway",
        "IB Gateway",
        "Interactive Brokers Gateway",
    ]


def test_override_replaces_one_field_leaves_siblings_untouched(tmp_path):
    (tmp_path / "labels.json").write_text(
        json.dumps({"login": {"gateway_titles": ["Custom Gateway Title"]}})
    )
    labels = load_labels(config_dir=tmp_path)
    assert labels.login.gateway_titles == ["Custom Gateway Title"]
    # sibling field, untouched
    assert labels.login.login_buttons == ["Log In", "Paper Log In", "Login"]


def test_override_leaves_other_domains_untouched(tmp_path):
    (tmp_path / "labels.json").write_text(
        json.dumps({"login_failed": {"title": "Custom Login Failed Title"}})
    )
    labels = load_labels(config_dir=tmp_path)
    assert labels.login_failed.title == "Custom Login Failed Title"
    assert labels.login_failed.dismiss_button == "OK"
    assert labels.existing_session.title == "Existing session detected"


def _write_override(tmp_path, content) -> None:
    (tmp_path / "labels.json").write_text(
        content if isinstance(content, str) else json.dumps(content)
    )


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"login": {"gateway_titles": "oops"}}, "$.login.gateway_titles"),
        ({"login": {"tws_titles": 5}}, "$.login.tws_titles"),
        ({"login": {"tws_titles": [1]}}, "$.login.tws_titles"),
        ({"mfa": {"title": ["a"]}}, "$.mfa.title"),
        ({"gateway_connection_failed": {"known_reasons": "x"}}, "known_reasons"),
    ],
)
def test_wrong_type_is_rejected_naming_file_and_field(tmp_path, override, field):
    _write_override(tmp_path, override)
    with pytest.raises(LabelsError) as excinfo:
        load_labels(config_dir=tmp_path)
    assert str(tmp_path / "labels.json") in str(excinfo.value)
    assert field in str(excinfo.value)


@pytest.mark.parametrize(
    ("group", "name"),
    [
        ("login", "gateway_titles"),
        ("login", "tws_titles"),
        ("login", "login_buttons"),
        ("existing_session", "continue_buttons"),
        ("existing_session", "cancel_buttons"),
        ("accept_incoming_connection", "accept_buttons"),
        ("accept_incoming_connection", "reject_buttons"),
    ],
)
def test_empty_candidate_list_is_rejected(tmp_path, group, name):
    _write_override(tmp_path, {group: {name: []}})
    with pytest.raises(LabelsError, match=f"{name} must not be empty"):
        load_labels(config_dir=tmp_path)


def test_empty_known_reasons_is_accepted(tmp_path):
    _write_override(tmp_path, {"gateway_connection_failed": {"known_reasons": []}})
    assert (
        load_labels(config_dir=tmp_path).gateway_connection_failed.known_reasons == []
    )


def test_dismiss_rule_without_match_fields_raises_labels_error(tmp_path):
    _write_override(tmp_path, {"dismiss_rules": [{"name": "bad", "click": "OK"}]})
    with pytest.raises(LabelsError, match="dismiss_rules entry 'bad'"):
        load_labels(config_dir=tmp_path)


def test_invalid_json_raises_labels_error(tmp_path):
    _write_override(tmp_path, "{oops")
    with pytest.raises(LabelsError, match="invalid JSON"):
        load_labels(config_dir=tmp_path)


def test_non_object_top_level_raises_labels_error(tmp_path):
    _write_override(tmp_path, [1, 2])
    with pytest.raises(LabelsError, match="JSON object"):
        load_labels(config_dir=tmp_path)


def test_labels_error_is_a_config_error():
    assert issubclass(LabelsError, ConfigError)


def test_unknown_keys_are_logged_and_ignored(tmp_path, caplog):
    _write_override(
        tmp_path,
        {
            "login": {"gateway_title": ["x"]},
            "bogus": 1,
            "dismiss_rules": [
                {"name": "r", "click": "OK", "match_title": "t", "extra": 1}
            ],
        },
    )
    with caplog.at_level(logging.WARNING, logger="ibcontroller.labels"):
        labels = load_labels(config_dir=tmp_path)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "login.gateway_title" in logged
    assert "'bogus'" in logged
    assert "dismiss_rules[0].extra" in logged
    assert labels.login.gateway_titles[0] == "IBKR Gateway"  # typo had no effect


def test_settings_sweep_paths_are_in_the_bundled_file():
    paths = load_labels().settings.sweep_paths
    assert {"API/Settings", "Lock and Exit", "Orders/Smart Routing"} <= set(paths)
