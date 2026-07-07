"""
tests/test_contra_detection.py — guards for the rewired contra (cross-bank
transfer) detection logic.

A contra can only settle in *another bank account*. The detector must:
  * accept a counterparty only when GnuCash types it BANK/CASH or it sits under
    a "Cash and Bank" branch — never investments/income/expense/receivables;
  * exclude the target bank itself (even though its transaction paths carry a
    "Root Account:" prefix the caller's target path does not);
  * rate a match "possible" (amount+date only) or "confirmed" (a reference/
    cheque match), and only confirmed transfers get auto-booked to the bank.
"""
from __future__ import annotations

import csv
import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Frozen app puts both src/ (for the `agents.` package) and src/agents/ (for
# bare skill_* imports) on the path; mirror that so the pipeline module loads.
for _p in (ROOT / "src", ROOT / "src" / "agents"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from skill_gnucash_reconciler.agent import (  # noqa: E402
    detect_contra_entries,
    parse_gnucash_for_reconcile,
)
from skill_gnucash_pipeline.agent import _apply_confirmed_contras  # noqa: E402


BOB = "Root Account:Assets:Current Assets:Cash and Bank:BOB - 7600"
RBI = "Root Account:Assets:Investments:Bonds:RBI Bond"
HDFC = "Root Account:Assets:Current Assets:Cash and Bank:HDFC Bank - 1579"
# gnucash_bank_account is passed to the detector with "Root Account:" stripped.
TARGET = "Assets:Current Assets:Cash and Bank:HDFC Bank - 1579"

_ACCOUNTS = {
    "h": {"name": "HDFC", "type": "BANK", "path": HDFC},
    "b": {"name": "BOB", "type": "BANK", "path": BOB},
    "r": {"name": "RBI Bond", "type": "ASSET", "path": RBI},
}


def _cheque_row():
    return [{"Date": "2025-05-29", "Description": "CHQ DEP KIRAN",
             "Deposit": "300000.00", "Withdrawal": ""}]


def test_bank_counterparty_flagged_possible():
    """A cheque deposit matching a same-size BOB movement is a *possible*
    contra (amount+date only), matched to the bank — not the RBI-bond decoy
    that shares the amount/date."""
    gd = {"accounts": _ACCOUNTS, "transactions": [
        {"date": "2025-05-30", "amount": -300000.0, "account": BOB, "description": ""},
        {"date": "2025-05-30", "amount": -300000.0, "account": RBI, "description": ""},
    ]}
    res = detect_contra_entries(_cheque_row(), gd, TARGET)
    assert len(res) == 1
    assert res[0]["contra_account"] == BOB
    assert res[0]["confidence"] == "medium"
    assert res[0]["status"] == "possible"


def test_nonbank_counterparty_never_matched():
    """The regression that started this: when only a *non-bank* account carries
    the opposite amount, there is no contra at all. The old keyword filter
    ('asset'/'current') matched investments; the new type/branch filter must
    reject them."""
    gd = {"accounts": _ACCOUNTS, "transactions": [
        {"date": "2025-05-30", "amount": -300000.0, "account": RBI, "description": ""},
    ]}
    assert detect_contra_entries(_cheque_row(), gd, TARGET) == []


def test_electronic_transfer_confirmed():
    """A NEFT deposit whose reference also appears in the BOB transaction is a
    *confirmed* contra (high confidence)."""
    rows = [{"Date": "2025-06-01", "Description": "NEFT-SBIN0001234-XYZ",
             "Deposit": "50000.00", "Withdrawal": ""}]
    gd = {"accounts": _ACCOUNTS, "transactions": [
        {"date": "2025-06-01", "amount": -50000.0, "account": BOB,
         "description": "NEFT SBIN0001234 transfer"},
    ]}
    res = detect_contra_entries(rows, gd, TARGET)
    assert len(res) == 1
    assert res[0]["confidence"] == "high"
    assert res[0]["status"] == "confirmed"


def test_bank_type_accepted_without_cash_and_bank_branch():
    """A counterparty GnuCash types BANK is accepted even if its path has no
    'Cash and Bank' node (books that don't nest banks that way)."""
    axis = "Root Account:Assets:Axis Bank"
    accounts = dict(_ACCOUNTS, x={"name": "Axis", "type": "BANK", "path": axis})
    gd = {"accounts": accounts, "transactions": [
        {"date": "2025-05-30", "amount": -300000.0, "account": axis, "description": ""},
    ]}
    res = detect_contra_entries(_cheque_row(), gd, TARGET)
    assert len(res) == 1 and res[0]["contra_account"] == axis


def test_target_bank_excluded_despite_root_prefix():
    """The target bank's own transactions (paths carry 'Root Account:', the
    target arg does not) must be excluded, so a row never matches itself."""
    gd = {"accounts": _ACCOUNTS, "transactions": [
        # Only the target HDFC has the opposite amount → nothing to match.
        {"date": "2025-05-30", "amount": -300000.0, "account": HDFC, "description": ""},
    ]}
    assert detect_contra_entries(_cheque_row(), gd, TARGET) == []


# ── Parser: account type extraction ──────────────────────────────────────────

_GNC_XML = """<?xml version="1.0" encoding="utf-8"?>
<gnc-v2 xmlns:gnc="http://www.gnucash.org/XML/gnc"
        xmlns:act="http://www.gnucash.org/XML/act"
        xmlns:trn="http://www.gnucash.org/XML/trn"
        xmlns:split="http://www.gnucash.org/XML/split"
        xmlns:ts="http://www.gnucash.org/XML/ts"
        xmlns:cmdty="http://www.gnucash.org/XML/cmdty">
 <gnc:account version="2.0.0">
  <act:name>HDFC Bank</act:name>
  <act:id type="guid">aaa</act:id>
  <act:type>BANK</act:type>
 </gnc:account>
 <gnc:account version="2.0.0">
  <act:name>RBI Bond</act:name>
  <act:id type="guid">bbb</act:id>
  <act:type>ASSET</act:type>
 </gnc:account>
 <gnc:transaction version="2.0.0">
  <trn:num>123456</trn:num>
  <trn:description>NEFT SBIN0001234</trn:description>
  <trn:date-posted><ts:date>2025-06-01 00:00:00 +0000</ts:date></trn:date-posted>
  <trn:splits>
   <trn:split>
    <split:memo>ref memo</split:memo>
    <split:value>5000000/100</split:value>
    <split:account type="guid">aaa</split:account>
   </trn:split>
  </trn:splits>
 </gnc:transaction>
</gnc-v2>
"""


def test_parse_extracts_type_and_description(tmp_path):
    book = tmp_path / "test.gnucash"
    with gzip.open(book, "wt", encoding="utf-8") as f:
        f.write(_GNC_XML)
    data = parse_gnucash_for_reconcile(str(book))
    types = {a["name"]: a.get("type") for a in data["accounts"].values()}
    assert types["HDFC Bank"] == "BANK"
    assert types["RBI Bond"] == "ASSET"
    # Transaction description now carries trn:description + trn:num + memo so
    # reference matching has something to work with.
    txn = data["transactions"][0]
    assert "SBIN0001234" in txn["description"]
    assert "123456" in txn["description"]


# ── Pipeline: booking confirmed contras to the counterparty bank ─────────────

_OUT_HEADER = "Date,Description,Account,Transfer Account,Deposit,Withdrawal\n"
_OUT_ROWS = [
    "2025-06-01,NEFT XYZ,Income:Interest,Assets:...:HDFC,50000.00,\n",
    "2025-06-02,COFFEE,Expense:Food,Assets:...:HDFC,,100.00\n",
]


def _write_out_csv(tmp_path: Path) -> Path:
    p = tmp_path / "out.csv"
    p.write_text(_OUT_HEADER + "".join(_OUT_ROWS), encoding="utf-8")
    return p


def test_apply_confirmed_contras_remaps_account(tmp_path):
    p = _write_out_csv(tmp_path)
    flags = {0: {"status": "confirmed", "contra_account": BOB}}
    n = _apply_confirmed_contras(str(p), flags)
    assert n == 1
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    # Row 0 booked to the counterparty bank (Root Account: prefix stripped).
    assert rows[0]["Account"] == BOB[len("Root Account:"):]
    # Provenance recorded for the review UI.
    assert flags[0]["mapped_account"] == "Income:Interest"
    assert flags[0]["applied_account"] == BOB[len("Root Account:"):]
    # Untouched row keeps its mapping.
    assert rows[1]["Account"] == "Expense:Food"


def test_apply_confirmed_contras_leaves_possible_alone(tmp_path):
    p = _write_out_csv(tmp_path)
    flags = {0: {"status": "possible", "contra_account": BOB}}
    n = _apply_confirmed_contras(str(p), flags)
    assert n == 0
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    assert rows[0]["Account"] == "Income:Interest"  # unchanged
