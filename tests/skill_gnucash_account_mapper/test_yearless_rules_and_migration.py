"""
tests/skill_gnucash_account_mapper/test_yearless_rules_and_migration.py --
Phase 0b "book registry" fix: the per-book mapping-rules sidecar YAML must
be named from the YEAR-LESS book stem so learned rules survive a book being
rolled to a new financial year, and any pre-existing year-stamped sidecar(s)
must be self-healed (merged + renamed to `*.migrated`) the first time the
year-less file is loaded.

Covers:
  - `_yearless_stem`: trailing-digit stripping across stem shapes.
  - `rules_path`: year-less sidecar naming across all resolution branches.
  - `_merge_rules_dicts`: union of sections, dedup-by-first-pattern,
    newest-`added`-wins, missing/unparseable-date handling, determinism.
  - `load_rules` end-to-end self-heal: synthetic legacy year-stamped files
    on disk get merged into the year-less file and renamed `*.migrated`,
    and a second load is idempotent (no re-migration, no duplication).

All data is synthetic (AliceDoe/BobDoe/CarolDoe/DaveDoe, BobDoeHUF, fake
bank keys BankA/BankB). Nothing under Data/ or any real .gnucash file is
touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import persistent_rules as pr  # noqa: E402


# ---------------------------------------------------------------------------
# _yearless_stem
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stem, expected",
    [
        ("AliceDoe2526", "AliceDoe"),
        ("BobDoeHUF2526", "BobDoeHUF"),
        ("NoDigitsHere", "NoDigitsHere"),
        ("Carol2Doe", "Carol2Doe"),          # mid-name digit, not trailing -> untouched
        ("DaveDoe25626", "DaveDoe"),          # 5-digit FY-style suffix
        ("alicedoe2526", "alicedoe"),         # case preserved (lowercase)
        ("AliceDoe", "AliceDoe"),             # no digits at all
        ("AliceDoe0", "AliceDoe"),            # single trailing digit
    ],
)
def test_yearless_stem(stem, expected):
    assert pr._yearless_stem(stem) == expected


# ---------------------------------------------------------------------------
# rules_path — year-less naming across resolution branches
#
# NOTE: pytest's tmp_path fixture itself resolves under the real OS temp
# directory on this machine (...\AppData\Local\Temp\pytest-of-...), which
# would make `_is_temp_dir` classify EVERY tmp_path-derived directory as
# "temp" via its substring heuristic. To exercise each resolution branch
# deterministically regardless of where tmp_path physically lives, these
# tests monkeypatch `_is_temp_dir` with an explicit "is this under my
# designated temp root" predicate instead of relying on the real path text.
# ---------------------------------------------------------------------------

def _mark_temp_under(*temp_roots: Path):
    """Return a fake `_is_temp_dir` that treats only paths under *temp_roots*
    as temp, regardless of the real filesystem location of tmp_path."""
    roots = [r.resolve() for r in temp_roots]

    def _fake(d: Path) -> bool:
        d = d.resolve()
        return any(d == r or r in d.parents for r in roots)

    return _fake


def test_rules_path_nontemp_colocate_is_yearless(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pr, "_is_temp_dir", lambda d: False)
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    gc = book_dir / "AliceDoe2526.gnucash"
    gc.touch()
    rp = pr.rules_path(str(gc))
    assert rp == book_dir / "AliceDoe_mapping_rules.yaml"


def test_rules_path_nontemp_huf_colocate_is_yearless(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pr, "_is_temp_dir", lambda d: False)
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    gc = book_dir / "BobDoeHUF2627.gnucash"
    gc.touch()
    rp = pr.rules_path(str(gc))
    assert rp == book_dir / "BobDoeHUF_mapping_rules.yaml"


def test_rules_path_nodigits_passthrough(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(pr, "_is_temp_dir", lambda d: False)
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    gc = book_dir / "NoDigitsHere.gnucash"
    gc.touch()
    rp = pr.rules_path(str(gc))
    assert rp == book_dir / "NoDigitsHere_mapping_rules.yaml"


def test_rules_path_scans_data_tree_for_original_uses_yearless_name(tmp_path: Path, monkeypatch):
    # Simulate a Data/ tree with the real (raw-stem) .gnucash book, and a
    # temp-looking upload path pointing at "the same" book by name.
    data_dir = tmp_path / "Data" / "books"
    data_dir.mkdir(parents=True)
    real_gc = data_dir / "CarolDoe2526.gnucash"
    real_gc.touch()

    temp_dir = tmp_path / "uploads" / "staging"
    temp_dir.mkdir(parents=True)
    temp_gc = temp_dir / "CarolDoe2526.gnucash"
    temp_gc.touch()

    monkeypatch.setattr(pr, "_is_temp_dir", _mark_temp_under(temp_dir))
    monkeypatch.chdir(tmp_path)
    pr._original_dir_cache.clear()

    rp = pr.rules_path(str(temp_gc))
    # Co-located with the ORIGINAL (found via raw-stem scan), year-less name.
    assert rp == data_dir / "CarolDoe_mapping_rules.yaml"
    pr._original_dir_cache.clear()


def test_rules_path_settings_fallback_uses_yearless_name(tmp_path: Path, monkeypatch):
    # No matching original book anywhere -> falls back to settings dir,
    # sidecar name still year-less.
    settings_dir = tmp_path / "Data" / "settings"
    settings_dir.mkdir(parents=True)
    config_path = settings_dir / "config.yaml"
    config_path.touch()

    temp_dir = tmp_path / "uploads" / "staging"
    temp_dir.mkdir(parents=True)
    temp_gc = temp_dir / "DaveDoe2526.gnucash"
    temp_gc.touch()

    monkeypatch.setattr(pr, "_is_temp_dir", _mark_temp_under(temp_dir))
    monkeypatch.chdir(tmp_path)
    pr._original_dir_cache.clear()

    rp = pr.rules_path(str(temp_gc), config_path=str(config_path))
    assert rp == settings_dir / "DaveDoe_mapping_rules.yaml"
    pr._original_dir_cache.clear()


def test_rules_path_temp_last_resort_uses_yearless_name(tmp_path: Path, monkeypatch):
    temp_dir = tmp_path / "uploads" / "staging"
    temp_dir.mkdir(parents=True)
    temp_gc = temp_dir / "AliceDoe2526.gnucash"
    temp_gc.touch()

    monkeypatch.setattr(pr, "_is_temp_dir", _mark_temp_under(temp_dir))
    monkeypatch.chdir(tmp_path)
    pr._original_dir_cache.clear()

    rp = pr.rules_path(str(temp_gc))  # no config_path -> branch 4
    assert rp == temp_dir / "AliceDoe_mapping_rules.yaml"
    pr._original_dir_cache.clear()


# ---------------------------------------------------------------------------
# _merge_rules_dicts
# ---------------------------------------------------------------------------

def _rule(pattern, account, added=None, **extra):
    d = {"patterns": [pattern], "account": account}
    if added is not None:
        d["added"] = added
    d.update(extra)
    return d


def test_merge_union_of_sections():
    d1 = {"_overrides": [_rule("PAT1", "Expenses:A")]}
    d2 = {"BankA": [_rule("PAT2", "Expenses:B")]}
    merged = pr._merge_rules_dicts([d1, d2])
    assert set(merged.keys()) == {"_overrides", "BankA"}
    assert merged["_overrides"][0]["account"] == "Expenses:A"
    assert merged["BankA"][0]["account"] == "Expenses:B"


def test_merge_dedup_by_first_pattern_newest_added_wins():
    d1 = {"BankA": [_rule("SELF", "Expenses:Old", added="2025-01-01")]}
    d2 = {"BankA": [_rule("SELF", "Expenses:New", added="2026-01-01")]}
    merged = pr._merge_rules_dicts([d1, d2])
    assert len(merged["BankA"]) == 1
    assert merged["BankA"][0]["account"] == "Expenses:New"


def test_merge_dedup_older_after_newer_keeps_newer_regardless_of_order():
    d1 = {"BankA": [_rule("SELF", "Expenses:New", added="2026-01-01")]}
    d2 = {"BankA": [_rule("SELF", "Expenses:Old", added="2025-01-01")]}
    merged = pr._merge_rules_dicts([d1, d2])
    assert merged["BankA"][0]["account"] == "Expenses:New"


def test_merge_missing_date_loses_to_dated_entry_either_order():
    dated = {"BankA": [_rule("SELF", "Expenses:Dated", added="2025-01-01")]}
    undated = {"BankA": [_rule("SELF", "Expenses:Undated")]}

    merged_a = pr._merge_rules_dicts([undated, dated])
    assert merged_a["BankA"][0]["account"] == "Expenses:Dated"

    merged_b = pr._merge_rules_dicts([dated, undated])
    assert merged_b["BankA"][0]["account"] == "Expenses:Dated"


def test_merge_all_undated_keeps_first_encountered():
    d1 = {"BankA": [_rule("SELF", "Expenses:First")]}
    d2 = {"BankA": [_rule("SELF", "Expenses:Second")]}
    merged = pr._merge_rules_dicts([d1, d2])
    assert merged["BankA"][0]["account"] == "Expenses:First"


def test_merge_unparseable_date_treated_as_missing():
    d1 = {"BankA": [_rule("SELF", "Expenses:Bad", added="not-a-date")]}
    d2 = {"BankA": [_rule("SELF", "Expenses:Good", added="2026-01-01")]}
    merged = pr._merge_rules_dicts([d1, d2])
    assert merged["BankA"][0]["account"] == "Expenses:Good"


def test_merge_no_collision_keeps_both():
    d1 = {"BankA": [_rule("PAT1", "Expenses:A")]}
    d2 = {"BankA": [_rule("PAT2", "Expenses:B")]}
    merged = pr._merge_rules_dicts([d1, d2])
    assert len(merged["BankA"]) == 2


def test_merge_deterministic_given_order():
    d1 = {"BankA": [_rule("SELF", "Expenses:X", added="2026-01-01")]}
    d2 = {"BankA": [_rule("SELF", "Expenses:Y", added="2026-01-01")]}  # same date -> tie
    merged_1 = pr._merge_rules_dicts([d1, d2])
    merged_2 = pr._merge_rules_dicts([d1, d2])
    assert merged_1 == merged_2 == {"BankA": [_rule("SELF", "Expenses:X", added="2026-01-01")]}


# ---------------------------------------------------------------------------
# load_rules end-to-end self-heal
# ---------------------------------------------------------------------------

def test_load_rules_selfheals_single_legacy_file(tmp_path: Path):
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    gc = book_dir / "AliceDoe2526.gnucash"
    gc.touch()

    legacy = book_dir / "AliceDoe2526_mapping_rules.yaml"
    legacy.write_text(
        yaml.dump({"BankA": [_rule("SELF", "Assets:Cash", added="2025-06-01")]}),
        encoding="utf-8",
    )

    rules = pr.load_rules(str(gc))
    assert rules["BankA"][0]["account"] == "Assets:Cash"

    yearless = book_dir / "AliceDoe_mapping_rules.yaml"
    assert yearless.exists()
    assert not legacy.exists()
    assert (book_dir / "AliceDoe2526_mapping_rules.yaml.migrated").exists()


def test_load_rules_selfheals_and_merges_multiple_legacy_files(tmp_path: Path):
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    gc = book_dir / "BobDoe2627.gnucash"
    gc.touch()

    legacy_old = book_dir / "BobDoe2425_mapping_rules.yaml"
    legacy_old.write_text(
        yaml.dump({
            "_overrides": [_rule("IMPS.*X", "Expenses:Old", added="2024-05-01")],
            "BankA": [_rule("SELF", "Assets:Cash", added="2024-05-01")],
        }),
        encoding="utf-8",
    )
    legacy_new = book_dir / "BobDoe2526_mapping_rules.yaml"
    legacy_new.write_text(
        yaml.dump({
            "_overrides": [_rule("IMPS.*X", "Expenses:New", added="2025-05-01")],
            "BankB": [_rule("SALARY", "Income:Salary", added="2025-05-01")],
        }),
        encoding="utf-8",
    )

    rules = pr.load_rules(str(gc))

    # dedup by first pattern across legacy files -> newest added wins
    assert rules["_overrides"][0]["account"] == "Expenses:New"
    # union of bank sections from both legacy files
    assert rules["BankA"][0]["account"] == "Assets:Cash"
    assert rules["BankB"][0]["account"] == "Income:Salary"

    yearless = book_dir / "BobDoe_mapping_rules.yaml"
    assert yearless.exists()
    assert not legacy_old.exists()
    assert not legacy_new.exists()
    assert legacy_old.with_name(legacy_old.name + ".migrated").exists()
    assert legacy_new.with_name(legacy_new.name + ".migrated").exists()


def test_load_rules_second_load_is_idempotent_no_remigration(tmp_path: Path):
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    gc = book_dir / "CarolDoe2526.gnucash"
    gc.touch()

    legacy = book_dir / "CarolDoe2526_mapping_rules.yaml"
    legacy.write_text(
        yaml.dump({"BankA": [_rule("SELF", "Assets:Cash", added="2025-06-01")]}),
        encoding="utf-8",
    )

    first = pr.load_rules(str(gc))
    migrated_marker = book_dir / "CarolDoe2526_mapping_rules.yaml.migrated"
    assert migrated_marker.exists()

    # Second load: no legacy files left to migrate, file already exists.
    second = pr.load_rules(str(gc))
    assert second == first
    assert len(second["BankA"]) == 1
    # Migrated marker untouched / not duplicated.
    assert migrated_marker.exists()
    assert not (book_dir / "CarolDoe2526_mapping_rules.yaml.migrated.migrated").exists()


def test_load_rules_no_legacy_no_yearless_returns_empty(tmp_path: Path):
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    gc = book_dir / "DaveDoe2526.gnucash"
    gc.touch()

    rules = pr.load_rules(str(gc))
    assert rules == {}
    assert not (book_dir / "DaveDoe_mapping_rules.yaml").exists()


def test_load_rules_skips_unreadable_legacy_file(tmp_path: Path, caplog):
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    gc = book_dir / "AliceDoe2526.gnucash"
    gc.touch()

    bad_legacy = book_dir / "AliceDoe2526_mapping_rules.yaml"
    bad_legacy.write_text("not: [valid: yaml: at all::", encoding="utf-8")

    rules = pr.load_rules(str(gc))
    assert rules == {}
    # Unreadable legacy file left alone (not migrated, not renamed) since
    # nothing valid was found to migrate.
    assert bad_legacy.exists()
    assert not (book_dir / "AliceDoe_mapping_rules.yaml").exists()


def test_load_rules_yearless_file_already_present_skips_migration(tmp_path: Path):
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    gc = book_dir / "AliceDoe2627.gnucash"
    gc.touch()

    yearless = book_dir / "AliceDoe_mapping_rules.yaml"
    yearless.write_text(
        yaml.dump({"BankA": [_rule("SELF", "Assets:Current", added="2026-01-01")]}),
        encoding="utf-8",
    )
    legacy = book_dir / "AliceDoe2526_mapping_rules.yaml"
    legacy.write_text(
        yaml.dump({"BankA": [_rule("OTHER", "Assets:Stale", added="2025-01-01")]}),
        encoding="utf-8",
    )

    rules = pr.load_rules(str(gc))
    # Legacy file untouched (not merged in) because year-less file already existed.
    assert rules["BankA"][0]["account"] == "Assets:Current"
    assert len(rules["BankA"]) == 1
    assert legacy.exists()
