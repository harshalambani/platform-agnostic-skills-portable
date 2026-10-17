"""
tests/test_cc21_sort_older_sbm.py -- CC-21: CC Sort recognises the older SBM layout, which prints
only a month-year statement date ("Statement Date : April-2025"). Synthetic text only.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for _p in (str(SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_cc_sort import completeness as C  # noqa: E402

D = date
R = "₹"


def _older(month="April-2025", rows=None, extra=()):
    rows = rows if rows is not None else [
        f"2-Apr-2025 SAMPLE CAFE CITY XX {R} 120.00",
        f"14-Apr-2025 EXAMPLE BOOKS MUMBAI {R} 500.00",
    ]
    return "\n".join([
        "Sample Global Credit Card Statement",
        "Statement Summary",
        f"Statement Date : {month}",
        "Payment Due Date Total Amount Due Minimum Amount Due",
        f"16-May-2025 {R} 120.00 {R} 20.00",
        "Opening Balance Purchase/Debits Finance Charges Payment/Credit Total Amount Due",
        "0.00 620.00 0.00 0.00 620.00",
        *rows, *extra,
    ])


# --- positive ---------------------------------------------------------------

def test_older_layout_classifies_as_a_statement_with_the_month_period():
    i = C.classify_text(_older())
    assert i is not None
    assert (i.kind, i.start, i.end) == ("period", D(2025, 4, 1), D(2025, 4, 30))
    assert i.pattern == "SBM-older"


def test_period_starts_at_the_earlier_first_row_date():
    rows = [f"28-Mar-2025 SAMPLE SHOP {R} 90.00", f"2-Apr-2025 SAMPLE CAFE {R} 120.00"]
    i = C.classify_text(_older(rows=rows))
    assert (i.start, i.end) == (D(2025, 3, 28), D(2025, 4, 30))


def test_it_fills_that_month_in_the_completeness_check():
    fs, fe = C.fy_bounds("2025-26")
    months = [("April-2025", D(2025, 4, 1), D(2025, 4, 30)), ("May-2025", D(2025, 5, 1), D(2025, 5, 31))]
    entries = []
    for label, s, e in months:
        info = C.classify_text(_older(month=label, rows=[f"{s.day + 1}-{s:%b}-{s.year} SAMPLE SHOP {R} 10.00"]))
        assert info is not None
        entries.append((f"{label}.pdf", info))
    # June is missing on purpose, July onward is filled by ordinary periods
    for mth in (7, 8, 9, 10, 11, 12):
        entries.append((f"m{mth}.pdf", C.StatementInfo("period", D(2025, mth, 1), D(2025, mth, 28 if mth == 2 else 30 if mth in (9, 11) else 31))))
    cov = C.check_card("Sample", entries, fs, fe)
    assert cov.statements == 8
    gaps = " | ".join(cov.gaps)
    assert "1 Jun 2025" in gaps and "30 Jun 2025" in gaps       # June is the gap ...
    assert "1 Apr 2025" not in gaps and "1 May 2025" not in gaps  # ... April and May are not


# --- negative ---------------------------------------------------------------

def test_prose_with_a_month_year_phrase_is_not_a_statement():
    text = ("Dear customer, please note that the Statement Date : March-2025 shown in your last mailer "
            "was printed in error. Fees are listed in " + R + " in the schedule.")
    assert C.classify_text(text) is None


def test_the_date_line_alone_is_not_a_statement():
    assert C.classify_text("Statement Date : March-2025") is None


def test_the_date_line_with_a_rupee_sign_but_no_statement_structure_is_not_a_statement():
    text = f"Terms and conditions\nStatement Date : March-2025\nLate fee {R} 500.00 per month\n"
    assert C.classify_text(text) is None


def test_a_month_that_is_not_a_month_is_not_a_statement():
    assert C.classify_text(_older(month="Smarch-2025")) is None


def test_current_layout_sbm_classifies_exactly_as_before():
    i = C.classify_text("Statement Date : 7-Feb-2026")
    assert (i.kind, i.start, i.end, i.pattern) == ("date", None, D(2026, 2, 7), "SBM")


def test_other_banks_classify_as_before():
    i = C.classify_text("Billing Period 5 Jun, 2025 - 4 Jul, 2025")
    assert (i.kind, i.start, i.end, i.pattern) == ("period", D(2025, 6, 5), D(2025, 7, 4), "HDFC")
    i = C.classify_text("Statement Date:20/01/2026")
    assert (i.kind, i.end, i.pattern) == ("date", D(2026, 1, 20), "HDFC-old")
    assert C.classify_text("Terms and conditions of the card. Year summary.") is None
    assert C.classify_text("") is None and C.classify_text(None) is None


def test_a_full_date_or_period_wins_over_the_month_only_date():
    full = _older(extra=["Statement Date : 20-Apr-2025"])
    i = C.classify_text(full)
    assert (i.kind, i.end, i.pattern) == ("date", D(2025, 4, 20), "SBM")
    per = _older(extra=["Statement Period : 02/04/2025 To 01/05/2025"])
    i = C.classify_text(per)
    assert (i.kind, i.pattern) == ("period", "YES") or i.pattern in ("YES", "SBM")
    assert i.pattern != "SBM-older"
