"""
MAP-23 -- the smart (keyword) pattern pass goes through the MAP-22 own-account
gate.

smart_pattern_match picks an account by a leaf word ("Insurance", "Loan", ...).
If an own bank / FD account carries that word, a THIRD PARTY's narration used
to be booked onto it. Now a smart-pass hit on an own target needs own-transfer
evidence; a refused row falls to the next pass or Suspense. History matches onto
own accounts on ordinary tokens (sweeps) are NOT gated.

Synthetic book (gnc_book_fixture). No real names or numbers.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402

OWN_NAME = "ownerzq"
FD = "fdins"
P_FD = "Assets:Current Assets:Cash and Bank:Insurance Reserve FD"
FD_ACCOUNTS = [
    fx.account_xml(FD, "Insurance Reserve FD", "ASSET", "cab"),
    fx.account_xml("cashacct", "Cash", "ASSET", "cab"),
]
P_CASH = "Assets:Current Assets:Cash and Bank:Cash"


def _t(desc, target, n=1):
    return [fx.txn_xml(desc, "2025-06-10", [(fx.HDFC1, -10000), (target, 10000)])
            for _ in range(n)]


def _history(with_sweep=False):
    h = []
    for i in range(4):
        h += _t(f"NEFT {OWN_NAME} RESERVE TOPUP Q{i}", FD)
    if with_sweep:
        for i in range(5):
            h += _t(f"AUTO SWEEP TO RESERVE DEPOSIT {i}", FD)
    h += _t("CASH WITHDRAWAL ATM 1", "cashacct")     # puts Cash in the history-known tree
    for i in range(24):
        h += _t(f"SHOP{i} GROCERY PURCHASE", "groc")
    return h


def _run(tmp_path, desc, with_sweep=False, deposit="", withdrawal="100.00"):
    d = tmp_path / "r"
    d.mkdir(exist_ok=True)
    book = fx.write_book(d / "b.gnucash", fx.standard_accounts(FD_ACCOUNTS),
                         _history(with_sweep))
    csv_in = fx.canonical_csv(d / "in.csv", [("2025-08-01", desc, deposit, withdrawal)])
    out = d / "out.csv"
    m.run(book, csv_in, str(out), bank_name="HDFC", gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[0]


def test_the_fd_is_an_own_target_in_this_fixture():
    """Guard the fixture: the FD really is treated as an own account."""
    own = m._own_target_accounts(
        {fx.P_HDFC1}, [a for a in [P_FD, fx.P_HDFC1]])
    assert m._strip_root(P_FD) in own


def test_smart_keyword_does_not_book_a_third_party_to_an_own_fd(tmp_path, monkeypatch):
    """NEGATIVE: a premium to an insurer, with no owner evidence, is not booked
    to the own FD merely because the FD's name contains 'Insurance'."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, "LIC OF INDIA PREMIUM POLICY 4455")
    assert r["Account"] != P_FD
    assert "Smart" not in r["MatchReason"] or P_FD not in r["Account"]


def test_the_ungated_matcher_would_have_picked_the_fd():
    """Control for the negative test above: the raw matcher does pick the FD."""
    acc = [m._strip_root(a) for a in (P_FD, fx.P_HDFC1)]
    hit = m.smart_pattern_match("LIC OF INDIA PREMIUM POLICY 4455", acc, "", "100.00")
    assert hit and hit["account"] == m._strip_root(P_FD)


def test_owner_named_premium_still_routes_to_the_own_fd(tmp_path, monkeypatch):
    """POSITIVE: the same keyword plus the owner's own vocabulary / marker."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, "LIC OF INDIA PREMIUM XFER TO SELF 4455")
    assert r["Account"] == P_FD


def test_history_sweep_onto_an_own_account_is_not_gated(tmp_path, monkeypatch):
    """NEGATIVE (of over-gating): a sweep seen again and again in history still
    maps to the own account on ordinary tokens."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, "AUTO SWEEP TO RESERVE DEPOSIT 9", with_sweep=True)
    assert r["Account"] == P_FD


def test_self_cheque_to_cash_is_not_refused(tmp_path, monkeypatch):
    """The self-cheque rule keeps working: the narration itself says SELF."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, "SELF 1579-CHQ PAID")
    assert r["Account"] == P_CASH
