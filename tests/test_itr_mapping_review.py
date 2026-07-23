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
                    "status": "Individual", "default_regime": "new"},
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
