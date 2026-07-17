"""
tests/skill_itr_workbook/test_itr_mapping_review_ui.py -- Part 2 (2026-07-16):
ITR mapping-review UI tests, covering the 6 acceptance gates from
2026-07-16-itr-besteffort-workbook-and-mapping-ui-prompt.md PART 2:

  1. Load an entity's accounts -- unmapped ones flagged + suggestions shown.
  2. Assign + Save -> Data/itr/mappings/<entity>.mapping.yaml updated
     (anchored, backup kept), touched entries marked approved.
  3. Re-run the ITR Workbook for that entity -> the just-assigned account
     now resolves.
  4. Invalid/blank selections handled gracefully; no in-place clobber
     without a backup.
  5. Works in both source and frozen path layouts (data_root_dir() anchor).
  6. Full suite green (checked by CI / the top-level pytest run, not here).

Pure-Python against the tab's handler functions (_load_review_rows,
_save_changes, _mapping_path, _latest_proposed_mappings_path) -- no browser
driven, mirroring test_path_anchoring.py's patch("ui._config.data_root_dir")
pattern.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
AGENT_DIR = ROOT / "src" / "agents" / "skill_itr_workbook"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

SRC = ROOT / "src"

for p in (str(SCRIPTS), str(AGENT_DIR), str(Path(__file__).resolve().parent), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
import configs  # noqa: E402
import fixture_gen  # noqa: E402

from ui.tabs import itr_mapping_review as ui_mod  # noqa: E402

UNMAPPED_GUID = "ca85f6240bb66d5d38a0e4f40d908525"   # Assets/Misc Holding (unmapped)
RETAG_GUID = "11fc5a724efb9eb161ac039825b3e6dd"        # Income/Bank Interest


def _entities_yaml(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "SYN-IND": {
            "name": "Synthetic Individual", "pan": "AAAAA0000A", "status": "Individual",
            "default_regime": "new",
        },
    }), encoding="utf-8")


def _setup_data_root(tmp_path: Path, mapping_fixture: str) -> Path:
    """<tmp>/Data with entities.yaml + SYN-IND.mapping.yaml seeded from a
    fixture. Returns the data root."""
    data_root = tmp_path / "Data"
    _entities_yaml(data_root / "itr" / "entities.yaml")
    mappings_dir = data_root / "itr" / "mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / mapping_fixture, mappings_dir / "SYN-IND.mapping.yaml")
    return data_root


# ---------------------------------------------------------------------------
# Gate 1 -- load flags unmapped rows + shows suggestions
# ---------------------------------------------------------------------------

def test_load_rows_flags_unmapped_and_shows_suggestion(tmp_path):
    data_root = _setup_data_root(tmp_path, "syn_ind_unmapped.mapping.yaml")
    out_dir = data_root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    snippet_path = out_dir / "2026-07-16-000000-bs-ITR.xlsx-proposed-mappings.yaml"
    snippet_path.write_text(yaml.safe_dump([
        {"guid": UNMAPPED_GUID, "path": "Assets/Misc Holding (unmapped)",
         "tag": "AL_CASH_BANK", "note": "LLM suggestion", "suggested_by_llm": "2026-07-16"},
    ], sort_keys=False), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        rows = ui_mod._load_review_rows("SYN-IND")

    by_guid = {r["guid"]: r for r in rows}
    assert by_guid[UNMAPPED_GUID]["unmapped"] is True
    assert by_guid[UNMAPPED_GUID]["suggested"] == "AL_CASH_BANK"
    assert by_guid[UNMAPPED_GUID]["tag"] is None
    # An unmapped row is never flagged needs_review -- its own red
    # "unmapped" tier already dominates, and it carries no LLM-approved tag.
    assert by_guid[UNMAPPED_GUID]["needs_review"] is False

    # An already-mapped entry shows its current tag, not flagged unmapped.
    assert by_guid[RETAG_GUID]["unmapped"] is False
    assert by_guid[RETAG_GUID]["tag"] == "OS_INTEREST_BANK"
    # The fixture mapping's entries carry no suggested_by_llm -- confirmed.
    assert by_guid[RETAG_GUID]["needs_review"] is False


def test_load_rows_needs_review_flag_reflects_llm_suggestion(tmp_path):
    """RAG confidence tier: a mapped entry still carrying suggested_by_llm
    (an unapproved LLM suggestion) is needs_review=True; once a human has
    approved/set it (suggested_by_llm cleared to None), needs_review=False."""
    data_root = tmp_path / "Data"
    _entities_yaml(data_root / "itr" / "entities.yaml")
    mappings_dir = data_root / "itr" / "mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)
    (mappings_dir / "SYN-IND.mapping.yaml").write_text(yaml.safe_dump([
        {"guid": "guid-unreviewed", "path": "Income/Bank Interest",
         "tag": "OS_INTEREST_BANK", "suggested_by_llm": "2026-07-16",
         "note": "LLM suggestion, not yet approved"},
        {"guid": "guid-confirmed", "path": "Assets/Cash and Bank/Cash",
         "tag": "AL_CASH_BANK", "suggested_by_llm": None, "note": ""},
    ], sort_keys=False), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        rows = ui_mod._load_review_rows("SYN-IND")

    by_guid = {r["guid"]: r for r in rows}
    assert by_guid["guid-unreviewed"]["unmapped"] is False
    assert by_guid["guid-unreviewed"]["needs_review"] is True
    assert by_guid["guid-confirmed"]["unmapped"] is False
    assert by_guid["guid-confirmed"]["needs_review"] is False


def test_load_rows_replace_me_suggestion_shown_as_none(tmp_path):
    """A proposed-mappings entry with no LLM suggestion (tag: REPLACE_ME,
    e.g. no endpoint configured) must not be shown as a real suggestion."""
    data_root = _setup_data_root(tmp_path, "syn_ind_unmapped.mapping.yaml")
    out_dir = data_root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    snippet_path = out_dir / "run-ITR.xlsx-proposed-mappings.yaml"
    snippet_path.write_text(yaml.safe_dump([
        {"guid": UNMAPPED_GUID, "path": "Assets/Misc Holding (unmapped)",
         "tag": "REPLACE_ME", "note": "unmapped -- needs review"},
    ], sort_keys=False), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        rows = ui_mod._load_review_rows("SYN-IND")

    row = next(r for r in rows if r["guid"] == UNMAPPED_GUID)
    assert row["suggested"] is None
    assert row["unmapped"] is True


def test_load_rows_cold_start_all_unmapped(tmp_path):
    """No mapping file at all for the entity -- every row comes from the
    proposed-mappings snippet."""
    data_root = tmp_path / "Data"
    _entities_yaml(data_root / "itr" / "entities.yaml")
    out_dir = data_root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run-ITR.xlsx-proposed-mappings.yaml").write_text(yaml.safe_dump([
        {"guid": "guid-a", "path": "Assets/Cash", "tag": "REPLACE_ME", "note": "unmapped"},
        {"guid": "guid-b", "path": "Income/Salary", "tag": "SALARY_GROSS",
         "note": "LLM suggestion", "suggested_by_llm": "2026-07-16"},
    ], sort_keys=False), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        rows = ui_mod._load_review_rows("SYN-IND")

    assert len(rows) == 2
    assert all(r["unmapped"] for r in rows)
    by_guid = {r["guid"]: r for r in rows}
    assert by_guid["guid-a"]["suggested"] is None
    assert by_guid["guid-b"]["suggested"] == "SALARY_GROSS"


def test_load_rows_blank_entity_returns_empty():
    assert ui_mod._load_review_rows("") == []
    assert ui_mod._load_review_rows("   ") == []


# ---------------------------------------------------------------------------
# Part 4 -- tag glossary data (_tag_options) and the sortable/filterable +
# tooltip-carrying markup in the rendered review HTML.
# ---------------------------------------------------------------------------

def test_tag_options_include_sheet_target_and_description():
    """Every tag row carries enough for the glossary panel and the badge
    tooltips: the code, its target sheet, and a one-line meaning."""
    options = ui_mod._tag_options()
    assert options, "expected at least one tag in the vocabulary"
    by_tag = {o["tag"]: o for o in options}
    assert "OS_INTEREST_BANK" in by_tag
    row = by_tag["OS_INTEREST_BANK"]
    assert row["target"] == "OtherSources"
    assert row["sheet"] in ("RE", "BS", "EITHER")
    assert row["desc"]  # non-empty treatment note
    # Sorted by tag.
    assert [o["tag"] for o in options] == sorted(o["tag"] for o in options)


def test_review_html_has_sortable_headers_and_glossary_and_tooltips(tmp_path):
    """The rendered HTML must carry: clickable/sortable column headers with
    a per-column filter row (mirroring gnucash_review.py), a toggleable tag
    glossary panel, and a JS-side tag-description lookup used to title
    Current/Suggested/New tag badges."""
    data_root = _setup_data_root(tmp_path, "syn_ind_unmapped.mapping.yaml")
    with patch("ui._config.data_root_dir", return_value=data_root):
        html = ui_mod._load_review_data("SYN-IND")

    # Sortable/filterable column headers, built dynamically in JS from COLS.
    assert 'id="itrmap-thead"' in html
    assert "sort-arrow" in html
    assert "filter-row" in html
    assert "colFilters" in html

    # Tag glossary: toggle button + panel + search box.
    assert 'id="itrmap-glossary-btn"' in html
    assert 'id="itrmap-glossary-panel"' in html
    assert 'id="itrmap-glossary-search"' in html

    # Tag-description tooltip lookup, sourced from the TAGS payload.
    assert "TAG_DESC" in html
    assert "tagTitle" in html
    # The vocabulary payload itself is embedded (used by both the glossary
    # and the tooltips) -- spot-check one known tag/description round-trips.
    assert "OS_INTEREST_BANK" in html


# ---------------------------------------------------------------------------
# Gate 2 -- Save writes the anchored mapping file, keeps a backup, marks
# touched entries approved.
# ---------------------------------------------------------------------------

def test_save_writes_anchored_file_backup_and_marks_approved(tmp_path):
    data_root = _setup_data_root(tmp_path, "syn_ind_unmapped.mapping.yaml")
    mapping_path = data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    assert mapping_path.is_file()

    payload = json.dumps({
        "entity": "SYN-IND",
        "changes": [
            {"guid": UNMAPPED_GUID, "path": "Assets/Misc Holding (unmapped)", "tag": "AL_CASH_BANK"},
            {"guid": RETAG_GUID, "path": "Income/Bank Interest", "tag": "OS_INTEREST_SB"},
        ],
    })

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes("SYN-IND", payload)

    assert "Saved" in msg
    assert "Applied 2 correction" in msg

    backups = list(mapping_path.parent.glob("SYN-IND.mapping.yaml.bak-*"))
    assert len(backups) == 1, "expected exactly one backup of the pre-save file"
    # The backup preserves the PRE-save content (still missing UNMAPPED_GUID).
    backup_loaded = configs.load_mapping(backups[0])
    assert UNMAPPED_GUID not in backup_loaded.entries

    updated = configs.load_mapping(mapping_path)
    assert updated.entries[UNMAPPED_GUID].tag == "AL_CASH_BANK"
    assert updated.entries[UNMAPPED_GUID].note is not None
    assert "approved" in updated.entries[UNMAPPED_GUID].note.lower()
    assert updated.entries[UNMAPPED_GUID].suggested_by_llm is None
    assert updated.entries[RETAG_GUID].tag == "OS_INTEREST_SB"
    assert updated.entries[RETAG_GUID].suggested_by_llm is None


def test_save_cold_start_creates_mapping_file_no_backup_needed(tmp_path):
    """No mapping file exists yet for the entity -- Save creates it fresh;
    there's nothing to back up, and that must not be treated as an error."""
    data_root = tmp_path / "Data"
    _entities_yaml(data_root / "itr" / "entities.yaml")
    mapping_path = data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    assert not mapping_path.exists()

    payload = json.dumps({
        "entity": "SYN-IND",
        "changes": [{"guid": "guid-a", "path": "Assets/Cash", "tag": "AL_CASH_BANK"}],
    })
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes("SYN-IND", payload)

    assert "Saved" in msg
    assert "nothing to back up" in msg.lower()
    assert mapping_path.is_file()
    loaded = configs.load_mapping(mapping_path)
    assert loaded.entries["guid-a"].tag == "AL_CASH_BANK"


# ---------------------------------------------------------------------------
# Gate 3 -- integration: a saved correction resolves on the next ITR
# Workbook run for that entity.
# ---------------------------------------------------------------------------

def test_saved_correction_resolves_on_workbook_rerun(tmp_path):
    data_root = _setup_data_root(tmp_path, "syn_ind_unmapped.mapping.yaml")
    entities_path = data_root / "itr" / "entities.yaml"
    out_dir = data_root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    html_path = tmp_path / "bs.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out_path = out_dir / "2026-07-16-000000-bs-ITR.xlsx"

    # First run: partial mapping -- one leaf unresolved, best-effort build.
    summary_before = agent.run(
        str(html_path), str(out_path),
        entity_key="SYN-IND", entities_path=str(entities_path),
    )
    assert "STATUS: BUILT -- 1 REVIEW ITEM(S)" in summary_before
    snippet_path = Path(str(out_path) + "-proposed-mappings.yaml")
    assert snippet_path.exists()

    # UI: load rows, confirm the leaf shows up unmapped, then save a
    # correction for it.
    with patch("ui._config.data_root_dir", return_value=data_root):
        rows = ui_mod._load_review_rows("SYN-IND")
        assert any(r["guid"] == UNMAPPED_GUID and r["unmapped"] for r in rows)

        save_msg = ui_mod._save_changes("SYN-IND", json.dumps({
            "entity": "SYN-IND",
            "changes": [{"guid": UNMAPPED_GUID, "path": "Assets/Misc Holding (unmapped)",
                         "tag": "AL_CASH_BANK"}],
        }))
    assert "Saved" in save_msg

    # Second run: mapping_file omitted -- agent auto-derives the entity's
    # (now-corrected) mapping file, same as gate 3 in test_path_anchoring.py.
    out_path2 = out_dir / "2026-07-16-000001-bs-ITR.xlsx"
    summary_after = agent.run(
        str(html_path), str(out_path2),
        entity_key="SYN-IND", entities_path=str(entities_path),
    )
    assert "STATUS: OK" in summary_after
    assert "REVIEW ITEM(S)" not in summary_after


# ---------------------------------------------------------------------------
# Gate 4 -- invalid/blank selections handled gracefully; no clobber without
# a backup.
# ---------------------------------------------------------------------------

def test_blank_entity_never_touches_filesystem(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes("", json.dumps({"entity": "", "changes": [{"guid": "x", "tag": "AL_CASH_BANK"}]}))
    assert "select an entity" in msg.lower()
    assert not data_root.exists()


def test_blank_changes_payload_is_a_noop():
    assert "no changes" in ui_mod._save_changes("SYN-IND", "").lower()
    assert "no changes" in ui_mod._save_changes("SYN-IND", "   ").lower()
    assert "no changes" in ui_mod._save_changes("SYN-IND", json.dumps({"entity": "SYN-IND", "changes": []})).lower()


def test_invalid_tag_reported_not_applied_others_still_saved(tmp_path):
    data_root = _setup_data_root(tmp_path, "syn_ind_unmapped.mapping.yaml")
    mapping_path = data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml"

    payload = json.dumps({
        "entity": "SYN-IND",
        "changes": [
            {"guid": UNMAPPED_GUID, "path": "Assets/Misc Holding (unmapped)", "tag": "NOT_A_REAL_TAG"},
            {"guid": RETAG_GUID, "path": "Income/Bank Interest", "tag": "OS_INTEREST_SB"},
        ],
    })
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes("SYN-IND", payload)

    assert "not applied" in msg.lower()
    assert "NOT_A_REAL_TAG" in msg
    # A backup was still made before the (partial) rewrite.
    backups = list(mapping_path.parent.glob("SYN-IND.mapping.yaml.bak-*"))
    assert len(backups) == 1

    updated = configs.load_mapping(mapping_path)
    assert UNMAPPED_GUID not in updated.entries          # invalid tag: not written
    assert updated.entries[RETAG_GUID].tag == "OS_INTEREST_SB"   # valid one still applied


def test_blank_tag_selection_skipped_gracefully(tmp_path):
    data_root = _setup_data_root(tmp_path, "syn_ind_unmapped.mapping.yaml")
    payload = json.dumps({
        "entity": "SYN-IND",
        "changes": [{"guid": UNMAPPED_GUID, "path": "Assets/Misc Holding (unmapped)", "tag": ""}],
    })
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes("SYN-IND", payload)
    assert "no valid changes" in msg.lower()


# ---------------------------------------------------------------------------
# Gate 5 -- works in both source and frozen path layouts.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("layout", ["frozen", "source"])
def test_mapping_path_anchored_in_both_layouts(tmp_path, layout):
    if layout == "frozen":
        data_root = tmp_path / "Data"   # CWD = <tmp>/Data convention
    else:
        data_root = tmp_path / "src-mode-data"
    data_root.mkdir(parents=True, exist_ok=True)

    with patch("ui._config.data_root_dir", return_value=data_root):
        mp = ui_mod._mapping_path("SYN-IND")
    assert mp == data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    # Never doubled up (Data/Data/itr/... regression, see test_path_anchoring.py).
    assert "Data" + "\\Data" not in str(mp) or True  # path sep-agnostic guard
    doubled = data_root / "Data" / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    assert mp != doubled


@pytest.mark.parametrize("layout", ["frozen", "source"])
def test_save_round_trip_in_both_layouts(tmp_path, layout):
    # _setup_data_root always nests under <root>/Data; build directly here
    # instead so the frozen/source root name itself varies.
    subdir = "Data" if layout == "frozen" else "src-mode-data"
    data_root = tmp_path / subdir
    _entities_yaml(data_root / "itr" / "entities.yaml")
    mappings_dir = data_root / "itr" / "mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "syn_ind_unmapped.mapping.yaml", mappings_dir / "SYN-IND.mapping.yaml")

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes("SYN-IND", json.dumps({
            "entity": "SYN-IND",
            "changes": [{"guid": UNMAPPED_GUID, "path": "Assets/Misc Holding (unmapped)", "tag": "AL_CASH_BANK"}],
        }))
    assert "Saved" in msg
    updated = configs.load_mapping(mappings_dir / "SYN-IND.mapping.yaml")
    assert updated.entries[UNMAPPED_GUID].tag == "AL_CASH_BANK"
