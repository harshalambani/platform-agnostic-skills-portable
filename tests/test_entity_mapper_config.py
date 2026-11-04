"""
MAP-34 / MAP-35 plumbing: the per-entity mapper settings in entities.yaml
(`drawings_accounts`, `card_spend_default_account`), their round trip, the
Entities tab never dropping fields its form does not show, and the pipeline's
entity loader. Synthetic data only.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
for _p in (str(SCRIPTS), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import configs  # noqa: E402
from ui.tabs import itr_entities as ui_mod  # noqa: E402
from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402


def _seed(tmp_path, extra=None):
    ent = {"name": "Synthetic Individual", "pan": "AAAAA0000A", "status": "Individual",
           "residency": "Resident", "default_regime": "new"}
    ent.update(extra or {})
    data_root = tmp_path / "Data"
    p = data_root / "itr" / "entities.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump({"SYN-IND": ent, "SYN-OTHER": dict(ent, name="Synthetic Other",
                                                                    pan="BBBBB1111B")}),
                 encoding="utf-8")
    return data_root, p


HIDDEN = {
    "drawings_accounts": ["Equity:Drawings"],
    "card_spend_default_account": "Equity:Drawings",
    "reimbursement_markers": ["ZZRB", "OTHERTAG"],
    "partner_comp_accounts": {"bank": "Assets:Bank"},
    "foreign_dividends_in_book": True,
    "foreign_dividends_in_book_by_ay": {"2026-27": True},
}


def test_fields_round_trip_through_load_and_dump(tmp_path):
    _, p = _seed(tmp_path, HIDDEN)
    ents = configs.load_entities(p)
    assert ents["SYN-IND"].drawings_accounts == ["Equity:Drawings"]
    assert ents["SYN-IND"].card_spend_default_account == "Equity:Drawings"
    again = yaml.safe_load(configs.dump_entities(ents))["SYN-IND"]
    assert again["drawings_accounts"] == ["Equity:Drawings"]
    assert again["card_spend_default_account"] == "Equity:Drawings"


def test_absent_fields_default_empty_and_are_not_emitted(tmp_path):         # NEGATIVE
    _, p = _seed(tmp_path)
    ents = configs.load_entities(p)
    assert ents["SYN-IND"].drawings_accounts == []
    assert ents["SYN-IND"].card_spend_default_account == ""
    out = yaml.safe_load(configs.dump_entities(ents))["SYN-IND"]
    assert "drawings_accounts" not in out and "card_spend_default_account" not in out


def _modify(data_root, orig, new):
    with patch("ui._config.data_root_dir", return_value=data_root):
        return ui_mod._save_entity(
            orig, new, "Synthetic Individual Renamed", "AAAAA0000A", "Individual", "Resident",
            "1990-01-01", "", "", "", "", "", "", "new", "", False, "", "", "")


def test_saving_an_entity_keeps_the_fields_the_form_does_not_show(tmp_path):
    data_root, p = _seed(tmp_path, HIDDEN)
    assert "Saved" in _modify(data_root, "SYN-IND", "SYN-IND")
    got = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-IND"]
    assert got["name"] == "Synthetic Individual Renamed"          # the edit applied
    for k, v in HIDDEN.items():
        assert got[k] == v, k                                    # nothing dropped


def test_renaming_the_key_keeps_them_too(tmp_path):
    data_root, p = _seed(tmp_path, HIDDEN)
    assert "Saved" in _modify(data_root, "SYN-IND", "SYN-RENAMED")
    got = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "SYN-IND" not in got
    for k, v in HIDDEN.items():
        assert got["SYN-RENAMED"][k] == v, k


def test_a_new_entity_does_not_inherit_another_entitys_hidden_fields(tmp_path):   # NEGATIVE
    data_root, p = _seed(tmp_path, HIDDEN)
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._save_entity(
            "", "SYN-NEW", "Synthetic New", "CCCCC2222C", "Individual", "Resident",
            "1990-01-01", "", "", "", "", "", "", "new", "", False, "", "", "")
    assert "Saved" in msg
    got = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-NEW"]
    for k in HIDDEN:
        assert k not in got, k


def test_other_entities_are_untouched_by_a_save(tmp_path):                   # NEGATIVE
    data_root, p = _seed(tmp_path, HIDDEN)
    before = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-OTHER"]
    _modify(data_root, "SYN-IND", "SYN-IND")
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-OTHER"] == before


# ------------------------------------------------------- pipeline loader

def test_loader_none_when_no_entity_is_picked(tmp_path):
    assert pipe._load_entity_profile("", str(tmp_path / "missing.yaml")) == (None, None)
    assert pipe._load_entity_profile(None, None) == (None, None)


def test_loader_returns_the_profile(tmp_path):
    _, p = _seed(tmp_path, HIDDEN)
    prof, err = pipe._load_entity_profile("SYN-IND", str(p))
    assert err is None and prof.drawings_accounts == ["Equity:Drawings"]


def test_loader_fails_loud_for_an_unknown_entity_or_unreadable_file(tmp_path):   # NEGATIVE
    _, p = _seed(tmp_path)
    prof, err = pipe._load_entity_profile("NOPE", str(p))
    assert prof is None and "NOPE" in err
    prof, err = pipe._load_entity_profile("SYN-IND", str(tmp_path / "missing.yaml"))
    assert prof is None and err and "entities.yaml" in err


def test_skill_yaml_passes_entity_without_letting_it_name_the_output():
    sk = yaml.safe_load((ROOT / "src/agents/skill_gnucash_pipeline/skill.yaml")
                        .read_text(encoding="utf-8"))
    assert sk["run_args"]["entity"] == "{inputs.entity}"
    assert sk["run_args"]["entities_path"] == "{data_root}/itr/entities.yaml"
    names = [i["name"] for i in sk["inputs"]]
    # UI-15: Entity leads the form. It still must not name the output file
    # (ui/tabs/_generic.py _output_name_source skips entity selects).
    assert names.index("entity") < names.index("bank") < names.index("gnucash_file")


# --------------------------------- Entities tab fields for the mapper settings

def _modify_with(data_root, drawings, card):
    with patch("ui._config.data_root_dir", return_value=data_root):
        return ui_mod._save_entity(
            "SYN-IND", "SYN-IND", "Synthetic Individual", "AAAAA0000A", "Individual", "Resident",
            "1990-01-01", "", "", "", "", "", "", "new", "", False, "", "", "",
            drawings_accounts_text=drawings, card_spend_default_account=card)


OTHER_HIDDEN = {k: v for k, v in HIDDEN.items()
                if k not in ("drawings_accounts", "card_spend_default_account")}


def test_form_loads_the_two_fields(tmp_path):
    data_root, p = _seed(tmp_path, dict(HIDDEN, drawings_accounts=["Equity:Drawings", "Equity:D2"]))
    with patch("ui._config.data_root_dir", return_value=data_root):
        ents = ui_mod._load_entities()
    form = ui_mod._entity_to_form("SYN-IND", ents)
    assert form[-3] == "Equity:Drawings\nEquity:D2" and form[-2] == "Equity:Drawings"
    blank = ui_mod._entity_to_form("", ents)
    assert blank[-3] == "" and blank[-2] == "" and blank[-1] == "" and len(blank) == len(form)


def test_saving_the_two_fields_keeps_partner_and_foreign_dividend_keys(tmp_path):
    data_root, p = _seed(tmp_path, OTHER_HIDDEN)
    assert "Saved" in _modify_with(data_root, "Equity:Drawings\n  Equity:Second \n\n", " Equity:Card ")
    got = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-IND"]
    assert got["drawings_accounts"] == ["Equity:Drawings", "Equity:Second"]
    assert got["card_spend_default_account"] == "Equity:Card"
    for k, v in OTHER_HIDDEN.items():
        assert got[k] == v, k


def test_saving_without_the_two_fields_keeps_them(tmp_path):                  # NEGATIVE (vice versa)
    data_root, p = _seed(tmp_path, HIDDEN)
    assert "Saved" in _modify_with(data_root, None, None)
    got = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-IND"]
    for k, v in HIDDEN.items():
        assert got[k] == v, k


def test_empty_fields_remove_the_keys_and_leave_the_rest(tmp_path):           # NEGATIVE
    data_root, p = _seed(tmp_path, HIDDEN)
    assert "Saved" in _modify_with(data_root, "  \n", "")
    got = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-IND"]
    assert "drawings_accounts" not in got and "card_spend_default_account" not in got
    for k, v in OTHER_HIDDEN.items():
        assert got[k] == v, k
    other = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-OTHER"]
    assert other["drawings_accounts"] == ["Equity:Drawings"]                  # other entity untouched


def _seed2(tmp_path, a_markers=None, b_markers=None):
    ent = {"name": "Synthetic Individual", "pan": "AAAAA0000A", "status": "Individual",
           "residency": "Resident", "default_regime": "new"}
    a, b = dict(ent), dict(ent, name="Synthetic Other", pan="BBBBB1111B")
    if a_markers is not None:
        a["reimbursement_markers"] = a_markers
    if b_markers is not None:
        b["reimbursement_markers"] = b_markers
    data_root = tmp_path / "Data"
    p = data_root / "itr" / "entities.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump({"SYN-IND": a, "SYN-OTHER": b}), encoding="utf-8")
    return data_root, p


def _save_markers(data_root, text, key="SYN-IND"):
    with patch("ui._config.data_root_dir", return_value=data_root):
        return ui_mod._save_entity(
            key, key, "Synthetic Individual", "AAAAA0000A", "Individual", "Resident",
            "1990-01-01", "", "", "", "", "", "", "new", "", False, "", "", "",
            reimbursement_markers_text=text)


def test_reimbursement_markers_round_trip_through_load_and_dump(tmp_path):
    _, p = _seed2(tmp_path, [" ZZRB ", "", "  ", "OTHERTAG"])
    ents = configs.load_entities(p)
    assert ents["SYN-IND"].reimbursement_markers == ["ZZRB", "OTHERTAG"]     # trimmed, blanks dropped
    again = yaml.safe_load(configs.dump_entities(ents))["SYN-IND"]
    assert again["reimbursement_markers"] == ["ZZRB", "OTHERTAG"]
    assert ents["SYN-OTHER"].reimbursement_markers == []                       # NEGATIVE: default empty
    assert "reimbursement_markers" not in yaml.safe_load(configs.dump_entities(ents))["SYN-OTHER"]


def test_ui_save_then_load_round_trips_markers_comma_or_lines(tmp_path):
    data_root, p = _seed2(tmp_path)
    assert "Saved" in _save_markers(data_root, " ZZRB , ,TAG2" + chr(10) + "  TAG3  " + chr(10) + chr(10))
    assert configs.load_entities(p)["SYN-IND"].reimbursement_markers == ["ZZRB", "TAG2", "TAG3"]
    with patch("ui._config.data_root_dir", return_value=data_root):
        form = ui_mod._entity_to_form("SYN-IND", ui_mod._load_entities())
    assert form[-1] == "ZZRB" + chr(10) + "TAG2" + chr(10) + "TAG3"


def test_saving_with_the_field_empty_yields_empty_list(tmp_path):              # NEGATIVE
    data_root, p = _seed2(tmp_path, ["ZZRB"])
    assert "Saved" in _save_markers(data_root, "  " + chr(10) + " , ")
    assert configs.load_entities(p)["SYN-IND"].reimbursement_markers == []
    assert "reimbursement_markers" not in yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-IND"]
    # a brand-new entity saved with the field empty also gets none (no default)
    with patch("ui._config.data_root_dir", return_value=data_root):
        ui_mod._save_entity("", "SYN-NEW", "Synthetic New", "CCCCC2222C", "Individual", "Resident",
                            "1990-01-01", "", "", "", "", "", "", "new", "", False, "", "", "",
                            reimbursement_markers_text="")
    assert configs.load_entities(p)["SYN-NEW"].reimbursement_markers == []


def test_a_save_that_omits_the_field_keeps_stored_markers(tmp_path):           # NEGATIVE (older caller)
    data_root, p = _seed2(tmp_path, ["ZZRB"])
    assert "Saved" in _save_markers(data_root, None)
    assert configs.load_entities(p)["SYN-IND"].reimbursement_markers == ["ZZRB"]


def test_editing_entity_a_markers_never_changes_entity_b(tmp_path):            # NEGATIVE
    data_root, p = _seed2(tmp_path, ["ZZRB"], ["BTAG"])
    assert "Saved" in _save_markers(data_root, "NEWTAG")
    ents = configs.load_entities(p)
    assert ents["SYN-IND"].reimbursement_markers == ["NEWTAG"]
    assert ents["SYN-OTHER"].reimbursement_markers == ["BTAG"]
