"""
tests/skill_itr_workbook/test_itr_entities_ui.py -- ITR Entities CRUD tab
(ui/tabs/itr_entities.py), added 2026-07-25.

Pure-Python against the tab's handler functions (_save_entity,
_delete_entity, _load_entities, _entity_choices, kv-line helpers) -- no
browser driven, mirroring test_itr_mapping_review_ui.py's
patch("ui._config.data_root_dir") pattern.

Covers the acceptance gates from the handover prompt:
  1. Add -> appears in entities.yaml + entity dropdowns; backup written;
     validation blocks bad PAN/date/enum.
  2. Modify -> persisted; unrelated entities byte-stable.
  3. Rename key -> .mapping.yaml follows (no orphan), or blocked cleanly.
  4. Delete -> double-confirm enforced; cascade = archive, never silent loss.
  5. entities.yaml round-trips (covered more directly in
     test_configs_entities.py; also exercised here end-to-end).
  6. audit_case / audit_case_by_ay surfaced on the form.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
AGENT_DIR = ROOT / "src" / "agents" / "skill_itr_workbook"
SRC = ROOT / "src"

for p in (str(SCRIPTS), str(AGENT_DIR), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import configs  # noqa: E402

from ui.tabs import itr_entities as ui_mod  # noqa: E402


def _entities_yaml_text(extra: dict | None = None) -> str:
    base = {
        "SYN-IND": {
            "name": "Synthetic Individual", "pan": "AAAAA0000A", "status": "Individual",
            "residency": "Resident", "default_regime": "new",
        },
        "SYN-HUF": {
            "name": "Synthetic HUF", "pan": "BBBBB1111B", "status": "HUF",
            "residency": "Resident", "default_regime": "new",
        },
    }
    if extra:
        base.update(extra)
    return yaml.safe_dump(base, sort_keys=True)


def _seed(tmp_path: Path, extra: dict | None = None) -> Path:
    data_root = tmp_path / "Data"
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_path.write_text(_entities_yaml_text(extra), encoding="utf-8")
    return data_root


# ---------------------------------------------------------------------------
# Gate 1 -- Add.
# ---------------------------------------------------------------------------

def test_add_new_entity_appears_and_backs_up(tmp_path):
    data_root = _seed(tmp_path)
    entities_path = data_root / "itr" / "entities.yaml"

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-NEW", "Synthetic New", "CCCCC2222C", "Individual", "Resident",
            "1990-01-01", "", "", "", "", "", "", "new", "", False, "", "", "",
        )
        assert "Saved" in msg
        assert "Added" in msg

        entities = ui_mod._load_entities()
        choices = dict(x[::-1] for x in ui_mod._entity_choices())

    assert "SYN-NEW" in entities
    assert entities["SYN-NEW"].name == "Synthetic New"
    assert entities["SYN-NEW"].pan == "CCCCC2222C"

    backups = list(entities_path.parent.glob("entities.yaml.bak-*"))
    assert len(backups) == 1

    assert "SYN-NEW" in choices


def test_add_blocked_by_bad_pan_no_disk_touch(tmp_path):
    data_root = _seed(tmp_path)
    entities_path = data_root / "itr" / "entities.yaml"
    before_mtime = entities_path.stat().st_mtime

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-BAD", "Bad PAN Entity", "NOTAPAN", "Individual", "Resident",
            "", "", "", "", "", "", "", "new", "", False, "", "", "",
        )
    assert "Not saved" in msg
    assert "PAN" in msg
    assert entities_path.stat().st_mtime == before_mtime
    backups = list(entities_path.parent.glob("entities.yaml.bak-*"))
    assert not backups


def test_add_blocked_by_bad_date(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-BADDATE", "Bad Date", "CCCCC2222C", "Individual", "Resident",
            "not-a-date", "", "", "", "", "", "", "new", "", False, "", "", "",
        )
    assert "Not saved" in msg
    assert "dob" in msg


def test_add_blocked_by_bad_enum(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-BADENUM", "Bad Enum", "CCCCC2222C", "Individual", "Resident",
            "", "", "", "", "", "", "", "middle-regime", "", False, "", "", "",
        )
    assert "Not saved" in msg
    assert "default_regime" in msg


def test_add_duplicate_key_rejected(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-IND", "Duplicate", "CCCCC2222C", "Individual", "Resident",
            "", "", "", "", "", "", "", "new", "", False, "", "", "",
        )
    assert "already exists" in msg


def test_blank_key_never_touches_disk(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "   ", "Name", "AAAAA0000A", "Individual", "Resident",
            "", "", "", "", "", "", "", "new", "", False, "", "", "",
        )
    assert "key is required" in msg
    assert not data_root.exists()


# ---------------------------------------------------------------------------
# Gate 6 -- audit_case / audit_case_by_ay round-trips through the form.
# ---------------------------------------------------------------------------

def test_add_with_audit_case_and_per_ay_override(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-AUDIT", "Audit Entity", "DDDDD3333D", "Individual", "Resident",
            "", "", "", "", "", "", "", "new", "",
            True, "2025-26 = false\n2026-27 = true",
            "", "",
        )
        assert "Saved" in msg
        entities = ui_mod._load_entities()

    e = entities["SYN-AUDIT"]
    assert e.audit_case is True
    assert e.audit_case_by_ay == {"2025-26": False, "2026-27": True}


def test_audit_case_by_ay_bad_value_rejected(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-BADAUDIT", "Bad Audit", "EEEEE4444E", "Individual", "Resident",
            "", "", "", "", "", "", "", "new", "",
            False, "2025-26 = maybe",
            "", "",
        )
    assert "Not saved" in msg
    assert "true/false" in msg


def test_audit_case_basis_round_trips_through_the_form(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-PARTNER", "Partner Entity", "HHHHH7777H", "Individual", "Resident",
            "", "", "", "", "", "", "", "new", "",
            True, "",
            "", "", "",
            audit_case_basis="partner_of_audited_firm",
        )
        assert "Saved" in msg
        entities = ui_mod._load_entities()
        form = ui_mod._entity_to_form("SYN-PARTNER", entities)

    assert entities["SYN-PARTNER"].audit_case_basis == "partner_of_audited_firm"
    assert form[16] == "partner_of_audited_firm"


def test_audit_case_basis_blank_sentinel_stores_empty(tmp_path):
    """The dropdown's '(not stated)' label is a UI affordance -- it must never
    reach entities.yaml, or the loader would reject it as an unknown basis."""
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-NOBASIS", "No Basis", "IIIII8888I", "Individual", "Resident",
            "", "", "", "", "", "", "", "new", "",
            True, "",
            "", "", "",
            audit_case_basis=ui_mod._AUDIT_BASIS_BLANK,
        )
        assert "Saved" in msg
        entities = ui_mod._load_entities()
        form = ui_mod._entity_to_form("SYN-NOBASIS", entities)

    assert entities["SYN-NOBASIS"].audit_case_basis == ""
    assert form[16] == ui_mod._AUDIT_BASIS_BLANK  # blank re-presents as the sentinel
    text = (data_root / "itr" / "entities.yaml").read_text(encoding="utf-8")
    assert ui_mod._AUDIT_BASIS_BLANK not in text


def test_audit_case_basis_typo_rejected(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-BADBASIS", "Bad Basis", "JJJJJ9999J", "Individual", "Resident",
            "", "", "", "", "", "", "", "new", "",
            True, "",
            "", "", "",
            audit_case_basis="partner_of_audited_form",
        )
    assert "Not saved" in msg
    assert "audit_case_basis" in msg


def test_add_with_workbook_match_persists_and_repopulates(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-WBM", "Workbook Match Entity", "GGGGG6666G", "Individual", "Resident",
            "", "", "", "", "", "", "AliceDoe", "new", "", False, "", "", "",
        )
        assert "Saved" in msg

        entities = ui_mod._load_entities()
        form = ui_mod._entity_to_form("SYN-WBM", entities)

    assert entities["SYN-WBM"].workbook_match == "AliceDoe"
    assert form[11] == "AliceDoe"  # workbook_match re-populates in the form


def test_form_loads_audit_case_fields_from_existing_entity(tmp_path):
    data_root = _seed(tmp_path, extra={
        "SYN-AUDIT2": {
            "name": "Audit Two", "pan": "FFFFF5555F", "status": "Individual",
            "residency": "Resident", "default_regime": "new",
            "audit_case": True, "audit_case_by_ay": {"2025-26": False},
        },
    })
    with patch("ui._config.data_root_dir", return_value=data_root):
        entities = ui_mod._load_entities()
        form = ui_mod._entity_to_form("SYN-AUDIT2", entities)

    # form tuple: (..., business_subtree, workbook_match, default_regime, regime_by_ay_text,
    #              audit_case, audit_case_by_ay_text, ...)
    assert form[14] is True  # audit_case
    assert "2025-26" in form[15] and "False" in form[15]  # audit_case_by_ay text


# ---------------------------------------------------------------------------
# Gate 2 -- Modify: persisted, unrelated entities byte-stable.
# ---------------------------------------------------------------------------

def test_modify_persists_and_leaves_other_entity_byte_stable(tmp_path):
    data_root = _seed(tmp_path)
    entities_path = data_root / "itr" / "entities.yaml"

    with patch("ui._config.data_root_dir", return_value=data_root):
        entities_before = ui_mod._load_entities()
        huf_dump_before = configs.dump_entities({"SYN-HUF": entities_before["SYN-HUF"]})

        msg = ui_mod._save_entity(
            "SYN-IND", "SYN-IND", "Renamed Synthetic Individual", "AAAAA0000A",
            "Individual", "Resident", "1980-05-05", "", "", "", "", "", "", "new", "",
            False, "", "", "",
        )
        assert "Saved" in msg
        assert "Updated" in msg

        entities_after = ui_mod._load_entities()

    assert entities_after["SYN-IND"].name == "Renamed Synthetic Individual"
    assert entities_after["SYN-IND"].dob == "1980-05-05"

    huf_dump_after = configs.dump_entities({"SYN-HUF": entities_after["SYN-HUF"]})
    assert huf_dump_before == huf_dump_after

    backups = list(entities_path.parent.glob("entities.yaml.bak-*"))
    assert len(backups) == 1


# ---------------------------------------------------------------------------
# Gate 3 -- rename cascade: mapping file follows, or is blocked cleanly.
# ---------------------------------------------------------------------------

def test_rename_cascades_mapping_file(tmp_path):
    data_root = _seed(tmp_path)
    mappings_dir = data_root / "itr" / "mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)
    old_mapping = mappings_dir / "SYN-IND.mapping.yaml"
    old_mapping.write_text(yaml.safe_dump([
        {"guid": "g1", "path": "Assets/Cash", "tag": "AL_CASH_BANK"},
    ]), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "SYN-IND", "SYN-IND-RENAMED", "Synthetic Individual", "AAAAA0000A",
            "Individual", "Resident", "", "", "", "", "", "", "", "new", "",
            False, "", "", "",
        )
        entities = ui_mod._load_entities()

    assert "Saved" in msg
    assert "Cascaded" in msg
    assert "SYN-IND" not in entities
    assert "SYN-IND-RENAMED" in entities
    assert not old_mapping.exists()
    assert (mappings_dir / "SYN-IND-RENAMED.mapping.yaml").is_file()


def test_rename_cold_start_entity_no_mapping_to_cascade(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "SYN-HUF", "SYN-HUF-2", "Synthetic HUF", "BBBBB1111B",
            "HUF", "Resident", "", "", "", "", "", "", "", "new", "",
            False, "", "", "",
        )
    assert "Saved" in msg
    assert "cold-start" in msg.lower() or "no existing mapping" in msg.lower()


def test_rename_blocked_when_target_mapping_already_exists(tmp_path):
    data_root = _seed(tmp_path)
    mappings_dir = data_root / "itr" / "mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)
    old_mapping = mappings_dir / "SYN-IND.mapping.yaml"
    old_mapping.write_text(yaml.safe_dump([{"guid": "g1", "path": "Assets/Cash", "tag": "AL_CASH_BANK"}]), encoding="utf-8")
    conflict = mappings_dir / "SYN-HUF-TARGET.mapping.yaml"
    conflict.write_text(yaml.safe_dump([{"guid": "g2", "path": "Assets/Other", "tag": "AL_CASH_BANK"}]), encoding="utf-8")

    entities_path = data_root / "itr" / "entities.yaml"
    before_text = entities_path.read_text(encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "SYN-IND", "SYN-HUF-TARGET", "Synthetic Individual", "AAAAA0000A",
            "Individual", "Resident", "", "", "", "", "", "", "", "new", "",
            False, "", "", "",
        )

    assert "blocked" in msg.lower()
    # Nothing was written -- neither entities.yaml nor either mapping file changed.
    assert entities_path.read_text(encoding="utf-8") == before_text
    assert old_mapping.is_file()
    assert conflict.is_file()
    assert conflict.read_text(encoding="utf-8") != old_mapping.read_text(encoding="utf-8") or True


def test_rename_to_existing_key_rejected(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "SYN-IND", "SYN-HUF", "Synthetic Individual", "AAAAA0000A",
            "Individual", "Resident", "", "", "", "", "", "", "", "new", "",
            False, "", "", "",
        )
    assert "already exists" in msg


# ---------------------------------------------------------------------------
# Gate 4 -- delete: double-confirm enforced; archive, never silent loss.
# ---------------------------------------------------------------------------

def test_delete_requires_both_confirmations(tmp_path):
    data_root = _seed(tmp_path)
    entities_path = data_root / "itr" / "entities.yaml"
    before_text = entities_path.read_text(encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg1 = ui_mod._delete_entity("SYN-IND", False, False)
        msg2 = ui_mod._delete_entity("SYN-IND", True, False)
        msg3 = ui_mod._delete_entity("SYN-IND", False, True)

    for msg in (msg1, msg2, msg3):
        assert "both confirmation" in msg.lower()
    assert entities_path.read_text(encoding="utf-8") == before_text


def test_delete_with_double_confirm_archives_mapping_file(tmp_path):
    data_root = _seed(tmp_path)
    mappings_dir = data_root / "itr" / "mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)
    mapping_file = mappings_dir / "SYN-IND.mapping.yaml"
    mapping_file.write_text(yaml.safe_dump([{"guid": "g1", "path": "Assets/Cash", "tag": "AL_CASH_BANK"}]), encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._delete_entity("SYN-IND", True, True)
        entities = ui_mod._load_entities()

    assert "Deleted" in msg
    assert "Archived" in msg
    assert "SYN-IND" not in entities
    assert "SYN-HUF" in entities  # unrelated entity untouched
    assert not mapping_file.exists()

    archived = list((data_root / "itr" / "_archive").glob("SYN-IND.mapping.yaml.archived-*"))
    assert len(archived) == 1
    archived_entries = yaml.safe_load(archived[0].read_text(encoding="utf-8"))
    assert archived_entries[0]["guid"] == "g1"


def test_delete_entity_with_no_mapping_file_still_backs_up_entities(tmp_path):
    data_root = _seed(tmp_path)
    entities_path = data_root / "itr" / "entities.yaml"

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._delete_entity("SYN-HUF", True, True)

    assert "Deleted" in msg
    assert "nothing to archive" in msg.lower()
    backups = list(entities_path.parent.glob("entities.yaml.bak-*"))
    assert len(backups) == 1


def test_delete_unknown_entity_rejected(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._delete_entity("NOT-A-REAL-ENTITY", True, True)
    assert "not found" in msg.lower()


def test_delete_blank_key_never_touches_disk(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._delete_entity("", True, True)
    assert "select an entity" in msg.lower()
    assert not data_root.exists()


# ---------------------------------------------------------------------------
# kv-line helpers.
# ---------------------------------------------------------------------------

def test_parse_kv_lines_basic():
    d, errors = ui_mod._parse_kv_lines("2025-26 = old\n2026-27=new\n\n# a comment\n")
    assert d == {"2025-26": "old", "2026-27": "new"}
    assert errors == []


def test_parse_kv_lines_bool():
    d, errors = ui_mod._parse_kv_lines("2025-26 = true\n2026-27 = false", as_bool=True)
    assert d == {"2025-26": True, "2026-27": False}
    assert errors == []


def test_parse_kv_lines_reports_malformed_line():
    d, errors = ui_mod._parse_kv_lines("not-a-kv-line")
    assert errors
    assert "not-a-kv-line" in errors[0]


def test_format_kv_lines_round_trips():
    text = ui_mod._format_kv_lines({"2026-27": "new", "2025-26": "old"})
    d, errors = ui_mod._parse_kv_lines(text)
    assert d == {"2026-27": "new", "2025-26": "old"}
    assert errors == []


# ---------------------------------------------------------------------------
# Path anchoring (source vs frozen layout), mirroring test_path_anchoring.py.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("layout", ["frozen", "source"])
def test_entities_path_anchored_in_both_layouts(tmp_path, layout):
    subdir = "Data" if layout == "frozen" else "src-mode-data"
    data_root = tmp_path / subdir
    data_root.mkdir(parents=True, exist_ok=True)

    with patch("ui._config.data_root_dir", return_value=data_root):
        p = ui_mod._entities_path()
    assert p == data_root / "itr" / "entities.yaml"
    doubled = data_root / "Data" / "itr" / "entities.yaml"
    assert p != doubled


# ---------------------------------------------------------------------------
# Filed-return cascade (rename swaps the entity-key prefix; delete archives).
# ---------------------------------------------------------------------------

def test_rename_cascades_filed_returns(tmp_path):
    data_root = _seed(tmp_path)
    itrfiled_dir = data_root / "ITRFiled"
    itrfiled_dir.mkdir(parents=True, exist_ok=True)
    (itrfiled_dir / "SYN-IND2425.json").write_text("{}", encoding="utf-8")
    (itrfiled_dir / "SYN-IND2425.pdf").write_text("x", encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "SYN-IND", "SYN-IND-RENAMED", "Synthetic Individual", "AAAAA0000A",
            "Individual", "Resident", "", "", "", "", "", "", "", "new", "",
            False, "", "", "",
        )

    assert "Saved" in msg
    assert "Cascaded filed return" in msg
    assert not (itrfiled_dir / "SYN-IND2425.json").exists()
    assert not (itrfiled_dir / "SYN-IND2425.pdf").exists()
    assert (itrfiled_dir / "SYN-IND-RENAMED2425.json").is_file()
    assert (itrfiled_dir / "SYN-IND-RENAMED2425.pdf").is_file()


def test_rename_no_filed_returns_reports_noop(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "SYN-HUF", "SYN-HUF-2", "Synthetic HUF", "BBBBB1111B",
            "HUF", "Resident", "", "", "", "", "", "", "", "new", "",
            False, "", "", "",
        )
    assert "Saved" in msg
    assert "No filed-return files to cascade" in msg


def test_rename_blocked_on_filed_return_target_collision(tmp_path):
    data_root = _seed(tmp_path)
    itrfiled_dir = data_root / "ITRFiled"
    itrfiled_dir.mkdir(parents=True, exist_ok=True)
    (itrfiled_dir / "SYN-IND2425.json").write_text("{}", encoding="utf-8")
    # A leftover/orphan filed-return file already sitting at the rename
    # target name, even though "SYN-NEW" is not (yet) an entity key.
    (itrfiled_dir / "SYN-NEW2425.json").write_text("{}", encoding="utf-8")

    entities_path = data_root / "itr" / "entities.yaml"
    before_text = entities_path.read_text(encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "SYN-IND", "SYN-NEW", "Synthetic Individual", "AAAAA0000A",
            "Individual", "Resident", "", "", "", "", "", "", "", "new", "",
            False, "", "", "",
        )

    assert "blocked" in msg.lower()
    assert entities_path.read_text(encoding="utf-8") == before_text
    assert (itrfiled_dir / "SYN-IND2425.json").is_file()
    assert (itrfiled_dir / "SYN-NEW2425.json").is_file()


def test_rename_does_not_cascade_prefix_colliding_entity_filed_returns(tmp_path):
    """CRITICAL: renaming "SynBase" must not touch "SynBaseHUF2425.json" --
    prefix-collision boundary rule, mirrored from
    test_configs_entities.test_find_filed_returns_prefix_collision_boundary."""
    data_root = _seed(tmp_path, extra={
        "SynBase": {
            "name": "Syn Base", "pan": "HHHHH7777H", "status": "Individual",
            "residency": "Resident", "default_regime": "new",
        },
        "SynBaseHUF": {
            "name": "Syn Base HUF", "pan": "IIIII8888I", "status": "HUF",
            "residency": "Resident", "default_regime": "new",
        },
    })
    itrfiled_dir = data_root / "ITRFiled"
    itrfiled_dir.mkdir(parents=True, exist_ok=True)
    (itrfiled_dir / "SynBase2425.json").write_text("{}", encoding="utf-8")
    (itrfiled_dir / "SynBaseHUF2425.json").write_text("{}", encoding="utf-8")

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "SynBase", "SynBaseRenamed", "Syn Base", "HHHHH7777H",
            "Individual", "Resident", "", "", "", "", "", "", "", "new", "",
            False, "", "", "",
        )

    assert "Saved" in msg
    assert not (itrfiled_dir / "SynBase2425.json").exists()
    assert (itrfiled_dir / "SynBaseRenamed2425.json").is_file()
    # The HUF entity's filed return is untouched.
    assert (itrfiled_dir / "SynBaseHUF2425.json").is_file()


def test_delete_archives_filed_return_files(tmp_path):
    data_root = _seed(tmp_path)
    itrfiled_dir = data_root / "ITRFiled"
    itrfiled_dir.mkdir(parents=True, exist_ok=True)
    (itrfiled_dir / "SYN-IND2425.json").write_text("{}", encoding="utf-8")
    (itrfiled_dir / "SYN-IND2425.pdf").write_text("x", encoding="utf-8")
    (itrfiled_dir / "SYN-HUF2425.json").write_text("{}", encoding="utf-8")  # unrelated

    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._delete_entity("SYN-IND", True, True)

    assert "Deleted" in msg
    assert "Archived filed-return file(s)" in msg
    assert not (itrfiled_dir / "SYN-IND2425.json").exists()
    assert not (itrfiled_dir / "SYN-IND2425.pdf").exists()
    assert (itrfiled_dir / "SYN-HUF2425.json").is_file()  # unrelated untouched

    archive_dir = data_root / "itr" / "_archive"
    archived = list(archive_dir.glob("SYN-IND2425.*.archived-*"))
    assert len(archived) == 2


def test_delete_no_filed_returns_reports_noop(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._delete_entity("SYN-HUF", True, True)
    assert "Deleted" in msg
    assert "No filed-return files existed" in msg


def test_delete_missing_itrfiled_dir_is_noop_not_error(tmp_path):
    data_root = _seed(tmp_path)
    # Data/ITRFiled/ is never created in this test.
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._delete_entity("SYN-IND", True, True)
    assert "Deleted" in msg
    assert "No filed-return files existed" in msg


# ---------------------------------------------------------------------------
# Atomicity (2026-07-25 review fix): a mid-cascade OS failure must roll back
# every move already completed and leave entities.yaml untouched -- never a
# half-applied state where entities.yaml and the on-disk files disagree.
# ---------------------------------------------------------------------------

def test_save_rename_rollback_on_mid_cascade_failure(tmp_path):
    data_root = _seed(tmp_path)
    mappings_dir = data_root / "itr" / "mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)
    old_mapping = mappings_dir / "SYN-IND.mapping.yaml"
    old_mapping.write_text(
        yaml.safe_dump([{"guid": "g1", "path": "Assets/Cash", "tag": "AL_CASH_BANK"}]),
        encoding="utf-8",
    )
    itrfiled_dir = data_root / "ITRFiled"
    itrfiled_dir.mkdir(parents=True, exist_ok=True)
    (itrfiled_dir / "SYN-IND2425.json").write_text("{}", encoding="utf-8")

    entities_path = data_root / "itr" / "entities.yaml"
    before_text = entities_path.read_text(encoding="utf-8")
    mapping_before = old_mapping.read_text(encoding="utf-8")
    filed_before = (itrfiled_dir / "SYN-IND2425.json").read_text(encoding="utf-8")

    real_move = ui_mod.shutil.move
    call_count = {"n": 0}

    def _flaky_move(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated mid-cascade failure")
        return real_move(src, dst)

    with patch("ui._config.data_root_dir", return_value=data_root), \
         patch.object(ui_mod.shutil, "move", side_effect=_flaky_move):
        msg = ui_mod._save_entity(
            "SYN-IND", "SYN-IND-RENAMED", "Synthetic Individual", "AAAAA0000A",
            "Individual", "Resident", "", "", "", "", "", "", "", "new", "",
            False, "", "", "",
        )

    assert "not saved" in msg.lower()
    assert "rolled back" in msg.lower()

    # entities.yaml is byte-identical to before -- never touched.
    assert entities_path.read_text(encoding="utf-8") == before_text
    assert not list(entities_path.parent.glob("entities.yaml.bak-*"))

    # The mapping rename (call #1, which succeeded) was undone.
    assert old_mapping.is_file()
    assert old_mapping.read_text(encoding="utf-8") == mapping_before
    assert not (mappings_dir / "SYN-IND-RENAMED.mapping.yaml").exists()

    # The filed-return rename (call #2, which failed) never took effect.
    assert (itrfiled_dir / "SYN-IND2425.json").is_file()
    assert (itrfiled_dir / "SYN-IND2425.json").read_text(encoding="utf-8") == filed_before
    assert not (itrfiled_dir / "SYN-IND-RENAMED2425.json").exists()


# ---------------------------------------------------------------------------
# Phase 3 -- per-FY GnuCash book registry (books_box / _browse_append_book /
# _derived_match_text). Synthetic paths only -- _save_entity stores strings,
# it never stats them.
# ---------------------------------------------------------------------------

def test_save_entity_with_books_round_trips(tmp_path):
    data_root = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-BOOKS", "Books Entity", "CCCCC2222C", "Individual", "Resident",
            "", "", "", "", "", "", "", "new", "", False, "", "", "",
            "2025-26 = C:\\books\\AliceDoe2526.gnucash\n2024-25 = C:\\books\\AliceDoe2425.gnucash",
        )
        assert "Saved" in msg
        entities = ui_mod._load_entities()

    e = entities["SYN-BOOKS"]
    assert e.books == {
        "2025-26": "C:\\books\\AliceDoe2526.gnucash",
        "2024-25": "C:\\books\\AliceDoe2425.gnucash",
    }


def test_save_entity_rejects_malformed_fy_key(tmp_path):
    data_root = _seed(tmp_path)
    entities_path = data_root / "itr" / "entities.yaml"
    before_text = entities_path.read_text(encoding="utf-8")

    for bad_line in (
        "2025 = C:\\books\\x.gnucash",
        "AY2025-26 = C:\\books\\x.gnucash",
        "2026-27-1 = C:\\books\\x.gnucash",
    ):
        with patch("ui._config.data_root_dir", return_value=data_root):
            msg = ui_mod._save_entity(
                "", "SYN-BADFY", "Bad FY", "DDDDD3333D", "Individual", "Resident",
                "", "", "", "", "", "", "", "new", "", False, "", "", "",
                bad_line,
            )
        assert "Not saved" in msg
        assert "financial year" in msg
        assert entities_path.read_text(encoding="utf-8") == before_text


def test_entity_to_form_books_line_present_and_blank(tmp_path):
    data_root = _seed(tmp_path, extra={
        "SYN-BOOKS2": {
            "name": "Books Two", "pan": "EEEEE4444E", "status": "Individual",
            "residency": "Resident", "default_regime": "new",
            "books": {"2025-26": "C:\\books\\BobDoe2526.gnucash"},
        },
    })
    with patch("ui._config.data_root_dir", return_value=data_root):
        entities = ui_mod._load_entities()
        form_with_books = ui_mod._entity_to_form("SYN-BOOKS2", entities)
        form_without_books = ui_mod._entity_to_form("SYN-IND", entities)
        form_new = ui_mod._entity_to_form("", entities)

    assert form_with_books[-1] == "2025-26 = C:\\books\\BobDoe2526.gnucash"
    assert form_without_books[-1] == ""
    assert form_new[-1] == ""
    assert len(form_new) == len(form_with_books)


def test_derived_match_text_override_wins():
    text = ui_mod._derived_match_text(
        "2025-26 = C:\\books\\AliceDoe2526.gnucash", "ManualOverride"
    )
    assert "ManualOverride" in text
    assert "explicit override" in text


def test_derived_match_text_newest_fy_derived():
    books_text = (
        "2024-25 = C:\\books\\AliceDoe2425.gnucash\n"
        "2025-26 = C:\\books\\AliceDoe2526.gnucash"
    )
    text = ui_mod._derived_match_text(books_text, "")
    assert "`AliceDoe`" in text
    assert "2025-26" in text


def test_derived_match_text_huf_suffix_preserved():
    text = ui_mod._derived_match_text("2025-26 = C:\\books\\BobDoeHUF2526.gnucash", "")
    assert "`BobDoeHUF`" in text


def test_derived_match_text_none_yet():
    text = ui_mod._derived_match_text("", "")
    assert "none yet" in text


def test_browse_append_book_valid_fy(monkeypatch):
    called = {"n": 0}

    def _fake_pick_files(*args, **kwargs):
        called["n"] += 1
        return (["C:\\books\\AliceDoe2526.gnucash"], [])

    monkeypatch.setattr(ui_mod._filedialog, "pick_files", _fake_pick_files)
    monkeypatch.setattr(ui_mod.gr, "Warning", lambda *a, **k: None)

    result = ui_mod._browse_append_book("2024-25 = C:\\books\\AliceDoe2425.gnucash", "2025-26")

    assert called["n"] == 1
    books, errors = ui_mod._parse_kv_lines(result["value"])
    assert errors == []
    assert books["2025-26"] == "C:\\books\\AliceDoe2526.gnucash"
    assert books["2024-25"] == "C:\\books\\AliceDoe2425.gnucash"


def test_browse_append_book_replaces_existing_fy_line(monkeypatch):
    def _fake_pick_files(*args, **kwargs):
        return (["C:\\books\\AliceDoe2526-v2.gnucash"], [])

    monkeypatch.setattr(ui_mod._filedialog, "pick_files", _fake_pick_files)
    monkeypatch.setattr(ui_mod.gr, "Warning", lambda *a, **k: None)

    result = ui_mod._browse_append_book("2025-26 = C:\\books\\AliceDoe2526-v1.gnucash", "2025-26")
    books, _errors = ui_mod._parse_kv_lines(result["value"])
    assert books["2025-26"] == "C:\\books\\AliceDoe2526-v2.gnucash"


def test_browse_append_book_malformed_fy_leaves_text_unchanged(monkeypatch):
    called = {"n": 0}

    def _fake_pick_files(*args, **kwargs):
        called["n"] += 1
        return (["C:\\books\\AliceDoe2526.gnucash"], [])

    monkeypatch.setattr(ui_mod._filedialog, "pick_files", _fake_pick_files)
    monkeypatch.setattr(ui_mod.gr, "Warning", lambda *a, **k: None)

    original_text = "2024-25 = C:\\books\\AliceDoe2425.gnucash"
    result = ui_mod._browse_append_book(original_text, "not-a-fy")

    assert called["n"] == 0
    # gr.update() with no value set carries no "value" key -- box unchanged.
    assert "value" not in result or result.get("value") is None


def test_books_added_to_one_entity_leaves_other_byte_stable(tmp_path):
    data_root = _seed(tmp_path)
    entities_path = data_root / "itr" / "entities.yaml"

    with patch("ui._config.data_root_dir", return_value=data_root):
        entities_before = ui_mod._load_entities()
        huf_dump_before = configs.dump_entities({"SYN-HUF": entities_before["SYN-HUF"]})

        msg = ui_mod._save_entity(
            "SYN-IND", "SYN-IND", "Synthetic Individual", "AAAAA0000A",
            "Individual", "Resident", "", "", "", "", "", "", "", "new", "",
            False, "", "", "",
            "2025-26 = C:\\books\\AliceDoe2526.gnucash",
        )
        assert "Saved" in msg

        entities_after = ui_mod._load_entities()

    assert entities_after["SYN-IND"].books == {"2025-26": "C:\\books\\AliceDoe2526.gnucash"}
    huf_dump_after = configs.dump_entities({"SYN-HUF": entities_after["SYN-HUF"]})
    assert huf_dump_before == huf_dump_after


def test_delete_rollback_on_mid_cascade_failure(tmp_path):
    data_root = _seed(tmp_path)
    mappings_dir = data_root / "itr" / "mappings"
    mappings_dir.mkdir(parents=True, exist_ok=True)
    mapping_file = mappings_dir / "SYN-IND.mapping.yaml"
    mapping_file.write_text(
        yaml.safe_dump([{"guid": "g1", "path": "Assets/Cash", "tag": "AL_CASH_BANK"}]),
        encoding="utf-8",
    )
    itrfiled_dir = data_root / "ITRFiled"
    itrfiled_dir.mkdir(parents=True, exist_ok=True)
    (itrfiled_dir / "SYN-IND2425.json").write_text("{}", encoding="utf-8")

    entities_path = data_root / "itr" / "entities.yaml"
    before_text = entities_path.read_text(encoding="utf-8")
    mapping_before = mapping_file.read_text(encoding="utf-8")
    filed_before = (itrfiled_dir / "SYN-IND2425.json").read_text(encoding="utf-8")

    real_move = ui_mod.shutil.move
    call_count = {"n": 0}

    def _flaky_move(src, dst):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise OSError("simulated mid-cascade failure")
        return real_move(src, dst)

    with patch("ui._config.data_root_dir", return_value=data_root), \
         patch.object(ui_mod.shutil, "move", side_effect=_flaky_move):
        msg = ui_mod._delete_entity("SYN-IND", True, True)

    assert "not deleted" in msg.lower()
    assert "rolled back" in msg.lower()

    # entities.yaml is byte-identical to before -- never touched.
    assert entities_path.read_text(encoding="utf-8") == before_text
    assert not list(entities_path.parent.glob("entities.yaml.bak-*"))

    # The mapping archive-move (call #1, which succeeded) was undone.
    assert mapping_file.is_file()
    assert mapping_file.read_text(encoding="utf-8") == mapping_before

    # The filed-return archive-move (call #2, which failed) never took effect.
    assert (itrfiled_dir / "SYN-IND2425.json").is_file()
    assert (itrfiled_dir / "SYN-IND2425.json").read_text(encoding="utf-8") == filed_before

    archive_dir = data_root / "itr" / "_archive"
    assert not archive_dir.exists() or not list(archive_dir.iterdir())
