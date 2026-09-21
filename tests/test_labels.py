"""Unit tests for labels.py -- no live Gateway needed."""

from __future__ import annotations

import json

import pytest

from ibcontroller.labels import DismissRule, Labels, LabelsError, load_labels


def test_bundled_default_loads_and_structures():
    labels = load_labels()
    assert isinstance(labels, Labels)
    assert "IBKR Gateway" in labels.login.gateway_titles
    assert labels.mfa.title == "Second Factor Authentication"
    assert labels.existing_session.title == "Existing session detected"
    assert labels.accept_incoming_connection.title == "Accept incoming connection"
    assert labels.accept_incoming_connection.accept_buttons == ["OK", "Yes"]
    assert labels.accept_incoming_connection.reject_buttons == ["No"]
    assert labels.login_failed.title == "Login failed"
    assert {rule.name for rule in labels.dismiss_rules} == {
        "non_brokerage_account",
        "auto_restart_confirmation",
    }
    non_brokerage = next(
        r for r in labels.dismiss_rules if r.name == "non_brokerage_account"
    )
    assert non_brokerage.match_text == "This is not a brokerage account"
    assert non_brokerage.click == "I understand and accept"


def test_dismiss_rule_needs_at_least_one_match_field():
    with pytest.raises(LabelsError):
        DismissRule(name="bad", click="OK")


def test_dismiss_rules_override_replaces_the_whole_list(tmp_path):
    """A list, not a nested object -- `_deep_merge` replaces it wholesale, it
    doesn't merge entry-by-entry (labels.py's own module docstring)."""
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
    assert [r.name for r in labels.dismiss_rules] == ["custom"]


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
