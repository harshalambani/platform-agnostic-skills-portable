"""
ITR-16: Review Mapping can mark a dividend income account as FOREIGN SOURCE
("Mark foreign income" / "Clear foreign income"), so the ITR-06 double-count stop
can fire from the UI. The mark is offered on, and accepted for, dividend income
tags only. Synthetic data only.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_itr05_schedule_al_firm_foreign as t5  # noqa: E402
import test_itr06_foreign_tax_credit as t6  # noqa: E402
from test_itr05_schedule_al_firm_foreign import (  # noqa: E402
    CASH_GUID, FIXTURES, INCOME_GUID, SEC_GUID, amc, configs, tag_vocab, ui_mod,
)

TDS_GUID = t6.TDS_INT_GUID          # Expense/TDS on Interest


def _root(tmp_path, edits=None, flags=None):
    root = tmp_path / "Data"
    (root / "itr" / "mappings").mkdir(parents=True)
    (root / "itr" / "entities.yaml").write_text(yaml.safe_dump({"SYN-IND": {
        "name": "Synthetic Individual", "pan": "AAAAA0000A", "status": "Individual",
        "default_regime": "new", "workbook_match": "SYN-IND"}}), encoding="utf-8")
    mp = t5._write_mapping(tmp_path, edits=edits, flags=flags)
    dest = root / "itr" / "mappings" / "SYN-IND.mapping.yaml"
    shutil.copy(mp, dest)
    return root, dest


def _save(root, changes):
    payload = json.dumps({"context": {"entity_key": "SYN-IND"}, "all_rows": [], "changes": [
        {"_idx": i, "path": "", **c} for i, c in enumerate(changes)]})
    with patch("ui._config.data_root_dir", return_value=root):
        return ui_mod._save_changes(payload)


def _mark(guid, tag, on=True):
    return {"guid": guid, "tag": tag, "_orig": tag, "foreign_income": "FOREIGN_INCOME" if on else ""}


DIV = {INCOME_GUID: "OS_DIVIDEND"}


# --- the vocabulary -----------------------------------------------------------

def test_only_dividend_income_can_be_foreign_income():
    assert tag_vocab.is_foreign_income_capable("OS_DIVIDEND")
    for t in ("OS_INTEREST_BANK", "OS_INTEREST_SB", "OS_INTEREST_NBFC", "SALARY_GROSS", "BUS_EXPENSE",
              "AL_SECURITIES", "AL_CASH_BANK", "AL_LIABILITY", "PERSONAL", tag_vocab.HOLD, "TAXPAID_TDS_SALARY"):
        assert not tag_vocab.is_foreign_income_capable(t), t
    assert tag_vocab.FOREIGN_INCOME_TAGS <= tag_vocab.RE_TAGS        # income side only
    assert not (tag_vocab.FOREIGN_INCOME_TAGS & tag_vocab.AL_ASSET_TAGS)


# --- the toggle is offered on dividend rows only ------------------------------------

def test_toggle_is_offered_only_on_a_mapped_dividend_row(tmp_path):
    root, _ = _root(tmp_path, edits={**DIV, CASH_GUID: "PERSONAL", TDS_GUID: tag_vocab.HOLD})
    with patch("ui._config.data_root_dir", return_value=root):
        rows, _i = ui_mod._collect_review("SYN-IND")
    for r in rows:
        ui_mod._row_presentation(r, {})
    by = {r["guid"]: r for r in rows}
    assert by[INCOME_GUID]["_flagok_fi"] is True
    assert not by[SEC_GUID]["_flagok_fi"]                  # asset
    assert not by[CASH_GUID]["_flagok_fi"]                 # PERSONAL
    assert not by[TDS_GUID]["_flagok_fi"]                  # HOLD
    assert [r["guid"] for r in rows if r["_flagok_fi"]] == [INCOME_GUID]


def test_toggle_is_not_offered_when_there_is_no_dividend_row(tmp_path):                  # NEGATIVE
    root, _ = _root(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        rows, _i = ui_mod._collect_review("SYN-IND")
    for r in rows:
        ui_mod._row_presentation(r, {})
    assert not any(r["_flagok_fi"] for r in rows)


def test_the_buttons_and_column_are_wired_and_the_al_buttons_stay():
    spec = ui_mod._spec([], "SYN-IND", "")
    keys = [c.key for c in spec.columns]
    assert "foreign_income" in keys and "al_foreign" in keys
    from ui import _review_engine as eng
    html = eng.build_html(spec, [])
    assert "Mark foreign income" in html and "Clear foreign income" in html
    assert "Mark foreign<" in html and "Clear foreign<" in html
    assert "-flag-on" in html and "-flag2-on" in html
    assert "r._flagok_fi" in html and "r._flagok)" in html


def test_other_screens_get_no_second_toggle():                                            # NEGATIVE
    from ui import _review_engine as eng
    spec = eng.ReviewSpec(app_id="other", columns=[eng.Column("a", "A")], target_col="a", payload_var="v")
    assert spec.extra_flag_toggles == ()
    html = eng.build_html(spec, [])
    assert "flag2-on" not in html and "foreign income" not in html


# --- saving, reloading, validation ---------------------------------------------------

def test_mark_is_saved_and_reloaded_in_its_own_column(tmp_path):
    root, mp = _root(tmp_path, edits=DIV)
    before = configs.load_mapping(mp).entries[INCOME_GUID]
    msg = _save(root, [_mark(INCOME_GUID, "OS_DIVIDEND")])
    assert "Saved" in msg
    after = configs.load_mapping(mp).entries[INCOME_GUID]
    assert after.flags == ["FOREIGN_INCOME"] and after.tag == before.tag and after.note == before.note
    with patch("ui._config.data_root_dir", return_value=root):
        rows, _i = ui_mod._collect_review("SYN-IND")
    row = {r["guid"]: r for r in rows}[INCOME_GUID]
    assert row["foreign_income"] == "FOREIGN_INCOME" and row["al_foreign"] == ""
    _save(root, [_mark(INCOME_GUID, "OS_DIVIDEND", on=False)])
    assert not configs.load_mapping(mp).entries[INCOME_GUID].flags


@pytest.mark.parametrize("guid,tag", [
    (SEC_GUID, "AL_SECURITIES"), (CASH_GUID, "AL_CASH_BANK"), (TDS_GUID, "TAXPAID_TDS_INTEREST"),
    (INCOME_GUID, "OS_INTEREST_BANK"),
])
def test_ui_refuses_the_mark_on_asset_expense_and_interest_rows(tmp_path, guid, tag):    # NEGATIVE
    root, mp = _root(tmp_path, edits={guid: tag})
    msg = _save(root, [_mark(guid, tag)])
    assert "dividend income" in msg
    assert not configs.load_mapping(mp).entries[guid].flags


@pytest.mark.parametrize("tag", ["PERSONAL", tag_vocab.HOLD])
def test_ui_refuses_the_mark_on_personal_and_hold_rows(tmp_path, tag):                   # NEGATIVE
    root, mp = _root(tmp_path, edits={INCOME_GUID: tag})
    msg = _save(root, [_mark(INCOME_GUID, tag)])
    assert "dividend income" in msg
    assert not configs.load_mapping(mp).entries[INCOME_GUID].flags


def test_apply_refuses_foreign_income_on_non_income_tags_but_allows_clearing(tmp_path):  # NEGATIVE
    mp = t5._write_mapping(tmp_path, edits={**DIV}, flags={SEC_GUID: ["FOREIGN_INCOME"]})
    out = tmp_path / "o.yaml"
    applied, invalid = amc.apply_corrections_map(
        str(mp), {}, str(out),
        flag_changes={CASH_GUID: {"FOREIGN_INCOME": True}, TDS_GUID: {"FOREIGN_INCOME": True},
                      SEC_GUID: {"FOREIGN_INCOME": True}})
    assert applied == 0 and len(invalid) == 3
    # a stale mark on a row that is no longer a dividend row can still be cleared
    applied, invalid = amc.apply_corrections_map(
        str(mp), {}, str(out), flag_changes={SEC_GUID: {"FOREIGN_INCOME": False}})
    assert applied == 1 and not invalid and not configs.load_mapping(out).entries[SEC_GUID].flags


def test_al_foreign_is_still_refused_on_an_income_row(tmp_path):                          # NEGATIVE
    mp = t5._write_mapping(tmp_path, edits=DIV)
    out = tmp_path / "o.yaml"
    applied, invalid = amc.apply_corrections_map(
        str(mp), {}, str(out), flag_changes={INCOME_GUID: {"AL_FOREIGN": True}})
    assert applied == 0 and len(invalid) == 1
    assert not configs.load_mapping(out).entries[INCOME_GUID].flags


def test_both_marks_in_one_save_do_not_overwrite_each_other(tmp_path):
    root, mp = _root(tmp_path, edits=DIV)
    _save(root, [{"guid": INCOME_GUID, "tag": "OS_DIVIDEND", "_orig": "OS_DIVIDEND",
                  "foreign_income": "FOREIGN_INCOME"},
                 {"guid": SEC_GUID, "tag": "AL_SECURITIES", "_orig": "AL_SECURITIES", "al_foreign": "AL_FOREIGN"}])
    e = configs.load_mapping(mp).entries
    assert e[INCOME_GUID].flags == ["FOREIGN_INCOME"] and e[SEC_GUID].flags == ["AL_FOREIGN"]


# --- the double-count stop now reachable from the UI -----------------------------------

def _model_from_saved(mp, **kw):
    import fixture_gen
    import mapping as me
    import parse_eguile as pe
    import parse_form16 as pf16
    import parse_gnucash as pg
    import rules as re_
    import schedules as sch
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    book = pg.parse_book(FIXTURES / "syn_ind.gnucash")
    result = me.resolve_tree(tree, configs.load_mapping(mp))
    form16 = pf16.parse_form16(FIXTURES / "syn_ind_form16.pdf")
    rules = re_.load_rules(t6.RULES_DIR, t6.YEAR_KEY)
    ents = t6.ROOT / "bundling" / "canonical" / "itr"
    entity = configs.load_entities(ents / "entities.example.yaml")["SYN-IND"]
    scrips = configs.load_scrips(ents / "scrips.example.yaml")
    return sch.build_all_schedules(tree, result.resolved, book, form16, t6.YEAR_KEY, rules, t6.REGIME,
                                   entity.status, entity.dob, scrips, sch.load_fmv_tables(), **kw)


def test_a_ui_marked_account_plus_a_report_not_in_book_stops_the_run(tmp_path):
    root, mp = _root(tmp_path, edits=DIV)
    _save(root, [_mark(INCOME_GUID, "OS_DIVIDEND")])
    with pytest.raises(t6.ft.ForeignDoubleCountError):
        _model_from_saved(mp, foreign_report=t6._report(ordinary_dividend=700.0), foreign_dividends_in_book=False)


def test_with_the_in_book_setting_it_does_not_stop_and_report_dividends_are_not_added(tmp_path):  # NEGATIVE
    root, mp = _root(tmp_path, edits=DIV)
    _save(root, [_mark(INCOME_GUID, "OS_DIVIDEND")])
    base = _model_from_saved(mp, foreign_dividends_in_book=True)
    m = _model_from_saved(mp, foreign_report=t6._report(ordinary_dividend=700.0), foreign_dividends_in_book=True)
    assert m.other_sources.taxable_total == pytest.approx(base.other_sources.taxable_total)
    assert m.other_sources.dividend_gross == pytest.approx(base.other_sources.dividend_gross)


def test_unmarked_dividend_account_with_a_report_is_the_normal_path(tmp_path):                    # NEGATIVE
    _root_, mp = _root(tmp_path, edits=DIV)
    base = _model_from_saved(mp)
    m = _model_from_saved(mp, foreign_report=t6._report(ordinary_dividend=700.0), foreign_dividends_in_book=False)
    assert m.other_sources.taxable_total == pytest.approx(base.other_sources.taxable_total + 700.0)


def test_marked_account_without_a_report_is_unchanged(tmp_path):                                   # NEGATIVE
    root, mp = _root(tmp_path, edits=DIV)
    base = _model_from_saved(mp)
    _save(root, [_mark(INCOME_GUID, "OS_DIVIDEND")])
    m = _model_from_saved(mp)
    assert m.other_sources.taxable_total == pytest.approx(base.other_sources.taxable_total)
