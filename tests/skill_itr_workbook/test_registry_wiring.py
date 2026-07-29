"""
tests/skill_itr_workbook/test_registry_wiring.py -- Phase 4 wiring of
ui/_book_registry.py into two seams (GnuCash book registry architecture,
2026-07-29 handover):

  1. ui/tabs/_generic.py's `_registry_book_fill()` -- auto-fills an empty
     optional ITR Workbook `book_file` input from the registry, keyed off
     the entity + FY (carried by the `ay` select) already chosen earlier in
     the same run.
  2. ui/tabs/itr_mapping_review.py's `_entity_book_path()` -- now delegates
     to `_book_registry.resolve_book()` (fy=None) instead of reading only
     the legacy raw `book:` key, so Layer B validation also sees `books`-
     registered entities.

Synthetic names only (AliceDoe/BobDoe/CarolDoe/DaveDoe/BobDoeHUF), synthetic
PAN ABCDE1234X. Every ".gnucash" path used here is a tmp_path placeholder
file (created empty, never parsed) -- no real book, no real entities.yaml,
nothing under Data/ or C:\\PortableApps is read.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
SRC = ROOT / "src"

for p in (str(SCRIPTS), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import configs  # noqa: E402

from ui.tabs import _generic as generic_mod  # noqa: E402
from ui.tabs import itr_mapping_review as review_mod  # noqa: E402


PAN = "ABCDE1234X"


def _write_entities(tmp_path: Path, entities: dict) -> Path:
    data_root = tmp_path / "Data"
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_path.write_text(configs.dump_entities(entities), encoding="utf-8")
    return data_root


class _Skill:
    def __init__(self, name: str):
        self.name = name


class _InputDef:
    def __init__(self, name: str):
        self.name = name


# ---------------------------------------------------------------------------
# _registry_book_fill
# ---------------------------------------------------------------------------

def test_registry_book_fill_returns_registered_book_for_selected_fy(tmp_path):
    book_path = tmp_path / "AliceDoe2526.gnucash"
    book_path.write_text("", encoding="utf-8")
    entities = {
        "AliceDoe": configs.EntityProfile(
            key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual",
            books={"2025-26": str(book_path)},
        ),
    }
    data_root = _write_entities(tmp_path, entities)

    with patch("ui._config.data_root_dir", return_value=data_root):
        result = generic_mod._registry_book_fill(
            _Skill("ITR Workbook"), _InputDef("book_file"),
            {"entity": "AliceDoe", "ay": "2025-26"},
        )
    assert result == str(book_path)


def test_registry_book_fill_empty_entity_returns_empty(tmp_path):
    with patch("ui._config.data_root_dir", return_value=tmp_path / "Data"):
        result = generic_mod._registry_book_fill(
            _Skill("ITR Workbook"), _InputDef("book_file"),
            {"entity": "", "ay": "2025-26"},
        )
    assert result == ""


def test_registry_book_fill_unknown_entity_returns_empty(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = generic_mod._registry_book_fill(
            _Skill("ITR Workbook"), _InputDef("book_file"),
            {"entity": "NoSuchEntity", "ay": "2025-26"},
        )
    assert result == ""


def test_registry_book_fill_missing_on_disk_returns_empty(tmp_path):
    # Registered but the file was never actually created under tmp_path.
    entities = {
        "BobDoe": configs.EntityProfile(
            key="BobDoe", name="Bob Doe", pan=PAN, status="Individual",
            books={"2025-26": str(tmp_path / "BobDoe2526.gnucash")},
        ),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = generic_mod._registry_book_fill(
            _Skill("ITR Workbook"), _InputDef("book_file"),
            {"entity": "BobDoe", "ay": "2025-26"},
        )
    assert result == ""


def test_registry_book_fill_non_itr_skill_returns_empty(tmp_path):
    book_path = tmp_path / "CarolDoe2526.gnucash"
    book_path.write_text("", encoding="utf-8")
    entities = {
        "CarolDoe": configs.EntityProfile(
            key="CarolDoe", name="Carol Doe", pan=PAN, status="Individual",
            books={"2025-26": str(book_path)},
        ),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = generic_mod._registry_book_fill(
            _Skill("Some Other Skill"), _InputDef("book_file"),
            {"entity": "CarolDoe", "ay": "2025-26"},
        )
    assert result == ""


def test_registry_book_fill_different_input_name_returns_empty(tmp_path):
    book_path = tmp_path / "DaveDoe2526.gnucash"
    book_path.write_text("", encoding="utf-8")
    entities = {
        "DaveDoe": configs.EntityProfile(
            key="DaveDoe", name="Dave Doe", pan=PAN, status="Individual",
            books={"2025-26": str(book_path)},
        ),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = generic_mod._registry_book_fill(
            _Skill("ITR Workbook"), _InputDef("mapping_file"),
            {"entity": "DaveDoe", "ay": "2025-26"},
        )
    assert result == ""


def test_registry_book_fill_swallows_resolve_book_exception(tmp_path):
    with patch("ui._config.data_root_dir", return_value=tmp_path / "Data"), \
         patch("ui._book_registry.resolve_book", side_effect=RuntimeError("boom")):
        result = generic_mod._registry_book_fill(
            _Skill("ITR Workbook"), _InputDef("book_file"),
            {"entity": "BobDoeHUF", "ay": "2025-26"},
        )
    assert result == ""


# ---------------------------------------------------------------------------
# _entity_book_path (Layer B, ui/tabs/itr_mapping_review.py)
# ---------------------------------------------------------------------------

def test_entity_book_path_uses_newest_registered_fy(tmp_path):
    entities = {
        "BobDoeHUF": configs.EntityProfile(
            key="BobDoeHUF", name="Bob Doe HUF", pan=PAN, status="HUF",
            books={
                "2024-25": "C:/books/BobDoeHUF2425.gnucash",
                "2025-26": "C:/books/BobDoeHUF2526.gnucash",
            },
        ),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = review_mod._entity_book_path("BobDoeHUF")
    assert result == Path("C:/books/BobDoeHUF2526.gnucash")


def test_entity_book_path_falls_back_to_legacy_book_key(tmp_path):
    data_root = tmp_path / "Data"
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_path.write_text(
        "DaveDoe:\n"
        "  name: Dave Doe\n"
        f"  pan: {PAN}\n"
        "  status: Individual\n"
        "  book: C:/books/DaveDoeLegacy.gnucash\n",
        encoding="utf-8",
    )
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = review_mod._entity_book_path("DaveDoe")
    assert result == Path("C:/books/DaveDoeLegacy.gnucash")


def test_entity_book_path_none_when_neither_configured(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = review_mod._entity_book_path("AliceDoe")
    assert result is None
