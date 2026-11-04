"""
tests/skill_itr_workbook/test_itr05_schedule_al_firm_foreign.py -- ITR-05:
Schedule AL carries interest in a firm / AOP on its own line (AL_FIRM_INTEREST,
firm PAN from the entity), and Review Mapping can set and clear the AL_FOREIGN
flag. Synthetic data only (fictitious firm PAN AAAAA0000A style).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULES_DIR = ROOT / "bundling" / "canonical" / "itr" / "rules"
for p in (str(SCRIPTS), str(SCRIPTS.parent), str(Path(__file__).resolve().parent), str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import apply_mapping_corrections as amc  # noqa: E402
import configs  # noqa: E402
import fixture_gen  # noqa: E402
import mapping as mapping_engine  # noqa: E402
import parse_eguile as pe  # noqa: E402
import parse_form16  # noqa: E402
import parse_gnucash as pg  # noqa: E402
import rules as rules_engine  # noqa: E402
import schedules as sch  # noqa: E402
import tags as tag_vocab  # noqa: E402
import write_workbook as ww  # noqa: E402
from ui.tabs import itr_mapping_review as ui_mod  # noqa: E402

SEC_GUID = "a892f15fe43fd86af7512ba45740ec6a"      # Assets/Investments/SynthCorp Shares (AL_SECURITIES)
CASH_GUID = "2af1901149441464ce63bf4184a66e15"     # Assets/Cash and Bank/Cash (AL_CASH_BANK)
INCOME_GUID = "11fc5a724efb9eb161ac039825b3e6dd"   # Income/Bank Interest
FIRM_PAN = "BBBBB1111B"
YEAR_KEY, REGIME = "2024-25", "new"


def _write_mapping(tmp_path, edits=None, flags=None) -> Path:
    entries = yaml.safe_load((FIXTURES / "syn_ind.mapping.yaml").read_text(encoding="utf-8"))
    for e in entries:
        if edits and e["guid"] in edits:
            e["tag"] = edits[e["guid"]]
        if flags and e["guid"] in flags:
            e["flags"] = list(flags[e["guid"]])
    path = tmp_path / "SYN-IND.mapping.yaml"
    path.write_text(yaml.safe_dump(entries), encoding="utf-8")
    return path


def _build(mapping_path, firm_pan=""):
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    book = pg.parse_book(FIXTURES / "syn_ind.gnucash")
    loaded = configs.load_mapping(mapping_path)
    result = mapping_engine.resolve_tree(tree, loaded)
    form16 = parse_form16.parse_form16(FIXTURES / "syn_ind_form16.pdf")
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    entity = configs.load_entities(ROOT / "bundling" / "canonical" / "itr" / "entities.example.yaml")["SYN-IND"]
    scrips = configs.load_scrips(ROOT / "bundling" / "canonical" / "itr" / "scrips.example.yaml")
    model = sch.build_all_schedules(
        tree, result.resolved, book, form16, YEAR_KEY, rules, REGIME, entity.status, entity.dob,
        scrips, sch.load_fmv_tables(), firm_pan=firm_pan,
    )
    return model, (tree, result, loaded, rules, entity, form16)


# ---------------------------------------------------------------------------
# The tag
# ---------------------------------------------------------------------------

def test_tag_exists_on_the_balance_sheet_side_only():
    assert tag_vocab.is_valid_tag("AL_FIRM_INTEREST")
    assert "AL_FIRM_INTEREST" in tag_vocab.AL_ASSET_TAGS
    assert "AL_FIRM_INTEREST" in tag_vocab.BS_TAGS and "AL_FIRM_INTEREST" not in tag_vocab.RE_TAGS


# ---------------------------------------------------------------------------
# NEGATIVE: firm interest is never added to the shares-and-securities line
# ---------------------------------------------------------------------------

def test_firm_interest_has_its_own_line_and_never_joins_securities(tmp_path):
    base, _ = _build(_write_mapping(tmp_path))
    sec = base.schedule_al.buckets["AL_SECURITIES"]
    assert sec > 0

    moved, _ = _build(_write_mapping(tmp_path, edits={SEC_GUID: "AL_FIRM_INTEREST"}), firm_pan=FIRM_PAN)
    al = moved.schedule_al
    assert al.buckets["AL_SECURITIES"] == pytest.approx(0.0)
    assert al.firm_interest == pytest.approx(sec)
    assert al.total_assets == pytest.approx(base.schedule_al.total_assets)      # still an asset
    assert al.firm_pan == FIRM_PAN


def test_securities_unchanged_when_nothing_is_tagged_firm(tmp_path):
    m, _ = _build(_write_mapping(tmp_path))
    assert m.schedule_al.firm_interest == 0.0
    assert not m.schedule_al.firm_pan_missing


# ---------------------------------------------------------------------------
# NEGATIVE: AL_FIRM_INTEREST on an income / expense account is refused
# ---------------------------------------------------------------------------

def test_firm_interest_on_an_income_account_is_refused(tmp_path):
    mp = _write_mapping(tmp_path)
    out = tmp_path / "out.yaml"
    applied, invalid = amc.apply_corrections_map(
        str(mp), {INCOME_GUID: "AL_FIRM_INTEREST"}, str(out),
        row_kinds={INCOME_GUID: tag_vocab.KIND_INCOME_EXPENSE})
    assert applied == 0 and [g for _p, g, _t in invalid] == [INCOME_GUID]
    assert configs.load_mapping(out).entries[INCOME_GUID].tag != "AL_FIRM_INTEREST"
    assert "AL_FIRM_INTEREST" not in tag_vocab.allowed_tags_for_kind(tag_vocab.KIND_INCOME_EXPENSE)
    assert "AL_FIRM_INTEREST" not in tag_vocab.allowed_tags_for_kind(tag_vocab.KIND_LIABILITY)


def test_firm_interest_is_accepted_on_an_asset_account(tmp_path):
    mp = _write_mapping(tmp_path)
    out = tmp_path / "out.yaml"
    applied, invalid = amc.apply_corrections_map(
        str(mp), {CASH_GUID: "AL_FIRM_INTEREST"}, str(out), row_kinds={CASH_GUID: tag_vocab.KIND_ASSET})
    assert applied == 1 and not invalid


# ---------------------------------------------------------------------------
# The firm PAN comes from the entity; a missing one is called out loudly
# ---------------------------------------------------------------------------

def _sheet_text(tmp_path, built):
    model, (tree, result, loaded, rules, entity, form16) = built
    out = tmp_path / "wb.xlsx"
    ww.write_workbook(
        str(out), tree, model, rules, rules_engine.load_user_rules(RULES_DIR / "user_rules.yaml"),
        entity, REGIME, YEAR_KEY, form16.opted_out_115bac, [], [], [], result.unmapped, "v1",
        "2026-01-01T00:00:00", {}, result.resolved, loaded.entries,
    )
    ws = openpyxl.load_workbook(str(out))["ScheduleAL"]
    return "\n".join(str(c.value) for row in ws.iter_rows() for c in row if c.value is not None)


def test_schedule_al_sheet_shows_firm_line_and_pan(tmp_path):
    built = _build(_write_mapping(tmp_path, edits={SEC_GUID: "AL_FIRM_INTEREST"}), firm_pan=FIRM_PAN)
    text = _sheet_text(tmp_path, built)
    assert "Interest in firm / AOP" in text and FIRM_PAN in text and "WARNING" not in text


def test_missing_firm_pan_is_a_warning_not_a_guess(tmp_path):
    built = _build(_write_mapping(tmp_path, edits={SEC_GUID: "AL_FIRM_INTEREST"}), firm_pan="")
    assert built[0].schedule_al.firm_pan_missing
    text = _sheet_text(tmp_path, built)
    assert "NOT SET" in text and "WARNING" in text


def test_no_firm_line_when_no_firm_interest(tmp_path):
    text = _sheet_text(tmp_path, _build(_write_mapping(tmp_path)))
    assert "Interest in firm" not in text


def test_entity_firm_pan_round_trips(tmp_path):
    ents = {"SYN-IND": {"name": "Synthetic Individual", "pan": "AAAAA0000A", "status": "Individual",
                        "partner_firm_pan": FIRM_PAN.lower()}}
    f = tmp_path / "entities.yaml"
    f.write_text(yaml.safe_dump(ents), encoding="utf-8")
    e = configs.load_entities(f)["SYN-IND"]
    assert e.partner_firm_pan == FIRM_PAN
    assert configs._entity_to_dict(e)["partner_firm_pan"] == FIRM_PAN


# ---------------------------------------------------------------------------
# AL_FOREIGN: set and clear
# ---------------------------------------------------------------------------

def test_clearing_the_foreign_flag_removes_the_row_from_schedule_fa(tmp_path):
    mp = _write_mapping(tmp_path, flags={SEC_GUID: ["AL_FOREIGN"]})
    with_flag, _ = _build(mp)
    assert [r.path for r in with_flag.schedule_fa.rows] == ["Assets/Investments/SynthCorp Shares"]

    out = tmp_path / "cleared.yaml"
    applied, invalid = amc.apply_corrections_map(str(mp), {}, str(out), flag_changes={SEC_GUID: {"AL_FOREIGN": False}})
    assert applied == 1 and not invalid
    assert "AL_FOREIGN" not in configs.load_mapping(out).entries[SEC_GUID].flags
    cleared, _ = _build(out)
    assert cleared.schedule_fa.rows == []


def test_setting_the_foreign_flag_adds_the_row(tmp_path):
    mp = _write_mapping(tmp_path)
    out = tmp_path / "set.yaml"
    amc.apply_corrections_map(str(mp), {}, str(out), flag_changes={SEC_GUID: {"AL_FOREIGN": True}})
    m, _ = _build(out)
    assert len(m.schedule_fa.rows) == 1


def test_a_tag_change_keeps_an_existing_foreign_flag(tmp_path):
    mp = _write_mapping(tmp_path, flags={SEC_GUID: ["AL_FOREIGN"]})
    out = tmp_path / "retag.yaml"
    amc.apply_corrections_map(str(mp), {SEC_GUID: "AL_FIRM_INTEREST"}, str(out),
                              row_kinds={SEC_GUID: tag_vocab.KIND_ASSET})
    e = configs.load_mapping(out).entries[SEC_GUID]
    assert e.tag == "AL_FIRM_INTEREST" and e.flags == ["AL_FOREIGN"]


def test_a_flag_change_never_touches_the_tag_or_the_note(tmp_path):
    mp = _write_mapping(tmp_path)
    before = configs.load_mapping(mp).entries[SEC_GUID]
    out = tmp_path / "flag.yaml"
    amc.apply_corrections_map(str(mp), {}, str(out), flag_changes={SEC_GUID: {"AL_FOREIGN": True}})
    after = configs.load_mapping(out).entries[SEC_GUID]
    assert (after.tag, after.note, after.suggested_by_llm) == (before.tag, before.note, before.suggested_by_llm)


def test_foreign_flag_is_refused_on_income_liability_and_unknown_rows(tmp_path):
    mp = _write_mapping(tmp_path)
    out = tmp_path / "bad.yaml"
    applied, invalid = amc.apply_corrections_map(
        str(mp), {}, str(out),
        flag_changes={INCOME_GUID: {"AL_FOREIGN": True}, "no-such-guid": {"AL_FOREIGN": True},
                      SEC_GUID: {"NOT_A_FLAG": True}})
    assert applied == 0 and len(invalid) == 3
    assert all(not e.flags for e in configs.load_mapping(out).entries.values())
    assert not tag_vocab.is_al_foreign_capable("AL_LIABILITY") and not tag_vocab.is_al_foreign_capable("PERSONAL")
    assert not tag_vocab.is_al_foreign_capable(tag_vocab.HOLD)


# ---------------------------------------------------------------------------
# Review Mapping UI: the control, and the save path
# ---------------------------------------------------------------------------

def _data_root(tmp_path, flags=None):
    root = tmp_path / "Data"
    (root / "itr" / "mappings").mkdir(parents=True)
    (root / "itr" / "entities.yaml").write_text(yaml.safe_dump({"SYN-IND": {
        "name": "Synthetic Individual", "pan": "AAAAA0000A", "status": "Individual",
        "default_regime": "new", "workbook_match": "SYN-IND"}}), encoding="utf-8")
    mp = _write_mapping(tmp_path, flags=flags)
    shutil.copy(mp, root / "itr" / "mappings" / "SYN-IND.mapping.yaml")
    return root, root / "itr" / "mappings" / "SYN-IND.mapping.yaml"


def _payload(changes):
    return json.dumps({"context": {"entity_key": "SYN-IND"}, "all_rows": [], "changes": [
        {"_idx": i, "path": "", **c} for i, c in enumerate(changes)]})


def test_ui_flag_only_save_sets_the_flag_and_does_not_reapprove_the_tag(tmp_path):
    root, mp = _data_root(tmp_path)
    before = configs.load_mapping(mp).entries[SEC_GUID]
    with patch("ui._config.data_root_dir", return_value=root):
        msg = ui_mod._save_changes(_payload([
            {"guid": SEC_GUID, "tag": "AL_SECURITIES", "_orig": "AL_SECURITIES", "al_foreign": "AL_FOREIGN"}]))
    assert "Saved" in msg
    after = configs.load_mapping(mp).entries[SEC_GUID]
    assert after.flags == ["AL_FOREIGN"] and after.tag == before.tag and after.note == before.note


def test_ui_clearing_the_flag_removes_it(tmp_path):
    root, mp = _data_root(tmp_path, flags={SEC_GUID: ["AL_FOREIGN"]})
    with patch("ui._config.data_root_dir", return_value=root):
        ui_mod._save_changes(_payload([
            {"guid": SEC_GUID, "tag": "AL_SECURITIES", "_orig": "AL_SECURITIES", "al_foreign": ""}]))
    assert not configs.load_mapping(mp).entries[SEC_GUID].flags


def test_ui_tag_change_with_the_flag_untouched_keeps_the_flag(tmp_path):
    root, mp = _data_root(tmp_path, flags={SEC_GUID: ["AL_FOREIGN"]})
    with patch("ui._config.data_root_dir", return_value=root):
        ui_mod._save_changes(_payload([
            {"guid": SEC_GUID, "tag": "AL_FIRM_INTEREST", "_orig": "AL_SECURITIES", "al_foreign": "AL_FOREIGN",
             "kind": tag_vocab.KIND_ASSET}]))
    e = configs.load_mapping(mp).entries[SEC_GUID]
    assert e.tag == "AL_FIRM_INTEREST" and e.flags == ["AL_FOREIGN"]


def test_ui_refuses_the_flag_on_an_income_row_and_says_so(tmp_path):
    root, mp = _data_root(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        msg = ui_mod._save_changes(_payload([
            {"guid": INCOME_GUID, "tag": "OS_INTEREST_BANK", "_orig": "OS_INTEREST_BANK", "al_foreign": "AL_FOREIGN"}]))
    assert "Schedule AL asset" in msg
    assert not configs.load_mapping(mp).entries[INCOME_GUID].flags


def test_ui_rows_carry_the_flag_and_the_toggle_is_wired(tmp_path):
    root, _mp = _data_root(tmp_path, flags={SEC_GUID: ["AL_FOREIGN"]})
    with patch("ui._config.data_root_dir", return_value=root):
        rows, _info = ui_mod._collect_review("SYN-IND")
    by = {r["guid"]: r for r in rows}
    assert by[SEC_GUID]["al_foreign"] == "AL_FOREIGN" and by[CASH_GUID]["al_foreign"] == ""
    for r in rows:
        ui_mod._row_presentation(r, {})
    assert by[SEC_GUID]["_flagok"] and by[CASH_GUID]["_flagok"]
    assert not by[INCOME_GUID]["_flagok"]                  # an income row cannot take the flag
    spec = ui_mod._spec([], "SYN-IND", "")
    assert spec.flag_toggle and spec.flag_toggle[0] == "al_foreign"
    from ui import _review_engine as eng
    html = eng.build_html(spec, [])
    assert "Mark foreign" in html and "Clear foreign" in html


def test_other_engine_screens_get_no_flag_buttons():
    from ui import _review_engine as eng
    spec = eng.ReviewSpec(app_id="other", columns=[eng.Column("a", "A")], target_col="a", payload_var="v")
    assert spec.flag_toggle == ()
    html = eng.build_html(spec, [])
    assert "flag-on" not in html and "Mark foreign" not in html
