"""
IMP-13 -- the contra check must not flag POSSIBLE against a book entry that is
already a COMPLETE own transfer (both legs own bank accounts, neither the
importing bank).

Negative tests: a genuine candidate (other leg is the importing bank, or a
non-own account, or unknown) is still flagged; IMP-11 set-aside and PIPE-09
one-to-one claiming are unchanged. Synthetic data only.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (ROOT.parent / "src",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_reconciler.agent import (  # noqa: E402
    detect_contra_entries, match_booked_own_transfers)

BOB = "Root Account:Assets:Current Assets:Cash and Bank:BOB - 7600"
SBM = "Root Account:Assets:Current Assets:Cash and Bank:SBM - 1111"
HDFC = "Root Account:Assets:Current Assets:Cash and Bank:HDFC Bank - 1579"
FOOD = "Root Account:Expenses:Food"
TARGET = "Assets:Current Assets:Cash and Bank:HDFC Bank - 1579"
_ACCOUNTS = {
    "h": {"name": "HDFC", "type": "BANK", "path": HDFC},
    "b": {"name": "BOB", "type": "BANK", "path": BOB},
    "s": {"name": "SBM", "type": "BANK", "path": SBM},
    "f": {"name": "Food", "type": "EXPENSE", "path": FOOD},
}


def _gd(*txns):
    return {"accounts": _ACCOUNTS, "transactions": list(txns)}


def _cand(account=BOB, others=None, amount=2000.0, date="2025-06-10"):
    t = {"date": date, "amount": amount, "account": account, "description": ""}
    if others is not None:
        t["other_accounts"] = others
    return t


def _wd(date="2025-06-10", desc="UPI PAYMENT", amount="2000.00"):
    return {"Date": date, "Description": desc, "Deposit": "", "Withdrawal": amount}


def test_complete_own_transfer_between_two_other_banks_is_skipped():
    # BOB <-> SBM already booked: cannot be HDFC's counterpart.
    assert detect_contra_entries([_wd()], _gd(_cand(BOB, [SBM])), TARGET) == []


def test_candidate_whose_other_leg_is_the_importing_bank_still_flagged():
    res = detect_contra_entries([_wd()], _gd(_cand(BOB, [HDFC])), TARGET)
    assert len(res) == 1 and res[0]["status"] == "possible"


def test_candidate_whose_other_leg_is_not_an_own_account_still_flagged():
    res = detect_contra_entries([_wd()], _gd(_cand(BOB, [FOOD])), TARGET)
    assert len(res) == 1


def test_candidate_with_mixed_legs_still_flagged():
    res = detect_contra_entries([_wd()], _gd(_cand(BOB, [SBM, HDFC])), TARGET)
    assert len(res) == 1


def test_candidate_with_unknown_other_legs_still_flagged():
    assert len(detect_contra_entries([_wd()], _gd(_cand(BOB)), TARGET)) == 1
    assert len(detect_contra_entries([_wd()], _gd(_cand(BOB, [])), TARGET)) == 1


def test_skipping_one_candidate_leaves_a_genuine_one_flagged():
    gd = _gd(_cand(BOB, [SBM]), _cand(SBM, [FOOD]))
    res = detect_contra_entries([_wd()], gd, TARGET)
    assert len(res) == 1 and res[0]["contra_account"] == SBM


def test_pipe09_one_to_one_unchanged():
    rows = [_wd(desc="A"), _wd(desc="B"), _wd(desc="C")]
    res = detect_contra_entries(rows, _gd(_cand(BOB, [FOOD])), TARGET)
    assert len(res) == 1


def test_imp11_set_aside_unchanged():
    scoped = {"accounts": _ACCOUNTS, "transactions": [
        {"date": "2025-06-11", "amount": -2000.0, "account": HDFC,
         "description": "", "other_accounts": [BOB]}]}
    rows = [{"date": "2025-06-10", "description": "x", "deposit": 0.0,
             "withdrawal": 2000.0}]
    out = match_booked_own_transfers(rows, scoped, TARGET)
    assert len(out) == 1 and out[0]["days_off"] == 1
