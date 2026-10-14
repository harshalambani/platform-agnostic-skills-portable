"""
tests/skill_itr_workbook/test_itr08_mapping_picker.py -- ITR-08: "Entity
mapping" is no longer a file upload. The tab shows a read-only line from the
saved mapping, offers only the selected entity's own files as an override, and
the run refuses another entity's mapping. Synthetic data only.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
for p in (str(ROOT), str(SRC), str(SRC / "agents" / "skill_itr_workbook"),
          str(SRC / "agents" / "skill_itr_workbook" / "scripts"), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import agent  # noqa: E402
import fixture_gen  # noqa: E402
from agents import entity_scope  # noqa: E402
from agents.registry import discover  # noqa: E402
from ui.tabs import _generic  # noqa: E402
from ui.tabs import itr_mapping_review as ui_mod  # noqa: E402

ENTITIES_EXAMPLE = ROOT / "bundling" / "canonical" / "itr" / "entities.example.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _world(tmp_path):
    root = tmp_path / "Data"
    (root / "itr" / "mappings").mkdir(parents=True)
    (root / "outputs").mkdir()
    shutil.copy(ENTITIES_EXAMPLE, root / "itr" / "entities.yaml")
    return root


def _state(root, key):
    with patch("ui._config.data_root_dir", return_value=root):
        return _generic._mapping_picker_state(key)


def _picker():
    sk = next(s for s in discover(refresh=True) if Path(s.manifest_path).parent.name == "skill_itr_workbook")
    return next(i for i in sk.inputs if i.name == "mapping_file")


def _run(tmp_path, root, mapping_file=None, key="SYN-IND"):
    html = tmp_path / "bs.html"
    html.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out = tmp_path / "out.xlsx"
    kw = {"mapping_file": str(mapping_file)} if mapping_file else {}
    return agent.run(str(html), str(out), entity_key=key,
                     entities_path=str(root / "itr" / "entities.yaml"),
                     config_path="no-such-config.yaml", **kw)


# --- the manifest: no upload ----------------------------------------------------------

def test_mapping_input_is_a_scoped_picker_not_a_file_upload():
    i = _picker()
    assert i.type == "output_file" and i.type != "file"
    assert i.entity_from == "entity" and i.match == entity_scope.MAPPING_MATCH
    assert not i.required


# --- the read-only line ------------------------------------------------------------------

def test_line_names_entity_tag_count_and_save_date(tmp_path):
    root = _world(tmp_path)
    shutil.copy(FIXTURES / "syn_ind.mapping.yaml", root / "itr" / "mappings" / "SYN-IND.mapping.yaml")
    choices, value, msg = _state(root, "SYN-IND")
    assert value == "" and choices[0] == ("Saved mapping (default)", "")
    assert msg.startswith("Using saved mapping for SYN-IND (")
    assert " tags, saved " in msg and "20" in msg


def test_no_saved_mapping_says_cold_start_not_using(tmp_path):
    root = _world(tmp_path)
    _c, _v, msg = _state(root, "SYN-IND")
    assert "No saved mapping for SYN-IND" in msg and "Using saved mapping" not in msg


def test_no_entity_selected_offers_only_the_default(tmp_path):
    root = _world(tmp_path)
    shutil.copy(FIXTURES / "syn_ind.mapping.yaml", root / "itr" / "mappings" / "SYN-IND.mapping.yaml")
    choices, value, msg = _state(root, None)
    assert choices == [("Saved mapping (default)", "")] and value == ""
    assert "Pick an entity first" in msg


# --- NEGATIVE: another entity's mapping is never offered or used ---------------------------

def test_dropdown_never_lists_another_entitys_files(tmp_path):
    root = _world(tmp_path)
    m = root / "itr" / "mappings"
    for name in ("SYN-IND.mapping.yaml", "SYN-IND.old.mapping.yaml", "SYN-HUF.mapping.yaml",
                 "SYN-IND-BIZ.mapping.yaml", "SYN-IND.mapping.yaml.bak-20260101"):
        (m / name).write_text("[]\n", encoding="utf-8")
    choices, _v, _m = _state(root, "SYN-IND")
    names = [c[0] for c in choices]
    assert names == ["Saved mapping (default)", "SYN-IND.old.mapping.yaml"]


def test_run_refuses_a_file_named_for_another_entity(tmp_path):
    root = _world(tmp_path)
    other = root / "itr" / "mappings" / "SYN-HUF.mapping.yaml"
    shutil.copy(FIXTURES / "syn_ind.mapping.yaml", other)
    summary = _run(tmp_path, root, mapping_file=other)
    assert summary.startswith("ERROR:") and "SYN-HUF" in summary and "not to SYN-IND" in summary
    assert "STATUS: BUILT" not in summary and "STATUS: OK" not in summary


def test_run_refuses_a_file_whose_recorded_entity_differs(tmp_path):
    root = _world(tmp_path)
    f = tmp_path / "anything.yaml"
    f.write_text("# entity: SYN-HUF\n" + (FIXTURES / "syn_ind.mapping.yaml").read_text(encoding="utf-8"),
                 encoding="utf-8")
    summary = _run(tmp_path, root, mapping_file=f)
    assert summary.startswith("ERROR:") and "saved for entity SYN-HUF" in summary


def test_run_accepts_own_file_and_unrelated_name(tmp_path):
    """The guard refuses only a file that is positively another entity's."""
    root = _world(tmp_path)
    own = root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    shutil.copy(FIXTURES / "syn_ind.mapping.yaml", own)
    assert not _run(tmp_path, root, mapping_file=own).startswith("ERROR:")
    assert not _run(tmp_path, root, mapping_file=FIXTURES / "syn_ind.mapping.yaml").startswith("ERROR:")


# --- NEGATIVE: a blank choice never cold-starts when a saved mapping exists ------------------

def test_blank_choice_uses_the_saved_mapping_not_a_cold_start(tmp_path):
    root = _world(tmp_path)
    shutil.copy(FIXTURES / "syn_ind.mapping.yaml", root / "itr" / "mappings" / "SYN-IND.mapping.yaml")
    summary = _run(tmp_path, root)
    assert "Mapping: auto-derived SYN-IND.mapping.yaml" in summary
    assert "no mapping_file supplied and none found" not in summary


def test_blank_choice_with_no_saved_mapping_does_start_cold(tmp_path):
    root = _world(tmp_path)
    summary = _run(tmp_path, root)
    assert "auto-derived" not in summary


# --- NEGATIVE: a run never reads a mapping older than the latest Review Mapping save --------------

def test_run_after_a_review_save_sees_the_saved_tag(tmp_path):
    """The default is resolved at run time from the live file, so a save made
    after the form was filled is always what the run reads."""
    root = _world(tmp_path)
    mf = root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    shutil.copy(FIXTURES / "syn_ind_partial.mapping.yaml", mf)
    before = _run(tmp_path, root)
    assert "REVIEW ITEM" in before                     # an account is still unmapped
    entries = yaml.safe_load(FIXTURES.joinpath("syn_ind.mapping.yaml").read_text(encoding="utf-8"))
    # the Review Mapping save rewrites the entity's file in place
    mf.write_text(yaml.safe_dump(entries), encoding="utf-8")
    sub = tmp_path / "second"
    sub.mkdir()
    after = _run(sub, root)
    assert before != after and "Mapping: auto-derived SYN-IND.mapping.yaml" in after


def test_backups_are_never_offered_as_overrides(tmp_path):
    root = _world(tmp_path)
    m = root / "itr" / "mappings"
    (m / "SYN-IND.mapping.yaml").write_text("[]\n", encoding="utf-8")
    (m / "SYN-IND.mapping.yaml.bak-20260101-000000").write_text("[]\n", encoding="utf-8")
    choices, _v, _m = _state(root, "SYN-IND")
    assert [c[0] for c in choices] == ["Saved mapping (default)"]


def test_scoped_picker_state_routes_to_mapping_state(tmp_path):
    root = _world(tmp_path)
    shutil.copy(FIXTURES / "syn_ind.mapping.yaml", root / "itr" / "mappings" / "SYN-IND.mapping.yaml")
    with patch("ui._config.data_root_dir", return_value=root):
        choices, value, msg = _generic._scoped_picker_state(_picker(), "SYN-IND")
    assert value == "" and msg.startswith("Using saved mapping for SYN-IND")
