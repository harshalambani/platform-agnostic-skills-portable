"""An entity key containing a path separator, a drive colon or '..' is refused on add and on
rename, before anything is written or moved. Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ui.tabs import itr_entities as ui_mod       # noqa: E402

BAD_KEYS = ["a/b", "a\\b", "..", "../x", "C:foo", ".", "a\x00b"]


def _entity(name="Synthetic Entity", pan="AAAAA0000A"):
    return {"name": name, "pan": pan, "status": "Individual", "residency": "Resident", "default_regime": "new"}


def _seed(tmp_path):
    root = tmp_path / "Data"
    ent = root / "itr" / "entities.yaml"
    maps = root / "itr" / "mappings"
    maps.mkdir(parents=True)
    ent.write_text(yaml.safe_dump({"SYN-P": _entity()}, sort_keys=True), encoding="utf-8")
    (maps / "SYN-P.mapping.yaml").write_text("rules: []\n", encoding="utf-8")
    return root, ent, maps


def _save(root, orig, new):
    with patch("ui._config.data_root_dir", return_value=root):
        e = ui_mod._load_entities().get("SYN-P") if orig else None
        name, pan = (e.name, e.pan) if e else ("Synthetic Entity", "AAAAA0000A")
        return ui_mod._save_entity(orig, new, name, pan, "Individual", "Resident", "", "", "", "", "", "", "",
                                   "new", "", False, "", "", "")


def _tree(root):
    return sorted(str(p.relative_to(root.parent)) for p in root.parent.rglob("*"))


@pytest.mark.parametrize("bad", BAD_KEYS)
def test_bad_key_is_refused_on_a_new_entity(tmp_path, bad):          # NEGATIVE
    root, ent, maps = _seed(tmp_path)
    before, tree = ent.read_bytes(), _tree(root)
    msg = _save(root, "", bad)
    assert msg.startswith("Error") and "nothing was saved" in msg
    assert ent.read_bytes() == before and _tree(root) == tree


@pytest.mark.parametrize("bad", BAD_KEYS)
def test_bad_key_is_refused_on_a_rename_and_nothing_moves(tmp_path, bad):   # NEGATIVE
    root, ent, maps = _seed(tmp_path)
    before, tree = ent.read_bytes(), _tree(root)
    msg = _save(root, "SYN-P", bad)
    assert msg.startswith("Error") and "nothing was saved" in msg
    assert (maps / "SYN-P.mapping.yaml").is_file()
    assert ent.read_bytes() == before
    assert _tree(root) == tree            # nothing appeared anywhere, inside or outside the folder


def test_a_normal_rename_still_moves_the_mapping_file(tmp_path):
    root, ent, maps = _seed(tmp_path)
    msg = _save(root, "SYN-P", "SYN-Q")
    assert not msg.startswith("Error"), msg
    assert (maps / "SYN-Q.mapping.yaml").is_file() and not (maps / "SYN-P.mapping.yaml").exists()
    assert "SYN-Q" in yaml.safe_load(ent.read_text(encoding="utf-8"))


@pytest.mark.parametrize("ok", ["SYN P", "SYN.P2", "syn-p_3"])
def test_keys_with_spaces_or_an_inner_dot_still_save(tmp_path, ok):
    root, ent, maps = _seed(tmp_path)
    msg = _save(root, "", ok)
    assert not msg.startswith("Error: entity key"), msg
    assert ok in yaml.safe_load(ent.read_text(encoding="utf-8"))
