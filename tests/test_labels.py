"""Unit tests for labels.py -- no live Gateway needed."""

from __future__ import annotations

import json
import logging

import pytest

from ibcontroller.config import ConfigError
from ibcontroller.labels import (
    Labels,
    LabelsError,
    label_matches,
    load_labels,
    strip_html,
    text_matches,
    text_starts_with,
)


def test_bundled_default_loads_and_structures():
    labels = load_labels()
    assert isinstance(labels, Labels)
    assert "IBKR Gateway" in labels.login.gateway_titles
    assert labels.mfa.title == "Second Factor Authentication"
    assert labels.mfa.tws_frame_label == "SECOND FACTOR AUTHENTICATION"
    assert labels.mfa.read_only_button == "Enter Read Only"
    assert labels.existing_session.title == "Existing session detected"
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
            "dismiss_rules": [{"name": "r", "click": "OK", "match_title": "t"}],
        },
    )
    with caplog.at_level(logging.WARNING, logger="ibcontroller.labels"):
        labels = load_labels(config_dir=tmp_path)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "login.gateway_title" in logged
    assert "'bogus'" in logged
    assert "'dismiss_rules'" in logged  # removed in favour of the dialog catalog
    assert labels.login.gateway_titles[0] == "IBKR Gateway"  # typo had no effect


def test_settings_sweep_paths_are_in_the_bundled_file():
    paths = load_labels().settings.sweep_paths
    assert {"API/Settings", "Lock and Exit", "Orders/Smart Routing"} <= set(paths)


def teststrip_html_collapses_tags_and_whitespace():
    raw = (
        "<html>Connection to server failed:  <br><br>  The specified user has "
        "multiple Paper Trading users associated with it.<br><br>Please log on "
        "using one of the Paper Trading users and corresponding password.</html>"
    )
    stripped = strip_html(raw)
    assert stripped.startswith("Connection to server failed:")
    assert "multiple Paper Trading users associated with it" in stripped
    assert "<" not in stripped


def test_text_matches_ignores_case_and_html():
    assert text_matches("login failed", "Some LOGIN FAILED here") is True
    assert text_matches("failed: the user", "<html>Failed:<br>The user</html>") is True
    assert text_matches('say "hi"', "say &quot;hi&quot;") is True
    assert text_matches("absent", "<html>Nothing</html>") is False


def test_label_matches_ignores_case_and_surrounding_whitespace():
    assert label_matches("Log In", "log in") is True
    assert label_matches("OK", " ok ") is True


def test_label_matches_is_whole_label_and_null_safe():
    assert label_matches("Log In", "Paper Log In") is False
    assert label_matches("OK", "Book") is False
    assert label_matches("OK", None) is False


def test_text_starts_with_is_anchored_ignores_case_and_html():
    prefix = "Connection to server failed:"
    assert text_starts_with(prefix, "<html>CONNECTION TO SERVER FAILED: x</html>")
    assert not text_starts_with(
        prefix, "<html>Oops connection to server failed:</html>"
    )
