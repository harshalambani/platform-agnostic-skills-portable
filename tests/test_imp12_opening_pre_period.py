"""
IMP-12 -- the OPENING-balance check compares the statement opening with the
book as of just BEFORE the statement's first date, not the all-dates balance.

The all-dates balance already holds in-period entries (e.g. other-bank
transfers IMP-11 sets aside), so comparing it with the opening raised a false
opening-gap warning. The CLOSING check is deliberately unchanged: all-dates
book balance + the net of the rows actually imported.

Synthetic book only. Every behaviour carries NEGATIVE tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for _p in (ROOT.parent / "src", ROOT.parent / "src" / "agents"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gnc_book_fixture as fx  # noqa: E402
from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402

ACC = fx.P_HDFC1
PRE = fx.txn_xml("OPENING", "2025-03-01", [(fx.HDFC1, 10000000), ("obe", -10000000)])      # 100000.00
INPERIOD = fx.txn_xml("TRANSFER FROM OTHER BANK", "2025-04-05",
                      [(fx.HDFC1, 30000), (fx.HDFC2, -30000)])                              # +300.00


def _book(tmp_path, txns):
    return fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts(), txns)


def _rows(opening=100000.00):
    return [{"Date": "2025-04-01", "Transaction ID": "T1", "Description": "NEFT SALARY CREDIT",
             "Account": "", "Deposit": "500.00", "Withdrawal": "",
             "Balance": f"{opening + 500.00:.2f}", "Currency": "INR"}]


def _recon(tmp_path, txns, opening=100000.00):
    return pipe._reconcile_opening_balance(_rows(opening), _book(tmp_path, txns), "HDFC", None,
                                           chosen_account=ACC)


def test_helper_counts_only_splits_strictly_before_the_start_date():
    e = [("2025-03-01", 100.0), ("2025-04-01", 7.0), ("2025-04-05", 3.0), ("", 1000.0)]
    assert pipe._balance_before(e, "2025-04-01") == 100.0          # the start day itself is NOT before
    assert pipe._balance_before(e, None) == 1110.0                 # no start date: all dates
    assert pipe._balance_before(e, "2025-03-01") == 0.0            # NEGATIVE: not <=


def test_correct_opening_with_in_period_entries_booked_is_no_gap(tmp_path):
    rec = _recon(tmp_path, [PRE, INPERIOD])
    assert rec["ok"] is True                                        # NEGATIVE: no false warning
    assert "MISMATCH" not in rec["message"] and "adjust" not in rec["message"].lower()
    assert rec["gnucash_opening_balance"] == 100000.00
    assert rec["gnucash_balance"] == 100300.00                      # all-dates kept for the closing base


def test_real_opening_mismatch_still_warns_with_the_exact_gap(tmp_path):
    rec = _recon(tmp_path, [PRE, INPERIOD], opening=99000.00)
    assert rec["ok"] is False
    assert "OPENING BALANCE MISMATCH" in rec["message"] and "diff=1000.00" in rec["message"]
    assert rec["gnucash_opening_balance"] == 100000.00


def test_in_period_entries_do_not_hide_a_real_gap(tmp_path):                  # NEGATIVE
    # all-dates 100300 vs a (wrong) opening of 100300 would have matched the old way
    rec = _recon(tmp_path, [PRE, INPERIOD], opening=100300.00)
    assert rec["ok"] is False and "diff=300.00" in rec["message"]


def test_explain_opening_gap_fires_when_pre_period_matches_net_the_gap(tmp_path):
    rec = _recon(tmp_path, [PRE], opening=99500.00)                 # book 100000, statement 99500
    gap = abs(rec["gnucash_opening_balance"] - rec["statement_opening"])
    assert gap == 500.00
    pre = {"pre_period": True, "amount": -500.0}
    assert pipe.explain_opening_gap(gap, [pre]) == [pre]
    # NEGATIVE: a partial explanation is never offered
    assert pipe.explain_opening_gap(gap, [{"pre_period": True, "amount": -400.0}]) == []
    assert pipe.explain_opening_gap(gap, [{"pre_period": False, "amount": -500.0}]) == []


def test_no_start_date_falls_back_to_all_dates(tmp_path):
    res = pipe._get_gnucash_account_balance(_book(tmp_path, [PRE, INPERIOD]), "HDFC",
                                            chosen_account=ACC)
    assert res["balance"] == res["balance_before_start"] == 100300.00


# ------------------------------------------------------- closing is unchanged

def test_closing_verdict_uses_all_dates_plus_net_imported(tmp_path):
    rec = _recon(tmp_path, [PRE, INPERIOD])
    final_rows = [{"Deposit": "500.00", "Withdrawal": ""}]
    # statement: 100000 open, +300 (already booked, set aside), +500 imported
    v = pipe.final_closing_balance_verdict(rec, final_rows, 100800.00, None)
    assert "VERIFIED" in v and "MISMATCH" not in v


def test_imp11_set_aside_case_has_zero_post_import_diff_and_no_double_count(tmp_path):
    rec = _recon(tmp_path, [PRE, INPERIOD])
    final_rows = [{"Deposit": "500.00", "Withdrawal": ""}]            # the booked +300 is NOT here
    v = pipe.final_closing_balance_verdict(rec, final_rows, 100800.00, None)
    assert "100800.00" in v and "VERIFIED" in v
    # NEGATIVE: counting the set-aside row as well (100300 + 800) must not be what verifies
    both = final_rows + [{"Deposit": "300.00", "Withdrawal": ""}]
    assert "MISMATCH" in pipe.final_closing_balance_verdict(rec, both, 100800.00, None)
    # NEGATIVE: the closing base is NOT the pre-period balance (that would be 100500)
    assert "MISMATCH" in pipe.final_closing_balance_verdict(
        {**rec, "gnucash_balance": rec["gnucash_opening_balance"]}, final_rows, 100800.00, None)
