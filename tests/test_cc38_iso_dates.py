"""tests/test_cc38_iso_dates.py -- CC-38: Book Spends writes its two CSV files with yyyy-mm-dd dates,
the same as the bank skills, and every reader still reads an older dd/mm/yyyy file correctly.
Synthetic data only."""
from __future__ import annotations

import csv
import re
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src"), str(ROOT / "ui")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_cc_spend_booking import agent as ccsb                      # noqa: E402
from tests.test_cc_spend_booking import basic, fake_mapper_factory           # noqa: E402

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DMY = re.compile(r"^\d\d/\d\d/\d{4}$")


def _j(d, num="n1", side="W"):
    return SimpleNamespace(date=d, num=num, raw_description="RAW", description="DESC", account="Expenses:Food",
                           transfer="", amount=12.5, side=side, confidence="high", match_reason="r")


def _dates(path):
    with open(path, encoding="utf-8") as f:
        return [r["Date"] for r in csv.DictReader(f)]


# --- writers ---------------------------------------------------------------

def test_both_files_are_written_iso_and_day_month_never_swap(tmp_path):
    rows = [_j(date(2025, 4, 3), "a"), _j(date(2025, 3, 4), "b")]       # 03/04 and 04/03
    ccsb._canonical_csv(rows, tmp_path / "c.csv")
    ccsb._write_import_csv(rows, tmp_path / "i.csv")
    assert _dates(tmp_path / "c.csv") == ["2025-04-03", "2025-03-04"]
    assert sorted(_dates(tmp_path / "i.csv")) == ["2025-03-04", "2025-04-03"]
    for p in ("c.csv", "i.csv"):
        for d in _dates(tmp_path / p):
            assert ISO.match(d) and not DMY.match(d)                    # NEGATIVE: no dd/mm/yyyy written


def test_a_full_run_writes_no_dd_mm_yyyy_in_either_file(tmp_path, monkeypatch):
    seen = {}
    inner = fake_mapper_factory([])

    def spy(gnucash, cin, cout, config, drawings, default, model=None):
        seen["canonical"] = _dates(cin)
        return inner(gnucash, cin, cout, config, drawings, default, model)
    reply, rows, calls, out = basic(tmp_path, monkeypatch, mapper=spy)
    assert seen["canonical"] and rows
    assert all(ISO.match(d) for d in seen["canonical"] + [r["Date"] for r in rows])
    assert not any(DMY.match(d) for d in seen["canonical"] + [r["Date"] for r in rows])      # NEGATIVE
    assert "2025-04-05" in seen["canonical"]                          # 5 April, not 4 May
    assert "2025-05-04" not in seen["canonical"]


# --- readers: ISO and the older dd/mm/yyyy ----------------------------------

CASES = [("2025-04-03", date(2025, 4, 3)), ("2025-03-04", date(2025, 3, 4)),
         ("03/04/2025", date(2025, 4, 3)), ("04/03/2025", date(2025, 3, 4))]


@pytest.mark.parametrize("text,want", CASES)
def test_every_reader_gets_the_right_day_and_month(text, want):
    from agents.bank_balance_gate import _iso
    from agents.recency import parse_date
    from agents.skill_gnucash_account_mapper.agent import _parse_any_date, _row_fy_start_year
    from agents.skill_gnucash_pipeline.agent import _iso_date
    from agents.skill_gnucash_reconciler.agent import _parse_date
    assert _iso(text) == want.isoformat()
    assert parse_date(text) == want
    assert _parse_any_date(text) == want
    assert _parse_date(text).date() == want
    assert _iso_date(text) == want.isoformat()
    fy = want.year if want.month >= 4 else want.year - 1
    assert _row_fy_start_year(text) == fy


@pytest.mark.parametrize("text", ["2025-04-03", "03/04/2025"])
def test_a_swapped_reading_is_never_produced(text):                      # NEGATIVE
    from agents.bank_balance_gate import _iso
    assert _iso(text) != "2025-03-04"


def test_review_tab_shows_an_iso_and_an_older_file_with_the_right_dates(tmp_path):
    from ui.tabs import gnucash_review as gr
    hdr = "Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n"
    iso = tmp_path / "2026-01-01-ISO-GnuCash_import_ready.csv"
    iso.write_text(hdr + "2025-04-03,COFFEE,Expense:Food,,10.00,,high,r\n2025-03-04,TEA,Expense:Food,,5.00,,high,r\n",
                   encoding="utf-8")
    old = tmp_path / "2026-01-01-OLD-GnuCash_import_ready.csv"
    old.write_text(hdr + "03/04/2025,COFFEE,Expense:Food,,10.00,,high,r\n04/03/2025,TEA,Expense:Food,,5.00,,high,r\n",
                   encoding="utf-8")
    h_iso = gr._load_review_data(str(iso), str(iso))
    h_old = gr._load_review_data(str(old), str(old))
    assert "2025-04-03" in h_iso and "2025-03-04" in h_iso
    assert "03/04/2025" in h_old and "04/03/2025" in h_old
    assert "2025-04-03" not in h_old                                    # NEGATIVE: the old file is not rewritten


def test_bank_skill_files_are_read_exactly_as_before():
    from agents.bank_balance_gate import statement_points
    pts = statement_points([{"Date": "2025-04-01", "Deposit": "5.00", "Withdrawal": "", "Description": "x"},
                            {"Date": "01/04/2025", "Deposit": "", "Withdrawal": "7.00", "Description": "y"}])
    assert [p["date"] for p in pts] == ["2025-04-01", "2025-04-01"]
    assert [p["date_raw"] for p in pts] == ["2025-04-01", "01/04/2025"]      # raw text kept untouched
