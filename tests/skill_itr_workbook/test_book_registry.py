"""
tests/skill_itr_workbook/test_book_registry.py -- Phase 1 "book registry"
pure-logic groundwork (GnuCash book as single source of truth architecture,
2026-07-29 handover).

Covers:
  - derive_workbook_match(): filename-stem year-stripping (trailing digit
    run only; HUF suffix preserved; no-trailing-digit passthrough).
  - EntityProfile.books round-trip through load_entities()/dump_entities()
    (_entity_to_dict): non-empty emits a `books:` block, empty stays absent
    (byte-identical to a profile with no books field at all).
  - effective_workbook_match(): explicit-override / per-FY / newest-FY-
    fallback / None precedence.

Synthetic names only (AliceDoe/BobDoe/CarolDoe/DaveDoe/BobDoeHUF) -- no
real family names, PANs, or data files are read.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
for p in (str(SCRIPTS),):
    if p not in sys.path:
        sys.path.insert(0, p)

import configs  # noqa: E402


# ---------------------------------------------------------------------------
# derive_workbook_match
# ---------------------------------------------------------------------------

def test_derive_workbook_match_strips_trailing_year_digits():
    assert configs.derive_workbook_match("AliceDoe25626.gnucash") == "AliceDoe"
    assert configs.derive_workbook_match("BobDoe2526.gnucash") == "BobDoe"
    assert configs.derive_workbook_match("CarolDoe2526.gnucash") == "CarolDoe"


def test_derive_workbook_match_keeps_huf_suffix():
    assert configs.derive_workbook_match("BobDoeHUF2526.gnucash") == "BobDoeHUF"
    assert configs.derive_workbook_match("DaveDoeHUF25.gnucash") == "DaveDoeHUF"


def test_derive_workbook_match_no_trailing_digits_passthrough():
    assert configs.derive_workbook_match("AliceDoe.gnucash") == "AliceDoe"
    assert configs.derive_workbook_match("NoDigitsHere.gnucash") == "NoDigitsHere"


def test_derive_workbook_match_only_strips_trailing_digit_run():
    # Digits followed by letters are NOT stripped -- only a genuine trailing
    # run of digits at the very end of the stem counts as a year suffix.
    assert configs.derive_workbook_match("Alice2Doe2526.gnucash") == "Alice2Doe"
    assert configs.derive_workbook_match("Alice2Doe.gnucash") == "Alice2Doe"


def test_derive_workbook_match_accepts_full_path_or_bare_name():
    full = str(Path("C:/some/dir/CarolDoe2526.gnucash"))
    assert configs.derive_workbook_match(full) == "CarolDoe"
    assert configs.derive_workbook_match(Path(full)) == "CarolDoe"
    assert configs.derive_workbook_match("CarolDoe2526") == "CarolDoe"  # bare name, no ext


def test_derive_workbook_match_never_lowercases():
    result = configs.derive_workbook_match("DaveDoe2526.gnucash")
    assert result == "DaveDoe"
    assert result != result.lower()


# ---------------------------------------------------------------------------
# EntityProfile.books round-trip
# ---------------------------------------------------------------------------

def test_books_round_trips_through_dump_and_load(tmp_path):
    entities = {
        "SYN-BOOKS": configs.EntityProfile(
            key="SYN-BOOKS", name="Synthetic Books Owner", pan="AAAAA0000A",
            status="Individual",
            books={
                "2024-25": "C:/books/AliceDoe2425.gnucash",
                "2025-26": "C:/books/AliceDoe2526.gnucash",
            },
        ),
    }
    text = configs.dump_entities(entities)
    assert "books:" in text

    out = tmp_path / "entities.yaml"
    out.write_text(text, encoding="utf-8")
    loaded = configs.load_entities(out)

    assert loaded["SYN-BOOKS"].books == entities["SYN-BOOKS"].books


def test_books_absent_when_empty_stays_absent_in_dump():
    e = configs.EntityProfile(key="SYN-NOBOOKS", name="No Books", pan="BBBBB1111B", status="Individual")
    text = configs.dump_entities({"SYN-NOBOOKS": e})
    assert "books:" not in text


def test_books_default_is_empty_dict_on_load(tmp_path):
    """An entity without a `books:` key in the YAML loads with books == {}
    (tolerant of absent/None -- never a KeyError/AttributeError)."""
    out = tmp_path / "entities.yaml"
    out.write_text(
        "SYN-PLAIN:\n"
        "  name: Plain Entity\n"
        "  pan: CCCCC2222C\n"
        "  status: Individual\n",
        encoding="utf-8",
    )
    loaded = configs.load_entities(out)
    assert loaded["SYN-PLAIN"].books == {}


def test_books_tolerates_explicit_null_in_yaml(tmp_path):
    out = tmp_path / "entities.yaml"
    out.write_text(
        "SYN-NULLBOOKS:\n"
        "  name: Null Books\n"
        "  pan: DDDDD3333D\n"
        "  status: Individual\n"
        "  books:\n",
        encoding="utf-8",
    )
    loaded = configs.load_entities(out)
    assert loaded["SYN-NULLBOOKS"].books == {}


def test_editing_books_leaves_other_entity_byte_stable():
    """Mirrors the existing byte-stability gate in test_configs_entities.py:
    adding books to one entity must not perturb an unrelated entity's
    emitted block."""
    entities = {
        "SYN-A": configs.EntityProfile(key="SYN-A", name="Carol Doe", pan="EEEEE4444E", status="Individual"),
        "SYN-B": configs.EntityProfile(key="SYN-B", name="Dave Doe", pan="FFFFF5555F", status="Individual"),
    }
    text_before = configs.dump_entities(entities)

    def _block(text: str, key: str) -> str:
        lines = text.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln == f"{key}:")
        end = start + 1
        while end < len(lines) and (lines[end].startswith("  ") or not lines[end].strip()):
            end += 1
        return "\n".join(lines[start:end])

    b_before = _block(text_before, "SYN-B")

    entities["SYN-A"].books = {"2025-26": "C:/books/CarolDoe2526.gnucash"}
    text_after = configs.dump_entities(entities)
    b_after = _block(text_after, "SYN-B")

    assert b_before == b_after
    assert "books:" in text_after


# ---------------------------------------------------------------------------
# effective_workbook_match
# ---------------------------------------------------------------------------

def test_effective_workbook_match_explicit_override_wins():
    e = configs.EntityProfile(
        key="SYN-OVR", name="Override", pan="AAAAA0000A", status="Individual",
        workbook_match="ExplicitToken",
        books={"2025-26": "C:/books/BobDoe2526.gnucash"},
    )
    assert configs.effective_workbook_match(e) == "ExplicitToken"
    assert configs.effective_workbook_match(e, fy="2025-26") == "ExplicitToken"


def test_effective_workbook_match_uses_books_for_given_fy():
    e = configs.EntityProfile(
        key="SYN-FY", name="FY Pick", pan="BBBBB1111B", status="Individual",
        books={
            "2024-25": "C:/books/BobDoe2425.gnucash",
            "2025-26": "C:/books/BobDoe2526.gnucash",
        },
    )
    assert configs.effective_workbook_match(e, fy="2024-25") == "BobDoe"
    assert configs.effective_workbook_match(e, fy="2025-26") == "BobDoe"


def test_effective_workbook_match_falls_back_to_newest_fy_when_no_fy_given():
    e = configs.EntityProfile(
        key="SYN-NEWEST", name="Newest Pick", pan="CCCCC2222C", status="Individual",
        books={
            "2023-24": "C:/books/CarolDoe2324.gnucash",
            "2025-26": "C:/books/CarolDoe2526.gnucash",
            "2024-25": "C:/books/CarolDoe2425.gnucash",
        },
    )
    assert configs.effective_workbook_match(e) == "CarolDoe"


def test_effective_workbook_match_falls_back_to_newest_fy_when_fy_not_present():
    e = configs.EntityProfile(
        key="SYN-MISS", name="Missing FY", pan="DDDDD3333D", status="Individual",
        books={
            "2024-25": "C:/books/DaveDoe2425.gnucash",
            "2025-26": "C:/books/DaveDoe2526.gnucash",
        },
    )
    # fy="2026-27" not in books -> falls back to newest available (2025-26)
    assert configs.effective_workbook_match(e, fy="2026-27") == "DaveDoe"


def test_effective_workbook_match_none_when_nothing_configured():
    e = configs.EntityProfile(key="SYN-EMPTY", name="Empty", pan="FFFFF5555F", status="Individual")
    assert configs.effective_workbook_match(e) is None
    assert configs.effective_workbook_match(e, fy="2025-26") is None


def test_effective_workbook_match_huf_variant_preserved():
    e = configs.EntityProfile(
        key="SYN-HUF", name="Bob Doe HUF", pan="AAAAA0000A", status="HUF",
        books={"2025-26": "C:/books/BobDoeHUF2526.gnucash"},
    )
    assert configs.effective_workbook_match(e) == "BobDoeHUF"
