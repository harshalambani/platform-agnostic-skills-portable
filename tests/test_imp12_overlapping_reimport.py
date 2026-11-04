"""
IMP-12 follow-up -- the OVERLAPPING RE-IMPORT, end to end.

The opening check now compares the statement opening with the book as of just
before the statement's first date. For an overlapping re-import (the book
already holds the first N rows of THIS account's statement) that balance
matches, so the old date-based "Scenario A" row skip no longer runs. This
proves the overlap is still caught (Phase-4 dedup / IMP-11 set-aside) and that
the closing check would NOT verify if any overlapping row slipped through.

Synthetic data only; the book is built in code.

Statement (tests/skill_hdfc fixture): opening 100000.00, five rows
  04-01 +50000 salary | 04-02 -2000 grocery | 04-03 -5000 SIP |
  04-04 +500 refund   | 04-05 -3500 autopay          closing 140000.00
Book (account HDFC1): the opening, rows 1 and 2 booked by an EARLIER import of
an overlapping statement (descriptions worded differently), and the SIP booked
from the OTHER bank's statement one day later (the IMP-11 set-aside case).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (ROOT, ROOT / "skill_hdfc", ROOT.parent / "src", ROOT.parent / "src" / "agents"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gnc_book_fixture as fx  # noqa: E402
import hdfc_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402


def _book(tmp_path):
    txns = [
        fx.txn_xml("OPENING", "2025-03-01", [(fx.HDFC1, 10000000), ("obe", -10000000)]),
        # earlier import of an overlapping statement; wording differs from the new one
        fx.txn_xml("Salary credit SYNCO", "2025-04-01", [(fx.HDFC1, 5000000), ("int", -5000000)]),
        fx.txn_xml("Grocery store UPI", "2025-04-02", [(fx.HDFC1, -200000), ("groc", 200000)]),
        # the SIP, booked from the other bank's statement one day later
        fx.txn_xml("SIP transfer", "2025-04-04", [(fx.HDFC1, -500000), (fx.HDFC2, 500000)]),
    ]
    return fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts(), txns)


def _run(tmp_path):
    csv_path = tmp_path / "stmt.csv"
    csv_path.write_text(fixture_gen.build_csv_text(), encoding="utf-8")
    out = tmp_path / "out.csv"
    result = pipe.run(bank="HDFC", statement_files=str(csv_path), gnucash_file=_book(tmp_path),
                      output_path=str(out), config_path=None, bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return result, rows, out


def _dep_wd(r):
    from agents.canonical_io import row_deposit, row_withdrawal
    return row_deposit(r), row_withdrawal(r)


def test_overlapping_rows_are_not_imported_again_and_closing_verifies(tmp_path):
    result, rows, out = _run(tmp_path)
    dates = [r["Date"] for r in rows]
    # (i) rows 1 and 2 (already booked) are gone from the importable CSV
    assert "2025-04-01" not in dates and "2025-04-02" not in dates, rows
    # the other-bank transfer is set aside unticked, not imported and not lost
    assert "2025-04-03" not in dates
    side = json.loads(out.with_suffix(".matched.json").read_text(encoding="utf-8"))
    parked = side if isinstance(side, list) else side.get("matches", side.get("rows", []))
    assert len(parked) == 1 and "2025-04-03" in json.dumps(parked)
    # only the two genuinely new rows remain
    assert sorted(dates) == ["2025-04-04", "2025-04-05"]
    # no false opening-gap warning, and (ii) the closing check verifies at 0.00
    assert "Opening balance gap" not in result
    assert "VERIFIED" in result and "MISMATCH" not in result


def test_closing_would_not_verify_if_an_overlapping_row_got_through(tmp_path):   # NEGATIVE
    _run(tmp_path)
    import agents.skill_gnucash_pipeline.agent as p
    book = str(tmp_path / "b.gnucash")
    rec = p._reconcile_opening_balance(
        [{"Date": "2025-04-01", "Transaction ID": "T1", "Description": "x", "Account": "",
          "Deposit": "50000.00", "Withdrawal": "", "Balance": "150000.00", "Currency": "INR"}],
        book, "HDFC", None, chosen_account=fx.P_HDFC1)
    assert rec["ok"] is True and rec["gnucash_balance"] == 143000.00
    good = [{"Deposit": "500.00", "Withdrawal": ""}, {"Deposit": "", "Withdrawal": "3500.00"}]
    assert "VERIFIED" in p.final_closing_balance_verdict(rec, good, 140000.00, None)
    # force one overlapping row (the 50000 salary) through: must not verify
    forced = good + [{"Deposit": "50000.00", "Withdrawal": ""}]
    bad = p.final_closing_balance_verdict(rec, forced, 140000.00, None)
    assert "VERIFIED" not in bad and "MISMATCH" in bad
