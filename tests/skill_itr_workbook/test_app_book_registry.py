"""
tests/skill_itr_workbook/test_app_book_registry.py -- ui/_book_registry.py
(GnuCash book registry architecture, Phase 2, 2026-07-29 handover).

Covers resolve_book() precedence (fy hit / newest-FY fallback / legacy
`book:` fallback / None), list_books() (empty / populated / legacy-book-not-
synthesized), and set_book() (new FY / overwrite / round-trip / unknown key
/ malformed FY).

Synthetic names only (AliceDoe/BobDoe/CarolDoe/DaveDoe/BobDoeHUF), synthetic
PAN ABCDE1234X -- no real family names, PANs, or data files are read. All
entities.yaml fixtures are written into tmp_path; nothing under Data/ or
C:\\PortableApps is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
SRC = ROOT / "src"

for p in (str(SCRIPTS), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import configs  # noqa: E402
from ui import _book_registry as reg  # noqa: E402


PAN = "ABCDE1234X"


def _write_entities(tmp_path: Path, entities: dict) -> Path:
    out = tmp_path / "entities.yaml"
    out.write_text(configs.dump_entities(entities), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# resolve_book
# ---------------------------------------------------------------------------

def test_resolve_book_fy_hit(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(
            key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual",
            books={
                "2024-25": "C:/books/AliceDoe2425.gnucash",
                "2025-26": "C:/books/AliceDoe2526.gnucash",
            },
        ),
    }
    path = _write_entities(tmp_path, entities)
    result = reg.resolve_book("AliceDoe", fy="2024-25", entities_path=path)
    assert result == Path("C:/books/AliceDoe2425.gnucash")


def test_resolve_book_newest_fy_fallback_when_no_fy_given(tmp_path):
    entities = {
        "BobDoe": configs.EntityProfile(
            key="BobDoe", name="Bob Doe", pan=PAN, status="Individual",
            books={
                "2023-24": "C:/books/BobDoe2324.gnucash",
                "2025-26": "C:/books/BobDoe2526.gnucash",
                "2024-25": "C:/books/BobDoe2425.gnucash",
            },
        ),
    }
    path = _write_entities(tmp_path, entities)
    result = reg.resolve_book("BobDoe", entities_path=path)
    assert result == Path("C:/books/BobDoe2526.gnucash")


def test_resolve_book_newest_fy_fallback_when_fy_not_present(tmp_path):
    entities = {
        "CarolDoe": configs.EntityProfile(
            key="CarolDoe", name="Carol Doe", pan=PAN, status="Individual",
            books={
                "2024-25": "C:/books/CarolDoe2425.gnucash",
                "2025-26": "C:/books/CarolDoe2526.gnucash",
            },
        ),
    }
    path = _write_entities(tmp_path, entities)
    result = reg.resolve_book("CarolDoe", fy="2026-27", entities_path=path)
    assert result == Path("C:/books/CarolDoe2526.gnucash")


def test_resolve_book_legacy_fallback_when_no_books_dict(tmp_path):
    out = tmp_path / "entities.yaml"
    out.write_text(
        "DaveDoe:\n"
        "  name: Dave Doe\n"
        f"  pan: {PAN}\n"
        "  status: Individual\n"
        "  book: C:/books/DaveDoeLegacy.gnucash\n",
        encoding="utf-8",
    )
    result = reg.resolve_book("DaveDoe", entities_path=out)
    assert result == Path("C:/books/DaveDoeLegacy.gnucash")


def test_resolve_book_books_dict_wins_over_legacy(tmp_path):
    out = tmp_path / "entities.yaml"
    out.write_text(
        "BobDoeHUF:\n"
        "  name: Bob Doe HUF\n"
        f"  pan: {PAN}\n"
        "  status: HUF\n"
        "  book: C:/books/LegacyIgnored.gnucash\n"
        "  books:\n"
        "    2025-26: C:/books/BobDoeHUF2526.gnucash\n",
        encoding="utf-8",
    )
    result = reg.resolve_book("BobDoeHUF", entities_path=out)
    assert result == Path("C:/books/BobDoeHUF2526.gnucash")


def test_resolve_book_none_when_nothing_configured(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
    }
    path = _write_entities(tmp_path, entities)
    assert reg.resolve_book("AliceDoe", entities_path=path) is None


def test_resolve_book_none_for_unknown_entity(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
    }
    path = _write_entities(tmp_path, entities)
    assert reg.resolve_book("NoSuchEntity", entities_path=path) is None


def test_resolve_book_none_when_file_missing(tmp_path):
    assert reg.resolve_book("AliceDoe", entities_path=tmp_path / "nope.yaml") is None


def test_resolve_book_none_when_file_malformed(tmp_path):
    bad = tmp_path / "entities.yaml"
    bad.write_text("not: [valid, entities, - yaml structure\n", encoding="utf-8")
    assert reg.resolve_book("AliceDoe", entities_path=bad) is None


# ---------------------------------------------------------------------------
# list_books
# ---------------------------------------------------------------------------

def test_list_books_empty_when_no_books(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
    }
    path = _write_entities(tmp_path, entities)
    assert reg.list_books("AliceDoe", entities_path=path) == {}


def test_list_books_populated(tmp_path):
    entities = {
        "BobDoe": configs.EntityProfile(
            key="BobDoe", name="Bob Doe", pan=PAN, status="Individual",
            books={
                "2024-25": "C:/books/BobDoe2425.gnucash",
                "2025-26": "C:/books/BobDoe2526.gnucash",
            },
        ),
    }
    path = _write_entities(tmp_path, entities)
    assert reg.list_books("BobDoe", entities_path=path) == {
        "2024-25": "C:/books/BobDoe2425.gnucash",
        "2025-26": "C:/books/BobDoe2526.gnucash",
    }


def test_list_books_does_not_synthesize_legacy_book_key(tmp_path):
    out = tmp_path / "entities.yaml"
    out.write_text(
        "DaveDoe:\n"
        "  name: Dave Doe\n"
        f"  pan: {PAN}\n"
        "  status: Individual\n"
        "  book: C:/books/DaveDoeLegacy.gnucash\n",
        encoding="utf-8",
    )
    assert reg.list_books("DaveDoe", entities_path=out) == {}


def test_list_books_unknown_entity(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
    }
    path = _write_entities(tmp_path, entities)
    assert reg.list_books("NoSuchEntity", entities_path=path) == {}


# ---------------------------------------------------------------------------
# set_book
# ---------------------------------------------------------------------------

def test_set_book_new_fy(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
    }
    path = _write_entities(tmp_path, entities)
    reg.set_book("AliceDoe", "2025-26", "C:/books/AliceDoe2526.gnucash", entities_path=path)

    reloaded = configs.load_entities(path)
    assert reloaded["AliceDoe"].books == {"2025-26": "C:/books/AliceDoe2526.gnucash"}


def test_set_book_overwrite_existing_fy(tmp_path):
    entities = {
        "BobDoe": configs.EntityProfile(
            key="BobDoe", name="Bob Doe", pan=PAN, status="Individual",
            books={"2025-26": "C:/books/BobDoeOld.gnucash"},
        ),
    }
    path = _write_entities(tmp_path, entities)
    reg.set_book("BobDoe", "2025-26", "C:/books/BobDoeNew.gnucash", entities_path=path)

    reloaded = configs.load_entities(path)
    assert reloaded["BobDoe"].books == {"2025-26": "C:/books/BobDoeNew.gnucash"}


def test_set_book_adds_fy_without_disturbing_others(tmp_path):
    entities = {
        "CarolDoe": configs.EntityProfile(
            key="CarolDoe", name="Carol Doe", pan=PAN, status="Individual",
            books={"2024-25": "C:/books/CarolDoe2425.gnucash"},
        ),
    }
    path = _write_entities(tmp_path, entities)
    reg.set_book("CarolDoe", "2025-26", "C:/books/CarolDoe2526.gnucash", entities_path=path)

    reloaded = configs.load_entities(path)
    assert reloaded["CarolDoe"].books == {
        "2024-25": "C:/books/CarolDoe2425.gnucash",
        "2025-26": "C:/books/CarolDoe2526.gnucash",
    }


def test_set_book_unknown_entity_raises(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
    }
    path = _write_entities(tmp_path, entities)
    with pytest.raises(ValueError):
        reg.set_book("NoSuchEntity", "2025-26", "C:/books/x.gnucash", entities_path=path)


@pytest.mark.parametrize("bad_fy", ["2025", "25-26", "2025-2026", "FY2025-26", ""])
def test_set_book_malformed_fy_rejected(tmp_path, bad_fy):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
    }
    path = _write_entities(tmp_path, entities)
    with pytest.raises(ValueError):
        reg.set_book("AliceDoe", bad_fy, "C:/books/x.gnucash", entities_path=path)


def test_set_book_does_not_disturb_unrelated_entity_block(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
        "DaveDoe": configs.EntityProfile(key="DaveDoe", name="Dave Doe", pan=PAN, status="Individual"),
    }
    path = _write_entities(tmp_path, entities)
    reg.set_book("AliceDoe", "2025-26", "C:/books/AliceDoe2526.gnucash", entities_path=path)

    reloaded = configs.load_entities(path)
    assert reloaded["DaveDoe"].books == {}
    assert reloaded["DaveDoe"].name == "Dave Doe"
