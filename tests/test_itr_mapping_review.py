"""
tests/test_itr_mapping_review.py -- regression guards for the migrated ITR
Mapping review tab (ui/tabs/itr_mapping_review.py), now built on the shared
ui._review_engine.

Covers what tests/skill_itr_workbook/test_itr_mapping_review_ui.py doesn't
(that file exercises _load_review_rows/_save_changes business logic against
real ITR-Workbook fixtures; this file exercises the render path + the
engine-shaped save payload in isolation):

  - the full render path end to end (a render-path test is mandatory here
    precisely because a prior migration in this series shipped 17 green
    tests while the screen crashed on open with a NameError -- nothing
    exercised the render path);
  - hostile row content (path/note/tag) never reaching the DOM as live markup;
  - _row_presentation's _tags/_rowclass/_badges for all three RAG tiers
    (unmapped/needs_review/confirmed) plus the suggested-tag badge;
  - _save_changes reading the engine's payload shape ({context, changes,
    all_rows}, each change carrying _idx/_orig/declared columns/guid) rather
    than the old {entity, changes:[{guid,path,tag}]} shape;
  - the glossary panel lives in extra_panel_html, not a hand-rolled template.

All data here is synthetic -- no content from the gitignored Data/ directory
appears in this file, per the project's PII rule for committed tests.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for p in (str(ROOT), str(ROOT / "ui"), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from ui.tabs import itr_mapping_review as ui_mod  # noqa: E402


def _entities_yaml(path: Path) -> None:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "SYN-IND": {"name": "Synthetic Individual", "pan": "AAAAA0000A",
                    "status": "Individual", "default_regime": "new",
                    "workbook_match": "SYN-IND"},
    }), encoding="utf-8")


def _setup_data_root(tmp_path: Path) -> Path:
    data_root = tmp_path / "Data"
    _entities_yaml(data_root / "itr" / "entities.yaml")
    (data_root / "itr" / "mappings").mkdir(parents=True, exist_ok=True)
    return data_root


# ---------------------------------------------------------------------------
# Render path -- mandatory: must actually build the HTML, not just call
# helper functions in isolation.
# ---------------------------------------------------------------------------

def test_load_review_data_render_path_has_no_residual_tokens(tmp_path):
    data_root = _setup_data_root(tmp_path)
    import yaml
    (data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml").write_text(yaml.safe_dump([
        {"guid": "g1", "path": "Income/Bank Interest", "tag": "OS_INTEREST_BANK",
         "suggested_by_llm": None, "note": ""},
    ], sort_keys=False), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        html = ui_mod._load_review_data("SYN-IND")

    assert re.findall(r"%%[A-Z_]*%%", html) == []
    assert "<table" in html
    assert 'id="itrmap-app"' in html


def test_load_review_data_hostile_content_not_live_markup(tmp_path):
    data_root = _setup_data_root(tmp_path)
    import yaml
    hostile = "<img src=x onerror=alert(1)>"
    (data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml").write_text(yaml.safe_dump([
        {"guid": "g1", "path": hostile, "tag": "OS_INTEREST_BANK",
         "suggested_by_llm": None, "note": hostile},
    ], sort_keys=False), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        html = ui_mod._load_review_data("SYN-IND")

    assert hostile not in html


def test_load_review_data_blank_entity_is_safe():
    html = ui_mod._load_review_data("")
    assert "Select an entity" in html


def test_load_review_data_no_rows_reports_clearly(tmp_path):
    data_root = _setup_data_root(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        html = ui_mod._load_review_data("SYN-IND")
    assert "No accounts found" in html


def test_load_review_data_glossary_in_extra_panel_not_hand_rolled_template(tmp_path):
    """The glossary must come from spec.extra_panel_html (a <details> block),
    not a re-implemented %%TOKEN%% template or a second bespoke search box."""
    data_root = _setup_data_root(tmp_path)
    import yaml
    (data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml").write_text(yaml.safe_dump([
        {"guid": "g1", "path": "Income/Bank Interest", "tag": "OS_INTEREST_BANK",
         "suggested_by_llm": None, "note": ""},
    ], sort_keys=False), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        html = ui_mod._load_review_data("SYN-IND")

    assert "<details" in html and "<summary" in html
    assert "Tag glossary" in html
    # The old hand-rolled glossary widget's ids/behaviour must be gone.
    assert "itrmap-glossary-btn" not in html
    assert "itrmap-glossary-search" not in html


# ---------------------------------------------------------------------------
# _row_presentation -- RAG tiers + suggested badge
# ---------------------------------------------------------------------------

_TAG_DESC = {"OS_INTEREST_BANK": "Bank savings interest -> Other Sources", "AL_CASH_BANK": "Cash/bank balance"}


def test_row_presentation_unmapped_is_red_with_unmapped_badge():
    row = {"unmapped": True, "needs_review": False, "tag": None, "suggested": None, "note": ""}
    ui_mod._row_presentation(row, _TAG_DESC)
    assert row["_tags"] == ["unmapped"]
    assert row["_rowclass"] == "accent-red"
    assert row["_badges"]["tag"]["text"] == "UNMAPPED"
    assert row["_badges"]["tag"]["cls"] == "red"


def test_row_presentation_needs_review_is_amber_and_tagged_mapped_too():
    row = {"unmapped": False, "needs_review": True, "tag": "OS_INTEREST_BANK", "suggested": None, "note": ""}
    ui_mod._row_presentation(row, _TAG_DESC)
    assert row["_tags"] == ["needs_review", "mapped"]
    assert row["_rowclass"] == "accent-amber"
    assert row["_badges"]["tag"]["text"] == "NEEDS REVIEW"
    assert "OS_INTEREST_BANK --" in row["_badges"]["tag"]["title"]


def test_row_presentation_confirmed_is_green_and_tagged_mapped_too():
    row = {"unmapped": False, "needs_review": False, "tag": "AL_CASH_BANK", "suggested": None, "note": ""}
    ui_mod._row_presentation(row, _TAG_DESC)
    assert row["_tags"] == ["confirmed", "mapped"]
    assert row["_rowclass"] == "accent-green"
    assert row["_badges"]["tag"]["text"] == "CONFIRMED"


def test_row_presentation_suggested_gets_blue_badge_on_suggested_column():
    row = {"unmapped": True, "needs_review": False, "tag": None, "suggested": "AL_CASH_BANK", "note": ""}
    ui_mod._row_presentation(row, _TAG_DESC)
    assert row["_badges"]["suggested"]["text"] == "SUGGESTED"
    assert row["_badges"]["suggested"]["cls"] == "blue"
    assert "AL_CASH_BANK --" in row["_badges"]["suggested"]["title"]


def test_row_presentation_note_carried_through():
    row = {"unmapped": False, "needs_review": False, "tag": "AL_CASH_BANK", "suggested": None, "note": "hello"}
    ui_mod._row_presentation(row, _TAG_DESC)
    assert row["_note"] == "hello"


# ---------------------------------------------------------------------------
# _save_changes -- the engine's payload shape, not the old {entity, changes:
# [{guid,path,tag}]} shape.
# ---------------------------------------------------------------------------

def _engine_payload(entity_key: str, changes: list[dict]) -> str:
    """Build a payload exactly like the engine's syncPayload() would."""
    out_changes = []
    for i, ch in enumerate(changes):
        row = {"_idx": i, "_orig": ch.get("_orig", "")}
        row["path"] = ch.get("path", "")
        row["tag"] = ch.get("tag", "")
        row["suggested"] = ch.get("suggested", "")
        if "guid" in ch:
            row["guid"] = ch["guid"]
        out_changes.append(row)
    return json.dumps({
        "context": {"entity_key": entity_key},
        "changes": out_changes,
        "all_rows": [],
    })


def test_save_changes_new_shape_single_arg_writes_mapping(tmp_path):
    data_root = _setup_data_root(tmp_path)
    payload = _engine_payload("SYN-IND", [
        {"guid": "g1", "path": "Assets/Cash", "tag": "AL_CASH_BANK", "_orig": ""},
    ])
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes(payload)

    assert "Saved" in msg
    assert "nothing to back up" in msg.lower()  # cold start

    import configs  # noqa: PLC0415 -- imported after _itr_modules() puts scripts/ on sys.path
    mapping_path = data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    loaded = configs.load_mapping(mapping_path)
    assert loaded.entries["g1"].tag == "AL_CASH_BANK"


def test_save_changes_entity_key_travels_via_context_not_a_second_input(tmp_path):
    """entity_key rides in payload['context'], not a separate function arg --
    confirms the render()'s save_btn.click no longer needs the entity
    dropdown as an input (fixing a latent staleness bug: Save used to read
    whatever the dropdown currently showed, not what was actually Loaded)."""
    data_root = _setup_data_root(tmp_path)
    payload = _engine_payload("SYN-IND", [{"guid": "g1", "path": "Assets/Cash", "tag": "AL_CASH_BANK"}])
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes(payload)
    assert "Saved" in msg
    # Confirm _save_changes takes exactly one argument now.
    import inspect
    assert len(inspect.signature(ui_mod._save_changes).parameters) == 1


def test_save_changes_blank_entity_key_in_context_never_touches_filesystem(tmp_path):
    data_root = tmp_path / "Data"
    payload = _engine_payload("", [{"guid": "g1", "path": "x", "tag": "AL_CASH_BANK"}])
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes(payload)
    assert "select an entity" in msg.lower()
    assert not data_root.exists()


def test_save_changes_no_changes_is_a_noop():
    assert "no changes" in ui_mod._save_changes("").lower()
    assert "no changes" in ui_mod._save_changes(_engine_payload("SYN-IND", [])).lower()


def test_save_changes_malformed_json_reports_error():
    assert "Error parsing changes" in ui_mod._save_changes("{not valid json")


def test_save_changes_delete_removes_entry_from_mapping_file(tmp_path):
    """A row whose guid IS in the entity's mapping file gets that entry
    REMOVED from the file when deleted -- the real need this covers: noise
    entries a user must be able to delete, not just hide client-side."""
    data_root = _setup_data_root(tmp_path)
    import yaml
    mapping_path = data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    mapping_path.write_text(yaml.safe_dump([
        {"guid": "g1", "path": "Assets/Noise", "tag": "AL_CASH_BANK",
         "suggested_by_llm": None, "note": ""},
        {"guid": "g2", "path": "Income/Bank Interest", "tag": "OS_INTEREST_BANK",
         "suggested_by_llm": None, "note": ""},
    ], sort_keys=False), encoding="utf-8")

    payload = json.dumps({
        "context": {"entity_key": "SYN-IND"},
        "changes": [{"_idx": 0, "_orig": "", "guid": "g1", "path": "Assets/Noise",
                     "tag": "AL_CASH_BANK", "_deleted": True}],
        "all_rows": [],
    })
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes(payload)

    assert "Deleted 1" in msg
    assert "Backup written" in msg

    import configs  # noqa: PLC0415
    loaded = configs.load_mapping(mapping_path)
    assert "g1" not in loaded.entries
    assert loaded.entries["g2"].tag == "OS_INTEREST_BANK"


def test_save_changes_delete_and_correction_together_in_one_save(tmp_path):
    data_root = _setup_data_root(tmp_path)
    import yaml
    mapping_path = data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    mapping_path.write_text(yaml.safe_dump([
        {"guid": "g1", "path": "Assets/Noise", "tag": "AL_CASH_BANK",
         "suggested_by_llm": None, "note": ""},
    ], sort_keys=False), encoding="utf-8")

    payload = json.dumps({
        "context": {"entity_key": "SYN-IND"},
        "changes": [
            {"_idx": 0, "_orig": "", "guid": "g1", "path": "Assets/Noise",
             "tag": "AL_CASH_BANK", "_deleted": True},
            {"_idx": 1, "_orig": "", "guid": "g2", "path": "Income/Bank Interest",
             "tag": "OS_INTEREST_BANK", "_deleted": False},
        ],
        "all_rows": [],
    })
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes(payload)

    assert "Applied 1 correction" in msg
    assert "Deleted 1" in msg

    import configs  # noqa: PLC0415
    loaded = configs.load_mapping(mapping_path)
    assert "g1" not in loaded.entries
    assert loaded.entries["g2"].tag == "OS_INTEREST_BANK"


def test_save_changes_delete_unmapped_row_is_view_only_never_persisted(tmp_path):
    """A deleted row whose guid is NOT in the mapping file (a snippet-only
    unmapped suggestion) has nothing to persist -- the client already
    dropped it from the table; the mapping file (cold start) is never
    created just to record that."""
    data_root = _setup_data_root(tmp_path)
    mapping_path = data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    payload = json.dumps({
        "context": {"entity_key": "SYN-IND"},
        "changes": [{"_idx": 0, "_orig": "", "guid": "never-mapped",
                     "path": "Assets/X", "tag": "", "_deleted": True}],
        "all_rows": [],
    })
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes(payload)

    assert "Removed 1 unmapped row" in msg
    assert not mapping_path.is_file()


def test_save_changes_delete_wins_over_a_tag_on_the_same_row(tmp_path):
    """`_deleted` on a change entry always takes precedence over a `tag`
    value on the same entry -- a row can't be both corrected and deleted."""
    data_root = _setup_data_root(tmp_path)
    import yaml
    mapping_path = data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    mapping_path.write_text(yaml.safe_dump([
        {"guid": "g1", "path": "Assets/Noise", "tag": "AL_CASH_BANK",
         "suggested_by_llm": None, "note": ""},
    ], sort_keys=False), encoding="utf-8")

    payload = json.dumps({
        "context": {"entity_key": "SYN-IND"},
        "changes": [{"_idx": 0, "_orig": "", "guid": "g1", "path": "Assets/Noise",
                     "tag": "OS_INTEREST_BANK", "_deleted": True}],
        "all_rows": [],
    })
    with patch("ui._config.data_root_dir", return_value=data_root):
        ui_mod._save_changes(payload)

    import configs  # noqa: PLC0415
    loaded = configs.load_mapping(mapping_path)
    assert "g1" not in loaded.entries


def test_spec_allow_delete_true_renders_remove_button(tmp_path):
    data_root = _setup_data_root(tmp_path)
    import yaml
    (data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml").write_text(yaml.safe_dump([
        {"guid": "g1", "path": "Income/Bank Interest", "tag": "OS_INTEREST_BANK",
         "suggested_by_llm": None, "note": ""},
    ], sort_keys=False), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        html = ui_mod._load_review_data("SYN-IND")

    assert 'id="itrmap-remove-sel"' in html


def test_save_changes_fields_survive_shape_translation_guid_path_tag(tmp_path):
    """The single highest risk in the guid/path/tag -> _idx/_orig/COLS
    translation is a field quietly vanishing. Prove guid, path and tag all
    survive by checking the actually-written mapping file reflects all
    three, for two rows saved in the same payload."""
    data_root = _setup_data_root(tmp_path)
    payload = _engine_payload("SYN-IND", [
        {"guid": "g1", "path": "Assets/Cash", "tag": "AL_CASH_BANK"},
        {"guid": "g2", "path": "Income/Bank Interest", "tag": "OS_INTEREST_BANK"},
    ])
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_changes(payload)
    assert "Applied 2 correction" in msg

    import configs  # noqa: PLC0415
    mapping_path = data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    loaded = configs.load_mapping(mapping_path)
    assert loaded.entries["g1"].path == "Assets/Cash"
    assert loaded.entries["g1"].tag == "AL_CASH_BANK"
    assert loaded.entries["g2"].path == "Income/Bank Interest"
    assert loaded.entries["g2"].tag == "OS_INTEREST_BANK"


# ---------------------------------------------------------------------------
# Layer A -- config-driven filename attribution (_workbook_match_map /
# _snippet_entity_key / _latest_proposed_mappings_path). Real-data
# validation showed workbook output stems are user-controlled CamelCase
# "<First><Last>[HUF]<year>" (e.g. "KiranAmbani25626",
# "VaikunthAmbaniHUF2526") which the old entity-key-as-boundary-substring
# scheme could never match (the entity key itself, e.g. "VaikunthHUF", isn't
# even a substring of the stem). Attribution is now driven by an explicit,
# optional `workbook_match:` token per entity in entities.yaml, matched as a
# plain case-insensitive substring with longest-match-wins.
# ---------------------------------------------------------------------------

def test_snippet_entity_key_individual_and_huf_longest_match_wins():
    match_map = {"Alice": "AliceSmith", "AliceHUF": "AliceSmithHUF"}
    # The HUF's own stem contains BOTH tokens ("AliceSmith" is a substring
    # of "AliceSmithHUF2526") -- the longer token must win.
    assert ui_mod._snippet_entity_key("AliceSmithHUF2526", match_map) == "AliceHUF"
    # The individual's own stem only contains the shorter token.
    assert ui_mod._snippet_entity_key("AliceSmith2526", match_map) == "Alice"


def test_snippet_entity_key_year_agnostic_same_token_both_years():
    match_map = {"Alice": "AliceSmith"}
    assert ui_mod._snippet_entity_key("AliceSmith2526", match_map) == "Alice"
    assert ui_mod._snippet_entity_key("AliceSmith2627", match_map) == "Alice"


def test_snippet_entity_key_foreign_stem_matches_nothing():
    match_map = {"Alice": "AliceSmith", "AliceHUF": "AliceSmithHUF"}
    assert ui_mod._snippet_entity_key("2026-07-25-144004-SomeoneElse2526", match_map) is None


def test_snippet_entity_key_ambiguous_tie_returns_none():
    # Two distinct entities configured with equally-long tokens, both
    # present in the same stem -- genuinely ambiguous, must not guess.
    match_map = {"Ent1": "Alpha", "Ent2": "Beta5"}
    assert ui_mod._snippet_entity_key("2026-Alpha-Beta5-2526", match_map) is None


def test_snippet_entity_key_no_match_map_matches_nothing():
    assert ui_mod._snippet_entity_key("AliceSmith2526", {}) is None


def test_snippet_entity_key_entity_with_no_workbook_match_never_matches():
    # Alice has no entry in match_map at all (no workbook_match configured)
    # -- even though "Bob" is a substring elsewhere, Alice's stem must not
    # resolve to anything.
    match_map = {"Bob": "BobJones"}
    assert ui_mod._snippet_entity_key("AliceSmith2526", match_map) is None


def _write_outputs_snippet(data_root: Path, name: str, guids: list[str], mtime: float) -> Path:
    import os
    import yaml
    out_dir = data_root / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / name
    p.write_text(yaml.safe_dump([
        {"guid": g, "path": f"Assets/{g}", "tag": "REPLACE_ME", "note": ""} for g in guids
    ], sort_keys=False), encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def test_latest_proposed_mappings_path_never_returns_a_newer_foreign_snippet(tmp_path):
    """The core regression: the OTHER entity's snippet is strictly newer,
    but must never be selected for this entity -- entity-scoping, not
    global-newest, decides the winner."""
    data_root = _setup_data_root(tmp_path)
    _write_outputs_snippet(data_root, "2026-07-25-100000-AliceSmith2526-proposed-mappings.yaml", ["gA"], mtime=1000)
    _write_outputs_snippet(data_root, "2026-07-25-200000-BobJones2526-proposed-mappings.yaml", ["gB"], mtime=2000)

    match_map = {"Alice": "AliceSmith", "Bob": "BobJones"}
    with patch("ui._config.data_root_dir", return_value=data_root):
        found = ui_mod._latest_proposed_mappings_path("Alice", match_map)

    assert found is not None
    assert "AliceSmith2526" in found.name


def test_latest_proposed_mappings_path_none_when_only_foreign_snippets_exist(tmp_path):
    data_root = _setup_data_root(tmp_path)
    _write_outputs_snippet(data_root, "2026-07-25-200000-BobJones2526-proposed-mappings.yaml", ["gB"], mtime=2000)

    match_map = {"Alice": "AliceSmith", "Bob": "BobJones"}
    with patch("ui._config.data_root_dir", return_value=data_root):
        found = ui_mod._latest_proposed_mappings_path("Alice", match_map)

    assert found is None


def test_latest_proposed_mappings_path_none_when_entity_has_no_workbook_match(tmp_path):
    """An entity with no `workbook_match:` configured can never match any
    stem -- it gets no snippet, never a foreign one, by construction."""
    data_root = _setup_data_root(tmp_path)
    _write_outputs_snippet(data_root, "2026-07-25-100000-AliceSmith2526-proposed-mappings.yaml", ["gA"], mtime=1000)

    match_map: dict[str, str] = {}  # "Alice" has no entry
    with patch("ui._config.data_root_dir", return_value=data_root):
        found = ui_mod._latest_proposed_mappings_path("Alice", match_map)

    assert found is None


def test_latest_proposed_mappings_path_newest_first_among_own_snippets(tmp_path):
    """When two snippets are both attributable to the same entity, the
    newer (by mtime) one is returned."""
    data_root = _setup_data_root(tmp_path)
    _write_outputs_snippet(data_root, "2026-07-25-100000-AliceSmith2526-proposed-mappings.yaml", ["gOld"], mtime=1000)
    _write_outputs_snippet(data_root, "2026-07-26-100000-AliceSmith2627-proposed-mappings.yaml", ["gNew"], mtime=2000)

    match_map = {"Alice": "AliceSmith"}
    with patch("ui._config.data_root_dir", return_value=data_root):
        found = ui_mod._latest_proposed_mappings_path("Alice", match_map)

    assert found is not None
    assert "AliceSmith2627" in found.name


def test_workbook_match_map_reads_entities_yaml(tmp_path):
    data_root = _setup_data_root(tmp_path)
    import yaml
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.write_text(yaml.safe_dump({
        "Alice": {"name": "Alice Smith", "pan": "AAAAA0000A",
                  "status": "Individual", "default_regime": "new",
                  "workbook_match": "AliceSmith"},
        "AliceHUF": {"name": "Alice Smith HUF", "pan": "BBBBB0000B",
                     "status": "HUF", "default_regime": "new",
                     "workbook_match": "AliceSmithHUF"},
        "Bob": {"name": "Bob Jones", "pan": "CCCCC0000C",
                "status": "Individual", "default_regime": "new"},
    }), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        match_map = ui_mod._workbook_match_map()

    assert match_map == {"Alice": "AliceSmith", "AliceHUF": "AliceSmithHUF"}
    assert "Bob" not in match_map  # no workbook_match configured


def test_load_review_rows_foreign_snippet_guid_never_leaks_into_this_entity(tmp_path):
    """End-to-end through _load_review_rows(): a foreign entity's snippet
    (newer, sitting in the same outputs folder) must contribute zero rows
    to this entity's review."""
    data_root = _setup_data_root(tmp_path)
    import yaml
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.write_text(yaml.safe_dump({
        "Alice": {"name": "Alice Smith", "pan": "AAAAA0000A",
                  "status": "Individual", "default_regime": "new",
                  "workbook_match": "AliceSmith"},
        "AliceHUF": {"name": "Alice Smith HUF", "pan": "BBBBB0000B",
                     "status": "HUF", "default_regime": "new",
                     "workbook_match": "AliceSmithHUF"},
    }), encoding="utf-8")

    _write_outputs_snippet(data_root, "2026-07-25-100000-AliceSmith2526-proposed-mappings.yaml", ["g-ind"], mtime=1000)
    _write_outputs_snippet(data_root, "2026-07-25-200000-AliceSmithHUF2526-proposed-mappings.yaml", ["g-huf"], mtime=2000)

    with patch("ui._config.data_root_dir", return_value=data_root):
        ind_rows = ui_mod._load_review_rows("Alice")
        huf_rows = ui_mod._load_review_rows("AliceHUF")

    assert [r["guid"] for r in ind_rows] == ["g-ind"]
    assert [r["guid"] for r in huf_rows] == ["g-huf"]


def test_load_review_rows_correct_entity_still_gets_its_own_snippet(tmp_path):
    data_root = _setup_data_root(tmp_path)
    _write_outputs_snippet(data_root, "2026-07-25-100000-SYN-IND2526-proposed-mappings.yaml", ["g1", "g2"], mtime=1000)

    with patch("ui._config.data_root_dir", return_value=data_root):
        rows = ui_mod._load_review_rows("SYN-IND")

    assert {r["guid"] for r in rows} == {"g1", "g2"}
    assert all(r["unmapped"] for r in rows)


# ---------------------------------------------------------------------------
# Layer B -- optional book GUID validation (_entity_book_path /
# _apply_book_validation). Purely additive: unconfigured/unreadable book ->
# Layer A's result stands unchanged; configured + readable -> drops rows
# whose GUID isn't in the book and overrides `path` with the book's own.
# ---------------------------------------------------------------------------

_GNC_NS = (
    'xmlns:gnc="http://www.gnucash.org/XML/gnc" '
    'xmlns:act="http://www.gnucash.org/XML/act"'
)


def _gnc_account_xml(guid: str, name: str, acct_type: str, parent_guid: str | None) -> str:
    parent_el = f'<act:parent type="guid">{parent_guid}</act:parent>' if parent_guid else ""
    return (
        '<gnc:account version="2.0.0">'
        f'<act:name>{name}</act:name>'
        f'<act:id type="guid">{guid}</act:id>'
        f'<act:type>{acct_type}</act:type>'
        f'{parent_el}'
        '</gnc:account>'
    )


def _write_synthetic_book(path: Path, accounts: list[tuple[str, str, str, str | None]]) -> None:
    """Write a minimal, PLAIN-TEXT (no gzip) synthetic .gnucash file with
    just enough structure for agents.gnucash_accounts.load_accounts() to
    read: a root account plus whatever `accounts` list (guid, name, type,
    parent_guid) is given."""
    body = "".join(_gnc_account_xml(g, n, t, p) for g, n, t, p in accounts)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<gnc-v2 {_GNC_NS}><gnc:book>{body}</gnc:book></gnc-v2>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")


def test_apply_book_validation_no_book_configured_passes_rows_through(tmp_path):
    data_root = _setup_data_root(tmp_path)
    rows = [{"guid": "g1", "path": "Assets/g1", "tag": None}]
    with patch("ui._config.data_root_dir", return_value=data_root):
        out = ui_mod._apply_book_validation("SYN-IND", rows)
    assert out == rows


def test_apply_book_validation_unreadable_book_path_passes_rows_through(tmp_path):
    data_root = _setup_data_root(tmp_path)
    import yaml
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.write_text(yaml.safe_dump({
        "SYN-IND": {"name": "Synthetic Individual", "pan": "AAAAA0000A",
                    "status": "Individual", "default_regime": "new",
                    "book": str(tmp_path / "does-not-exist.gnucash")},
    }), encoding="utf-8")
    rows = [{"guid": "g1", "path": "Assets/g1", "tag": None}]
    with patch("ui._config.data_root_dir", return_value=data_root):
        out = ui_mod._apply_book_validation("SYN-IND", rows)
    assert out == rows


def test_apply_book_validation_drops_guid_absent_from_book_and_overrides_path(tmp_path):
    data_root = _setup_data_root(tmp_path)
    book_path = tmp_path / "syn.gnucash"
    _write_synthetic_book(book_path, [
        ("root-guid", "Root Account", "ROOT", None),
        ("g-real", "Bank Interest", "INCOME", "root-guid"),
    ])
    import yaml
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.write_text(yaml.safe_dump({
        "SYN-IND": {"name": "Synthetic Individual", "pan": "AAAAA0000A",
                    "status": "Individual", "default_regime": "new",
                    "book": str(book_path)},
    }), encoding="utf-8")

    rows = [
        {"guid": "g-real", "path": "STALE/PATH", "tag": None},
        {"guid": "g-foreign", "path": "Assets/Foreign", "tag": None},
    ]
    with patch("ui._config.data_root_dir", return_value=data_root):
        out = ui_mod._apply_book_validation("SYN-IND", rows)

    assert [r["guid"] for r in out] == ["g-real"]
    assert out[0]["path"] == "Bank Interest"


def test_load_review_rows_layer_b_drops_snippet_row_not_in_entity_book(tmp_path):
    """End-to-end: a snippet row for a GUID that isn't in the entity's own
    book (even though Layer A attributed the snippet correctly) is dropped
    -- belt-and-suspenders against a cross-entity GUID collision."""
    data_root = _setup_data_root(tmp_path)
    book_path = tmp_path / "syn.gnucash"
    _write_synthetic_book(book_path, [
        ("root-guid", "Root Account", "ROOT", None),
        ("g-real", "Bank Interest", "INCOME", "root-guid"),
    ])
    import yaml
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.write_text(yaml.safe_dump({
        "SYN-IND": {"name": "Synthetic Individual", "pan": "AAAAA0000A",
                    "status": "Individual", "default_regime": "new",
                    "workbook_match": "SYN-IND", "book": str(book_path)},
    }), encoding="utf-8")
    _write_outputs_snippet(
        data_root, "2026-07-25-100000-SYN-IND2526-proposed-mappings.yaml",
        ["g-real", "g-not-in-book"], mtime=1000,
    )

    with patch("ui._config.data_root_dir", return_value=data_root):
        rows = ui_mod._load_review_rows("SYN-IND")

    assert [r["guid"] for r in rows] == ["g-real"]
    assert rows[0]["path"] == "Bank Interest"
