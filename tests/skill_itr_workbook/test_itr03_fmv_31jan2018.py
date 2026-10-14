"""
tests/skill_itr_workbook/test_itr03_fmv_31jan2018.py -- ITR-03: the 31-Jan-2018
FMV lookup. A GnuCash commodity id carries the exchange suffix ("ABC.NS") while
the bundled table is keyed by the bare symbol; stripping the suffix alone would
hand a post-split lot the PRE-split price and double the grandfathered cost, so
a scrip with a corporate action after 31-Jan-2018 never gets a table value
silently. Synthetic symbols only.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
RULES_DIR = ROOT / "bundling" / "canonical" / "itr" / "rules"
for p in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import configs  # noqa: E402
import lots as lots_engine  # noqa: E402
import rules as rules_engine  # noqa: E402
import schedules as sch  # noqa: E402

SPLIT = {"ex_date": date(2020, 9, 1), "ratio": 5.0, "old_isin": "INE000A01010", "new_isin": "INE000A01028", "note": ""}


def _tables(nse=None, actions=None, mf=None):
    return sch.FmvTables(nse=nse or {}, mf=mf or {}, actions=actions or {})


def _ref(sym, fmv=None, table_ref=None):
    return {sym: configs.ScripRef(symbol=sym, fmv_31jan2018=fmv, table_ref=table_ref)}


# --- (a) suffix handling --------------------------------------------------------

def test_ns_symbol_present_in_table_resolves_bare():
    t = _tables(nse={"ABC": {"isin": "INE1", "fmv_31jan2018": 120.0}})
    assert sch.resolve_fmv("ABC.NS", {}, t) == 120.0
    assert sch.resolve_fmv_with_source("ABC.NS", {}, t) == (120.0, sch.FMV_SOURCE_TABLE)


def test_symbol_in_neither_table_still_fails_loud_and_is_not_fuzzy_matched():
    t = _tables(nse={"ABCD": {"isin": "INE1", "fmv_31jan2018": 1.0}, "AB": {"isin": "INE2", "fmv_31jan2018": 2.0}})
    with pytest.raises(sch.FmvNotFoundError):
        sch.resolve_fmv("ABC.NS", {}, t)


def test_bse_only_share_still_fails_loud():
    t = _tables(nse={"ABC": {"isin": "INE1", "fmv_31jan2018": 120.0}})
    with pytest.raises(sch.FmvNotFoundError):
        sch.resolve_fmv("ABC.BO", {}, t)


def test_override_keyed_with_suffix_still_wins():
    t = _tables(nse={"XYZ": {"isin": "INE1", "fmv_31jan2018": 10.0}})
    assert sch.resolve_fmv_with_source("XYZ.NS", _ref("XYZ.NS", 77.0), t) == (77.0, sch.FMV_SOURCE_OVERRIDE)


def test_override_keyed_bare_also_wins_for_the_suffixed_symbol():
    t = _tables(nse={"XYZ": {"isin": "INE1", "fmv_31jan2018": 10.0}})
    assert sch.resolve_fmv_with_source("XYZ.NS", _ref("XYZ", 77.0), t) == (77.0, sch.FMV_SOURCE_OVERRIDE)


# --- (b) corporate actions ------------------------------------------------------

def test_post_2018_split_without_override_fails_loud_and_never_returns_the_table_value():
    t = _tables(nse={"SPL": {"isin": "INE000A01010", "fmv_31jan2018": 500.0}}, actions={"SPL": [SPLIT]})
    with pytest.raises(sch.FmvCorporateActionError) as e:
        sch.resolve_fmv("SPL.NS", {}, t)
    assert "scrips.yaml" in str(e.value) and "500" not in str(e.value)
    with pytest.raises(sch.FmvNotFoundError):          # it is a FmvNotFoundError: callers already handle it
        sch.resolve_fmv("SPL", {}, t)


def test_corporate_action_matched_by_old_isin_even_when_symbol_changed():
    t = _tables(nse={"OLDNAME": {"isin": "INE000A01010", "fmv_31jan2018": 500.0}}, actions={"NEWNAME": [SPLIT]})
    with pytest.raises(sch.FmvCorporateActionError):
        sch.resolve_fmv("OLDNAME.NS", {}, t)


def test_explicit_override_is_used_as_is_never_adjusted_again():
    t = _tables(nse={"SPL": {"isin": "INE000A01010", "fmv_31jan2018": 500.0}}, actions={"SPL": [SPLIT]})
    assert sch.resolve_fmv_with_source("SPL.NS", _ref("SPL.NS", 100.0), t) == (100.0, sch.FMV_SOURCE_OVERRIDE)
    assert sch.resolve_fmv("SPL.NS", _ref("SPL", 100.0), t) == 100.0       # not 100/5, not 100*5


def test_table_ref_alias_to_a_split_scrip_also_fails_loud():
    t = _tables(nse={"SPL": {"isin": "INE000A01010", "fmv_31jan2018": 500.0}}, actions={"SPL": [SPLIT]})
    with pytest.raises(sch.FmvCorporateActionError):
        sch.resolve_fmv("ALIAS.NS", _ref("ALIAS.NS", None, "bundled-fmv-table:SPL"), t)


def test_scrip_without_corporate_action_is_unaffected_by_other_scrips_actions():
    t = _tables(nse={"ABC": {"isin": "INE1", "fmv_31jan2018": 120.0}}, actions={"SPL": [SPLIT]})
    assert sch.resolve_fmv("ABC.NS", {}, t) == 120.0


# --- the corporate-actions file -------------------------------------------------

def test_bundled_file_has_the_header_and_loads_cleanly():
    path = SCRIPTS.parent / "data" / "corporate_actions_after_31jan2018.csv"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "symbol,ex_date,ratio,old_isin,new_isin,note" in text
    assert "add the scrips you actually hold" not in text
    assert sch.load_corporate_actions(path)        # ships WITH rows (see test_itr03_corporate_actions_list.py)
    assert sch.load_fmv_tables().actions


def test_loader_reads_rows(tmp_path):
    f = tmp_path / "ca.csv"
    f.write_text("# c\nsymbol,ex_date,ratio,old_isin,new_isin,note\nSPL,2020-09-01,5,INE0,INE1,split\n", encoding="utf-8")
    got = sch.load_corporate_actions(f)
    assert got["SPL"][0]["ratio"] == 5.0 and got["SPL"][0]["ex_date"] == date(2020, 9, 1)


@pytest.mark.parametrize("row", [
    "SPL,2017-12-01,5,a,b,x",          # ex-date not after 31-Jan-2018
    "SPL,2020-09-01,0,a,b,x",          # ratio not positive
    "SPL,notadate,5,a,b,x",
    ",2020-09-01,5,a,b,x",
])
def test_loader_rejects_malformed_rows_rather_than_ignoring_them(tmp_path, row):
    f = tmp_path / "ca.csv"
    f.write_text("symbol,ex_date,ratio,old_isin,new_isin,note\n" + row + "\n", encoding="utf-8")
    with pytest.raises(sch.CorporateActionsFileError):
        sch.load_corporate_actions(f)


def test_loader_rejects_a_wrong_header(tmp_path):
    f = tmp_path / "ca.csv"
    f.write_text("symbol,date\nSPL,2020-09-01\n", encoding="utf-8")
    with pytest.raises(sch.CorporateActionsFileError):
        sch.load_corporate_actions(f)


# --- (c) per-lot FMV source in the CG schedule -------------------------------------

class _Lot:
    def __init__(self, scrip="X"):
        self.sale_date, self.buy_date, self.qty = date(2024, 8, 1), date(2016, 1, 1), 100
        self.cost, self.proceeds, self.gain = 10000.0, 90000.0, 80000.0
        self.scrip, self.account_guid, self.attribution = scrip, "acct1", "exact"


def _run_cg(commodity, scrips, tables, monkeypatch):
    rules = rules_engine.load_rules(RULES_DIR, "2024-25")
    monkeypatch.setattr(lots_engine, "reconstruct_lots", lambda book, yk: ["recon"])
    monkeypatch.setattr(lots_engine, "all_lots", lambda recons: [_Lot(commodity)])

    class _Acct:
        commodity_id = commodity

    class _Book:
        accounts = {"acct1": _Acct()}

    return sch.build_capital_gains({}, {}, _Book(), "2024-25", rules, scrips, tables)


def test_cg_lot_carries_the_fmv_source(monkeypatch):
    t = _tables(nse={"ABC": {"isin": "INE1", "fmv_31jan2018": 120.0}})
    cg = _run_cg("ABC.NS", {}, t, monkeypatch)
    assert cg.lot_rows[0].fmv_used == 120.0 and cg.lot_rows[0].fmv_source == sch.FMV_SOURCE_TABLE
    cg = _run_cg("ABC.NS", _ref("ABC", 130.0), t, monkeypatch)
    assert cg.lot_rows[0].fmv_used == 130.0 and cg.lot_rows[0].fmv_source == sch.FMV_SOURCE_OVERRIDE


def test_cg_split_scrip_without_override_is_unresolved_and_uses_no_table_value(monkeypatch):
    t = _tables(nse={"SPL": {"isin": "INE000A01010", "fmv_31jan2018": 500.0}}, actions={"SPL": [SPLIT]})
    cg = _run_cg("SPL.NS", {}, t, monkeypatch)
    row = cg.lot_rows[0]
    assert cg.unresolved_scrips == ["SPL.NS"] and cg.fmv_failures
    assert row.fmv_used is None and row.fmv_source == ""
    assert row.taxable_gain == row.booked_gain          # nothing grandfathered silently


def test_workbook_lot_table_has_an_fmv_source_column():
    import write_workbook as ww
    src = Path(ww.__file__).read_text(encoding="utf-8")
    assert '"Attribution", "FMV source"' in src
