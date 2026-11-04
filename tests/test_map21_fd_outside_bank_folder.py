"""
MAP-21 -- the AI guard (MAP-14) must also cover an ASSET FD that sits in its
OWN folder, outside the bank accounts' branch, without widening to every asset
account: an MF, a share or a loan-to-family account must NOT start needing
own-transfer evidence.

Rule: an Assets account outside the bank branch is an own target when this
book's history shows it is fed by own transfers (>=2 rows and >=90% of its
rows carry own-transfer evidence learned from the BANK-side history).

Synthetic book (gnc_book_fixture): "Root Account:" prefix, several HDFC and
HSBC accounts, an ASSET-typed FD in its own folder, an ASSET MF and an ASSET
loan-to-family account.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402
from scripted_llm import ScriptedLLM, choose  # noqa: E402

OWN_NAME = "ownerzq"
INV = [
    fx.account_xml("inv", "Investments", "ASSET", "assets", ["placeholder"]),
    fx.account_xml("fdf", "Fixed Deposits", "ASSET", "inv", ["placeholder"]),
    fx.account_xml("fd", "ICICI FD - 5551234", "ASSET", "fdf"),
    fx.account_xml("mff", "Mutual Funds", "ASSET", "inv", ["placeholder"]),
    fx.account_xml("mf", "Growth Fund - 7777", "ASSET", "mff"),
    fx.account_xml("loan", "Loan to Relative", "ASSET", "inv"),
]
P_FD = "Assets:Investments:Fixed Deposits:ICICI FD - 5551234"
P_MF = "Assets:Investments:Mutual Funds:Growth Fund - 7777"
P_LOAN = "Assets:Investments:Loan to Relative"


def _t(desc, target, n=1):
    return [fx.txn_xml(desc, "2025-06-10", [(fx.HDFC1, -10000), (target, 10000)])
            for _ in range(n)]


def _history():
    h = []
    for i in range(4):     # the owner's own transfers between bank accounts
        h += _t(f"NEFT {OWN_NAME} SELF ACCT Q{i}", fx.HDFC2)
    for i in range(3):     # FD top-ups name the owner too; 'booking' is shared with a vendor
        h += _t(f"FD BOOKING {OWN_NAME} Q{i}", "fd")
        h += _t(f"FD BOOKING SHOP{i}", "groc")
    for i in range(3):     # MF purchases: no own-transfer shape
        h += _t(f"UNITS PURCHASE FUND Q{i}", "mf")
        h += _t(f"UNITS PURCHASE STORE{i}", "groc")
    for i in range(3):
        h += _t(f"LOAN INSTALMENT RELATIVE Q{i}", "loan")
        h += _t(f"LOAN INSTALMENT SHOP{i}", "groc")
    for i in range(24):
        h += _t(f"SHOP{i} GROCERIES", "groc")
    return h


def _accounts():
    return fx.standard_accounts(INV)


def _run(tmp_path, cfg, desc, name="r"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    book = fx.write_book(d / "b.gnucash", _accounts(), _history())
    csv_in = fx.canonical_csv(d / "in.csv", [("2025-08-01", desc, "", "100.00")])
    out = d / "out.csv"
    m.run(book, csv_in, str(out), config_path=cfg, bank_name="HDFC",
          gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[0]


def _pairs():
    return [{"description": h.split("<trn:description>")[1].split("<")[0],
             "account": a, "frequency": 1}
            for h, a in ((t, _target(t)) for t in _history())]


_ID2PATH = {"groc": "Expenses:Groceries", "fd": P_FD, "mf": P_MF, "loan": P_LOAN,
            fx.HDFC2: fx.P_HDFC2}


def _target(txn_xml):
    import re
    ids = re.findall(r'<split:account type="guid">([^<]+)<', txn_xml)
    return _ID2PATH[[i for i in ids if i != fx.HDFC1][0]]


BANKS = {fx.P_HDFC1, fx.P_HDFC2, fx.P_HDFC4, fx.P_HSBC1, fx.P_HSBC2}
ALL = BANKS | {P_FD, P_MF, P_LOAN, "Expenses:Groceries", "Assets:Suspense"}


def test_rule_classifies_the_out_of_folder_fd_but_not_mf_or_loan():
    t = m._own_target_accounts(BANKS, ALL, _pairs())
    assert P_FD in t
    assert P_MF not in t and P_LOAN not in t              # NEGATIVE: not widened
    assert "Expenses:Groceries" not in t


def test_without_history_only_the_structural_rule_applies():
    """NEGATIVE: no history -> the FD is not guessed to be an own account."""
    t = m._own_target_accounts(BANKS, ALL)
    assert P_FD not in t and P_MF not in t


def test_ai_booking_a_third_party_payment_to_the_fd_is_withheld(tmp_path, monkeypatch):
    h = ScriptedLLM(monkeypatch, tmp_path)
    try:
        h.say("ACMEVENDOR", choose(P_FD))
        r = _run(tmp_path, h.config_path, "ACMEVENDOR FD BOOKING")
        assert any(P_FD in c["user"] for c in h.real_calls())     # it really was offered
        assert r["Account"] != P_FD and r["Account"] != "Root Account:" + P_FD
        assert r["Confidence"] == "suspense"
        assert "your own account" in r["MatchReason"]
    finally:
        h.close()


def test_ai_booking_an_mf_purchase_to_the_mf_is_not_withheld(tmp_path, monkeypatch):
    h = ScriptedLLM(monkeypatch, tmp_path)
    try:
        h.say("ACMEFUND", choose(P_MF))
        r = _run(tmp_path, h.config_path, "ACMEFUND UNITS PURCHASE")
        assert any(P_MF in c["user"] for c in h.real_calls())
        assert r["Account"] == P_MF
        assert r["Confidence"] == "llm"
    finally:
        h.close()


def test_ai_booking_a_loan_instalment_to_the_loan_account_is_not_withheld(tmp_path, monkeypatch):
    h = ScriptedLLM(monkeypatch, tmp_path)
    try:
        h.say("ACMERELATIVE", choose(P_LOAN))
        r = _run(tmp_path, h.config_path, "ACMERELATIVE LOAN INSTALMENT")
        assert any(P_LOAN in c["user"] for c in h.real_calls())
        assert r["Account"] == P_LOAN and r["Confidence"] == "llm"
    finally:
        h.close()


def test_the_fd_still_gets_a_genuine_own_transfer_via_the_ai(tmp_path, monkeypatch):
    """The guard is not a ban: an owner-named row may go to the FD."""
    h = ScriptedLLM(monkeypatch, tmp_path)
    try:
        h.say("FD BOOKING", choose(P_FD))
        r = _run(tmp_path, h.config_path, f"FD BOOKING {OWN_NAME} NEWREF")
        assert r["Account"] in (P_FD,) or r["Confidence"] == "history"
        assert "Suspense" not in r["Account"]
    finally:
        h.close()


def test_sweeps_still_map_to_the_fd_from_history(tmp_path):
    r = _run(tmp_path, None, f"FD BOOKING {OWN_NAME} Q1")
    assert r["Account"] == P_FD


def test_weak_prefix_pass_does_not_book_a_third_party_to_the_own_fd(tmp_path):
    """MAP-22: was a strict xfail (RED FLAG); the weak prefix pass now goes through
    the same own-transfer evidence gate as the AI pass."""
    r = _run(tmp_path, None, "FD BOOKING ACMEVENDOR")
    assert r["Account"] != P_FD
