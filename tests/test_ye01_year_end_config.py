"""tests/test_ye01_year_end_config.py -- YE-01 PR 1: the year-end folder settings.

Per entity: ``folder_name`` in entities.yaml (one path segment). Global: ``drive_accounts_root`` and
``gnucash_profile_root`` in the portable config.yaml, edited on the Entities screen. Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import configs                                                       # noqa: E402
from tests.test_ui35_entities_all_fields import _call, _profiles, _seed   # noqa: E402
from ui.tabs import itr_entities as ui_mod                             # noqa: E402


def _raw(p):
    return yaml.safe_load(p.read_text(encoding="utf-8"))


# ---- folder_name: round trip ----

def test_folder_name_round_trips_and_is_written_to_the_yaml(tmp_path):
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, folder_name="Syn Folder").startswith("**Saved**")
    assert _raw(p)["SYN-P"]["folder_name"] == "Syn Folder"
    assert _profiles(p)["SYN-P"].folder_name == "Syn Folder"
    assert ui_mod._entity_to_new_form("SYN-P", _profiles(p))[8] == "Syn Folder"


def test_a_later_save_that_does_not_mention_it_keeps_it(tmp_path):
    root, p = _seed(tmp_path)
    _call("SYN-P", root, folder_name="Syn Folder")
    assert _call("SYN-P", root).startswith("**Saved**")                       # an older caller (None)
    assert _call("SYN-P", root, cc_cred_max_overage="20").startswith("**Saved**")     # an unrelated field
    assert _profiles(p)["SYN-P"].folder_name == "Syn Folder"


def test_empty_clears_it_and_the_key_leaves_the_file(tmp_path):
    root, p = _seed(tmp_path)
    _call("SYN-P", root, folder_name="Syn Folder")
    assert _call("SYN-P", root, folder_name="").startswith("**Saved**")
    assert _profiles(p)["SYN-P"].folder_name == ""
    assert "folder_name" not in _raw(p)["SYN-P"]


# ---- folder_name: refused values are NOT written ----

@pytest.mark.parametrize("bad", ["a/b", "a\\b", "..", "../x", "a..b", ".", " ", "  x", "x ", "x.", "C:", "a|b", "a*b", "a\tb"])
def test_an_invalid_folder_name_is_refused_and_not_written(tmp_path, bad):          # NEGATIVE
    root, p = _seed(tmp_path)
    _call("SYN-P", root, folder_name="Good Name")
    before = p.read_text(encoding="utf-8")
    msg = _call("SYN-P", root, folder_name=bad)
    assert "Not saved" in msg and "Folder name" in msg
    assert p.read_text(encoding="utf-8") == before                                  # the file is byte-identical
    assert _profiles(p)["SYN-P"].folder_name == "Good Name"


def test_a_refused_folder_name_also_blocks_the_other_fields_in_that_save(tmp_path):    # NEGATIVE
    root, p = _seed(tmp_path)
    msg = _call("SYN-P", root, folder_name="a/b", cc_cred_max_overage="25")
    assert "Not saved" in msg
    assert _profiles(p)["SYN-P"].cc_cred_max_overage is None


def test_the_unset_default_is_not_an_error(tmp_path):                               # NEGATIVE
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, folder_name="").startswith("**Saved**")
    assert "folder_name" not in _raw(p)["SYN-P"]


# ---- an entity without folder_name, and nothing else moves ----

def test_an_entity_without_folder_name_loads_and_saves_unchanged(tmp_path):
    root, p = _seed(tmp_path)
    assert _profiles(p)["SYN-P"].folder_name == ""
    assert _call("SYN-P", root).startswith("**Saved**")
    after = _raw(p)
    assert "folder_name" not in after["SYN-P"] and "folder_name" not in after["SYN-O"]
    assert ui_mod._entity_to_new_form("SYN-P", _profiles(p))[8] == ""


def test_setting_a_folder_name_changes_only_that_key_and_that_entity(tmp_path):
    root, p = _seed(tmp_path)
    _call("SYN-P", root)                                   # normalise the file through the writer once
    before = _raw(p)
    before_o_text = p.read_text(encoding="utf-8").split("SYN-P:")[0]
    _call("SYN-P", root, folder_name="Syn Folder")
    after = _raw(p)
    assert after["SYN-O"] == before["SYN-O"]                                         # NEGATIVE: the other entity
    expected = dict(before["SYN-P"], folder_name="Syn Folder")
    assert after["SYN-P"] == expected                                                # nothing else lost or changed
    assert list(after) == list(before)                                               # entity order unchanged
    assert p.read_text(encoding="utf-8").split("SYN-P:")[0] == before_o_text


def test_one_entitys_folder_name_never_lands_on_another(tmp_path):                   # NEGATIVE
    root, p = _seed(tmp_path)
    _call("SYN-P", root, folder_name="One")
    _call("SYN-O", root, folder_name="Two")
    got = _profiles(p)
    assert got["SYN-P"].folder_name == "One" and got["SYN-O"].folder_name == "Two"
    _call("SYN-P", root, folder_name="")
    got = _profiles(p)
    assert got["SYN-P"].folder_name == "" and got["SYN-O"].folder_name == "Two"


def test_nothing_else_in_the_entities_file_is_lost_by_a_folder_name_save(tmp_path):
    root, p = _seed(tmp_path)
    before = _raw(p)["SYN-P"]
    _call("SYN-P", root, folder_name="Syn Folder")
    after = _raw(p)["SYN-P"]
    after.pop("folder_name")
    assert after.keys() >= before.keys()                                             # no key dropped
    for k in before:
        assert after[k] == before[k], k


def test_the_example_file_still_loads(tmp_path):
    ents = configs.load_entities(ROOT / "bundling" / "canonical" / "itr" / "entities.example.yaml")
    assert ents and all(isinstance(e.folder_name, str) for e in ents.values())


# ---- the check functions ----

def test_check_folder_name_accepts_ordinary_names():
    for ok in ("Syn Folder", "SYN-P", "A_b.c", "Family (HUF)"):
        assert configs.check_folder_name(ok) is None, ok


def test_check_folder_name_message_names_the_value():
    assert "a/b" in configs.check_folder_name("a/b")
    assert "blank" in configs.check_folder_name("   ")


# ---- the two global roots ----

def _cfg(tmp_path):
    p = tmp_path / "settings" / "config.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump({"active_endpoint": "x", "cc_large_spend_threshold": 25000.0,
                                 "endpoints": {"x": {"model": "m"}}}, sort_keys=False), encoding="utf-8")
    return p


def test_roots_default_to_not_set_and_never_to_a_real_path(tmp_path):
    p = _cfg(tmp_path)
    assert configs.read_year_end_roots(p) == {"drive_accounts_root": "", "gnucash_profile_root": ""}
    assert configs.read_year_end_roots(tmp_path / "missing.yaml") == {"drive_accounts_root": "", "gnucash_profile_root": ""}
    assert configs.read_year_end_roots(None) == {"drive_accounts_root": "", "gnucash_profile_root": ""}


def test_roots_round_trip_and_other_config_keys_are_kept(tmp_path):
    p = _cfg(tmp_path)
    d, g = tmp_path / "drive", tmp_path / "profile"
    d.mkdir()
    g.mkdir()
    before = _raw(p)
    assert ui_mod._save_year_end_roots(str(d), str(g), settings_path=p).startswith("**Saved**")
    after = _raw(p)
    assert after["drive_accounts_root"] == str(d) and after["gnucash_profile_root"] == str(g)
    for k, v in before.items():
        assert after[k] == v, k                                                      # nothing else lost
    assert list(after)[:len(before)] == list(before)                                 # nothing reordered
    assert configs.read_year_end_roots(p) == {"drive_accounts_root": str(d), "gnucash_profile_root": str(g)}


def test_a_missing_folder_is_refused_and_nothing_is_written(tmp_path):               # NEGATIVE
    p = _cfg(tmp_path)
    d = tmp_path / "drive"
    d.mkdir()
    before = p.read_text(encoding="utf-8")
    msg = ui_mod._save_year_end_roots(str(d), str(tmp_path / "nope"), settings_path=p)
    assert "Not saved" in msg and "GnuCash profile root" in msg and "does not exist" in msg
    assert p.read_text(encoding="utf-8") == before                                   # not even the valid half
    assert configs.read_year_end_roots(p)["drive_accounts_root"] == ""


def test_a_file_is_refused_as_a_root(tmp_path):                                      # NEGATIVE
    p = _cfg(tmp_path)
    f = tmp_path / "a_file.txt"
    f.write_text("x", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    msg = ui_mod._save_year_end_roots(str(f), "", settings_path=p)
    assert "Not saved" in msg and "Drive Accounts root" in msg and "not a folder" in msg
    assert p.read_text(encoding="utf-8") == before


def test_a_relative_path_is_refused_as_a_root(tmp_path, monkeypatch):                # NEGATIVE
    p = _cfg(tmp_path)
    (tmp_path / "rel").mkdir()
    monkeypatch.chdir(tmp_path)
    before = p.read_text(encoding="utf-8")
    msg = ui_mod._save_year_end_roots("rel", "", settings_path=p)
    assert "Not saved" in msg and "full path" in msg
    assert p.read_text(encoding="utf-8") == before


def test_blank_clears_a_root_and_leaves_the_other(tmp_path):
    p = _cfg(tmp_path)
    d, g = tmp_path / "drive", tmp_path / "profile"
    d.mkdir()
    g.mkdir()
    ui_mod._save_year_end_roots(str(d), str(g), settings_path=p)
    msg = ui_mod._save_year_end_roots("", str(g), settings_path=p)
    assert msg.startswith("**Saved**") and "Not set" in msg
    got = configs.read_year_end_roots(p)
    assert got == {"drive_accounts_root": "", "gnucash_profile_root": str(g)}
    assert "drive_accounts_root" not in _raw(p)


def test_saving_the_roots_never_touches_entities_yaml_and_the_reverse(tmp_path):     # NEGATIVE
    root, ep = _seed(tmp_path / "e")
    cp = _cfg(tmp_path / "c")
    d = tmp_path / "drive"
    d.mkdir()
    e_before, c_before = ep.read_bytes(), cp.read_bytes()
    ui_mod._save_year_end_roots(str(d), "", settings_path=cp)
    assert ep.read_bytes() == e_before
    cp_after = cp.read_bytes()
    _call("SYN-P", root, folder_name="Syn Folder")
    assert cp.read_bytes() == cp_after != c_before


def test_the_roots_do_not_live_in_entities_yaml(tmp_path):                           # NEGATIVE
    root, ep = _seed(tmp_path / "e")
    cp = _cfg(tmp_path / "c")
    d = tmp_path / "drive"
    d.mkdir()
    ui_mod._save_year_end_roots(str(d), "", settings_path=cp)
    _call("SYN-P", root, folder_name="X")
    text = ep.read_text(encoding="utf-8")
    assert "drive_accounts_root" not in text and "gnucash_profile_root" not in text
    assert set(_raw(ep)) == {"SYN-P", "SYN-O"}                       # still only entities at the top level
