"""
tests/skill_itr_workbook/test_itr03_corporate_actions_list.py -- ITR-03 fix
round: the bundled public list of NSE splits / bonus issues, and the private
overlay that is read alongside it. Synthetic rows and symbols only; the real
overlay is never created or read.
"""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
SCRIPTS = SRC / "agents" / "skill_itr_workbook" / "scripts"
AGENT_DIR = SRC / "agents" / "skill_itr_workbook"
BUNDLED = AGENT_DIR / "data" / "corporate_actions_after_31jan2018.csv"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
for p in (str(SRC), str(SCRIPTS), str(AGENT_DIR), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import configs  # noqa: E402
import schedules as sch  # noqa: E402

HDR = "symbol,ex_date,ratio,old_isin,new_isin,note\n"


def _file(tmp_path, name, body):
    f = tmp_path / name
    f.write_text(HDR + body, encoding="utf-8")
    return f


def _tables(actions, nse=None):
    return sch.FmvTables(nse=nse or {"SPL": {"isin": "INE000A01010", "fmv_31jan2018": 500.0}},
                         mf={}, actions=actions)


# --- the bundled list -------------------------------------------------------------

def _bundled_rows():
    lines = [ln for ln in BUNDLED.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    return list(csv.DictReader(lines))


def test_bundled_list_is_big_dated_after_the_reference_date_and_unique():
    rows = _bundled_rows()
    assert len(rows) > 500
    dates = [date.fromisoformat(r["ex_date"]) for r in rows]
    assert min(dates) > date(2018, 1, 31)
    assert max(dates) <= date(2026, 10, 14)
    keys = [(r["symbol"], r["ex_date"]) for r in rows]
    assert len(keys) == len(set(keys))
    assert all(float(r["ratio"]) > 1 for r in rows)          # splits and bonus only: never a reverse split
    assert all(r["note"].startswith("NSE:") for r in rows)   # every row names its source
    assert sch.load_corporate_actions(BUNDLED)                # and the real loader accepts all of it


def test_bundled_list_carries_well_known_actions_with_the_right_ratio():
    got = {(s, a["ex_date"]): a["ratio"] for s, L in sch.load_corporate_actions(BUNDLED).items() for a in L}
    assert got[("HDFCBANK", date(2019, 9, 19))] == 2.0          # split, Rs 2 -> Re 1
    assert got[("RELIANCE", date(2024, 10, 28))] == 2.0         # 1:1 bonus
    assert got[("TATASTEEL", date(2022, 7, 28))] == 10.0        # split, Rs 10 -> Re 1
    assert got[("BAJFINANCE", date(2025, 6, 16))] == 10.0       # 4:1 bonus AND 2:1 split on one date, one row


def test_bundled_file_header_and_comment_are_honest():
    text = BUNDLED.read_text(encoding="utf-8")
    assert HDR.strip() in text
    assert "add the scrips you actually hold" not in text      # the old, misleading line
    assert "corporate_actions_overlay.csv" in text and "SOURCE" in text


def test_a_listed_scrip_stops_loud_and_an_unlisted_one_keeps_its_value():
    t = sch.load_fmv_tables()
    t.nse["HDFCBANK"] = {"isin": "INE040A01034", "fmv_31jan2018": 999.0}
    t.nse["SYNUNLISTED"] = {"isin": "INE999Z01011", "fmv_31jan2018": 123.0}
    with pytest.raises(sch.FmvCorporateActionError):
        sch.resolve_fmv("HDFCBANK.NS", {}, t)
    assert sch.resolve_fmv("SYNUNLISTED.NS", {}, t) == 123.0     # no action: unadjusted value kept


def test_bse_suffix_still_stops_loud_even_with_a_listed_nse_twin():
    t = _tables({"SPL": [{"ex_date": date(2020, 9, 1), "ratio": 5.0, "old_isin": "", "new_isin": "", "note": ""}]})
    with pytest.raises(sch.FmvNotFoundError):
        sch.resolve_fmv("SPL.BO", {}, t)


# --- the overlay ------------------------------------------------------------------

def test_missing_overlay_is_fine(tmp_path):
    base = _file(tmp_path, "b.csv", "SPL,2020-09-01,5,,,x\n")
    assert sch.load_corporate_actions(base, tmp_path / "nope.csv") == sch.load_corporate_actions(base)


def test_overlay_adds_rows_the_bundled_file_lacks(tmp_path):
    base = _file(tmp_path, "b.csv", "SPL,2020-09-01,5,,,x\n")
    over = _file(tmp_path, "o.csv", "MINE,2021-03-01,2,,,mine\n")
    got = sch.load_corporate_actions(base, over)
    assert set(got) == {"SPL", "MINE"}


def test_same_action_in_both_is_applied_once_and_the_overlay_wins(tmp_path):
    base = _file(tmp_path, "b.csv", "SPL,2020-09-01,5,,,bundled\n")
    over = _file(tmp_path, "o.csv", "SPL,2020-09-01,10,,,corrected\n")
    got = sch.load_corporate_actions(base, over)
    assert len(got["SPL"]) == 1                                  # ONCE, not twice
    assert got["SPL"][0]["ratio"] == 10.0 and got["SPL"][0]["note"] == "corrected"
    same = _file(tmp_path, "o2.csv", "SPL,2020-09-01,5,,,bundled\n")
    assert len(sch.load_corporate_actions(base, same)["SPL"]) == 1


def test_a_different_ex_date_for_the_same_symbol_is_a_second_action(tmp_path):
    base = _file(tmp_path, "b.csv", "SPL,2020-09-01,5,,,x\n")
    over = _file(tmp_path, "o.csv", "SPL,2022-01-03,2,,,y\n")
    got = sch.load_corporate_actions(base, over)
    assert [a["ratio"] for a in got["SPL"]] == [5.0, 2.0]


@pytest.mark.parametrize("row,why", [
    ("SPL,2018-01-31,5,,,x", "after 31-Jan-2018"),            # ex-date ON the reference date
    ("SPL,2017-06-01,5,,,x", "after 31-Jan-2018"),            # before it
    ("SPL,2020-09-01,abc,,,x", "bad ex_date or ratio"),
    ("SPL,2020-09-01,,,,x", "bad ex_date or ratio"),
    ("SPL,2020-09-01,-2,,,x", "ratio > 0"),
    ("SPL,2020-09-01,nan,,,x", "ratio > 0"),
    ("SPL,31/12/2020,5,,,x", "bad ex_date or ratio"),
])
def test_a_malformed_overlay_row_stops_loudly_naming_the_row(tmp_path, row, why):
    base = _file(tmp_path, "b.csv", "OK,2020-09-01,5,,,x\n")
    over = _file(tmp_path, "mine.csv", "OK2,2021-01-01,2,,,fine\n" + row + "\n")
    with pytest.raises(sch.CorporateActionsFileError) as e:
        sch.load_corporate_actions(base, over)
    msg = str(e.value)
    assert "mine.csv row 3 (SPL)" in msg and why in msg


def test_ex_date_on_or_before_the_reference_date_never_reaches_the_lookup(tmp_path):
    over = _file(tmp_path, "o.csv", "SPL,2018-01-31,5,,,x\n")
    with pytest.raises(sch.CorporateActionsFileError):
        sch.load_corporate_actions(tmp_path / "none.csv", over)
    # and so the table value is never withheld for a pre-reference action
    assert sch.resolve_fmv("SPL.NS", {}, _tables({})) == 500.0


def test_duplicate_key_inside_one_file_is_refused(tmp_path):
    base = _file(tmp_path, "b.csv", "SPL,2020-09-01,5,,,x\nSPL,2020-09-01,2,,,y\n")
    with pytest.raises(sch.CorporateActionsFileError, match="listed twice"):
        sch.load_corporate_actions(base)


def test_overlay_row_makes_the_lookup_stop_loud_and_an_override_resolves_it(tmp_path):
    base = _file(tmp_path, "b.csv", "")
    over = _file(tmp_path, "o.csv", "SPL,2020-09-01,5,INE000A01010,,mine\n")
    t = _tables(sch.load_corporate_actions(base, over))
    with pytest.raises(sch.FmvCorporateActionError):
        sch.resolve_fmv("SPL.NS", {}, t)
    ref = {"SPL.NS": configs.ScripRef(symbol="SPL.NS", fmv_31jan2018=100.0, table_ref=None)}
    assert sch.resolve_fmv("SPL.NS", ref, t) == 100.0


def test_overlay_wrong_header_stops(tmp_path):
    f = tmp_path / "o.csv"
    f.write_text("symbol,date\nSPL,2020-09-01\n", encoding="utf-8")
    with pytest.raises(sch.CorporateActionsFileError):
        sch.load_corporate_actions(tmp_path / "none.csv", f)


def test_the_run_reads_the_overlay_that_sits_next_to_scrips_yaml(tmp_path, monkeypatch):
    import agent
    import fixture_gen
    seen = {}
    real = sch.load_fmv_tables

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(sch, "load_fmv_tables", spy)
    html = tmp_path / "bs.html"
    html.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    scrips = tmp_path / "itr" / "scrips.yaml"
    scrips.parent.mkdir()
    try:
        agent.run(str(html), str(tmp_path / "o.xlsx"), mapping_file=str(FIXTURES / "syn_ind.mapping.yaml"),
                  entities_path=str(ROOT / "bundling" / "canonical" / "itr" / "entities.example.yaml"),
                  entity_key="SYN-IND", scrips_path=str(scrips), config_path="no-such-config.yaml")
    except Exception:  # noqa: BLE001 - only the lookup call matters here
        pass
    assert seen.get("overlay_path") == scrips.parent / sch.CORPORATE_ACTIONS_OVERLAY_NAME
    assert not (scrips.parent / sch.CORPORATE_ACTIONS_OVERLAY_NAME).exists()    # never created by the run
