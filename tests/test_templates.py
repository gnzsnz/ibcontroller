"""The bundled `ibcontroller.toml.example` and the repository's `ibcontroller.env`
document every catalog setting and dialog, and the commented examples stay valid."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import attrs

from ibcontroller.config import Config
from ibcontroller.dialogs import load_catalog, parse_custom_dialogs, resolve_dialogs
from ibcontroller.settings import (
    load_setting_catalog,
    parse_custom_settings,
    resolve_settings,
)

_EXAMPLE = Path("ibcontroller/data/ibcontroller.toml.example")
_ENV = Path("ibcontroller.env")
# A commented line that is a table header or `key = value`, as opposed to prose.
_COMMENTED_CODE = re.compile(r"^# (\[|[a-z_]+ = )")


def _uncommented(text: str) -> str:
    return "\n".join(
        line[2:] if _COMMENTED_CODE.match(line) else line for line in text.splitlines()
    )


def test_example_documents_every_catalog_dialog():
    text = _EXAMPLE.read_text(encoding="utf-8")
    for entry in load_catalog().dialog:
        assert f"# {entry.name} = " in text, entry.name


def test_example_documents_every_catalog_setting():
    text = _EXAMPLE.read_text(encoding="utf-8")
    for entry in load_setting_catalog().setting:
        assert f"# {entry.name} = " in text, entry.name


def test_env_file_documents_every_catalog_setting():
    text = _ENV.read_text(encoding="utf-8")
    for entry in load_setting_catalog().setting:
        assert f"# IBKR_{entry.name.upper()}=" in text, entry.name


def test_env_file_documents_every_catalog_dialog():
    text = _ENV.read_text(encoding="utf-8")
    for entry in load_catalog().dialog:
        assert f"# IBKR_{entry.name.upper()}=" in text, entry.name


def test_example_parses_with_every_example_uncommented():
    text = _EXAMPLE.read_text(encoding="utf-8")
    # the [ibcontroller] part has alternative values for one key, so it cannot all be
    # uncommented at once
    data = tomllib.loads(_uncommented(text[text.index("\n[ibkr]") :]))
    catalog = load_catalog()
    custom = parse_custom_dialogs(data["dialog"], source="example")
    ibkr = data["ibkr"]
    sources = dict.fromkeys(ibkr, "file")
    resolve_dialogs(catalog, custom, ibkr, sources)  # every mode is a real one
    assert set(ibkr) >= {entry.name for entry in catalog.dialog}
    setting_catalog = load_setting_catalog()
    custom_settings = parse_custom_settings(data["setting"], source="example")
    resolve_settings(setting_catalog, custom_settings, ibkr, sources)  # real values
    assert set(ibkr) >= {entry.name for entry in setting_catalog.setting}
    assert {e.name for e in custom_settings}.isdisjoint(
        e.name for e in setting_catalog.setting
    )


def test_example_ibcontroller_table_documents_only_config_fields():
    """Every commented key of the `[ibcontroller]` part is a real `Config` field, so
    a field moved to `[ibkr]` cannot linger there."""
    text = _EXAMPLE.read_text(encoding="utf-8")
    part = text[text.index("\n[ibcontroller]") : text.index("\n[ibkr]")]
    keys = set(re.findall(r"^# ([a-z_]+) =", part, re.MULTILINE))
    fields = {field.name for field in attrs.fields(Config)}
    assert keys <= fields, keys - fields


def test_example_shipped_state_changes_nothing():
    """Every key stays commented out, so the scaffold behaves as the defaults."""
    data = tomllib.loads(_EXAMPLE.read_text(encoding="utf-8"))
    assert data == {"ibcontroller": {}, "ibkr": {}}
