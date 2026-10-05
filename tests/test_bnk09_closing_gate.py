"""
tests/test_bnk09_closing_gate.py -- BNK-09: the book must end at the statement
closing balance, whatever was flagged as a possible duplicate or transfer.

All data is synthetic: invented bank names, account numbers and amounts.

The real-world defect: statement rows that moved money in THIS bank never
reached the book because a same-amount entry, booked from ANOTHER bank, already
sat in the shared category account (Cash, Drawings, Credit Card Payment). A row
may be skipped only when its twin is in THIS bank's own account.
"""
from __future__ import annotations

import csv
import fnmatch
import gzip
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "src" / "agents", ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import bank_balance_gate as bg  # noqa: E402
from agents.canonical_io import (IMPORT_READY_HEADERS, IMPORT_DEPOSIT_HEADER,  # noqa: E402
                                  IMPORT_WITHDRAWAL_HEADER)
from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402
from agents.skill_gnucash_reconciler.agent import (  # noqa: E402
    detect_contra_entries,
    match_booked_own_transfers,
    parse_gnucash_for_reconcile,
    reconcile,
)

HSBC = "Assets:Cash and Bank:Alpha Bank - 1111"
ICICI = "Assets:Cash and Bank:Beta Bank - 2222"
CASH = "Assets:Cash"
DRAW = "Expenses:Withdrawals:Drawings"
CARD = "Expenses:Withdrawals:Credit Card Payment"
EQ = "Equity:Opening Balances"
SAL = "Income:Salary"
TYPES = {HSBC: "BANK", ICICI: "BANK", CASH: "CASH", DRAW: "EXPENSE", CARD: "EXPENSE",
         EQ: "EQUITY", SAL: "INCOME"}


# ---------------------------------------------------------------------------
# synthetic book + statement builders
# ---------------------------------------------------------------------------

def _write_book(tmp_path: Path, txns: list, name: str = "book.gnucash") -> Path:
    """txns: [(date 'YYYY-MM-DD', description, [(account path, signed amount), ...])]"""
    paths = set()
    for _d, _desc, legs in txns:
        for acc, _a in legs:
            parts = acc.split(":")
            for i in range(1, len(parts) + 1):
                paths.add(":".join(parts[:i]))
    for t in TYPES:
        parts = t.split(":")
        for i in range(1, len(parts) + 1):
            paths.add(":".join(parts[:i]))
    ids = {pth: f"id{i}" for i, pth in enumerate(sorted(paths))}
    xml = ['<?xml version="1.0" encoding="utf-8"?>',
           '<gnc-v2 xmlns:gnc="http://www.gnucash.org/XML/gnc" '
           'xmlns:act="http://www.gnucash.org/XML/act" '
           'xmlns:trn="http://www.gnucash.org/XML/trn" '
           'xmlns:split="http://www.gnucash.org/XML/split" '
           'xmlns:ts="http://www.gnucash.org/XML/ts">',
           '<gnc:account version="2.0.0"><act:name>Root Account</act:name>'
           '<act:id type="guid">root</act:id><act:type>ROOT</act:type></gnc:account>']
    for pth in sorted(paths):
        parent = ":".join(pth.split(":")[:-1])
        xml.append(
            f'<gnc:account version="2.0.0"><act:name>{pth.split(":")[-1]}</act:name>'
            f'<act:id type="guid">{ids[pth]}</act:id><act:type>{TYPES.get(pth, "ASSET")}</act:type>'
            f'<act:parent type="guid">{ids[parent] if parent else "root"}</act:parent></gnc:account>')
    for d, desc, legs in txns:
        sp = "".join(
            f'<trn:split><split:value>{round(a * 100)}/100</split:value>'
            f'<split:account type="guid">{ids[acc]}</split:account></trn:split>'
            for acc, a in legs)
        xml.append(
            f'<gnc:transaction version="2.0.0"><trn:description>{desc}</trn:description>'
            f'<trn:date-posted><ts:date>{d} 00:00:00 +0000</ts:date></trn:date-posted>'
            f'<trn:splits>{sp}</trn:splits></gnc:transaction>')
    xml.append("</gnc-v2>")
    book = tmp_path / name
    with gzip.open(book, "wt", encoding="utf-8") as f:
        f.write("\n".join(xml))
    return book


OPEN_TXN = ("2025-03-31", "Opening", [(HSBC, 10000.0), (EQ, -10000.0)])


def _stmt(rows):
    """rows: [(date, desc, signed amount, running balance)] -> canonical dicts."""
    out = []
    for d, desc, amt, bal in rows:
        out.append({"Date": d, "Transaction ID": "", "Description": desc,
                    "Deposit": f"{amt:.2f}" if amt > 0 else "",
                    "Withdrawal": f"{-amt:.2f}" if amt < 0 else "",
                    "Balance": f"{bal:.2f}", "Currency": "INR"})
    return out


def _import_csv(tmp_path: Path, rows, name="stmt_GnuCash_import_ready.csv") -> Path:
    """rows: [(date, desc, signed amount, category account)] in import-ready layout."""
    p = tmp_path / name
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(IMPORT_READY_HEADERS))
        w.writeheader()
        for d, desc, amt, cat in rows:
            w.writerow({
                "Date": d, "Transaction ID": "", "Description": desc, "Account": cat,
                "Transfer Account": HSBC,
                IMPORT_DEPOSIT_HEADER: f"{amt:.2f}" if amt > 0 else "",
                IMPORT_WITHDRAWAL_HEADER: f"{-amt:.2f}" if amt < 0 else "",
                "Balance": "", "Currency": "INR", "Confidence": "high", "MatchReason": ""})
    return p


def _gate(tmp_path, book_txns, stmt_rows, import_rows):
    book = _write_book(tmp_path, book_txns)
    scoped = parse_gnucash_for_reconcile(str(book), account_filter="Root Account:" + HSBC)
    out = _import_csv(tmp_path, import_rows)
    lines, blocked = pipe._run_closing_gate(
        output_path=str(out), stmt_rows=_stmt(stmt_rows), scoped_data=scoped,
        filter_path="Root Account:" + HSBC, bank_account=HSBC)
    doc = bg.read_gate_sidecar(bg.gate_sidecar_path(out))
    return lines, blocked, doc, out


# ---------------------------------------------------------------------------
# (a) two banks, same amount, same category, same day: both import, gate passes
# ---------------------------------------------------------------------------

def test_a_two_banks_same_category_same_day_both_import_gate_passes(tmp_path):
    book_txns = [OPEN_TXN,
                 ("2025-04-05", "AUTO DEBIT CARD", [(ICICI, -500.0), (CARD, 500.0)])]
    stmt = [("2025-04-05", "CARD PAYMENT", -500.0, 9500.0),
            ("2025-04-07", "SALARY", 3000.0, 12500.0)]
    imp = [("2025-04-05", "CARD PAYMENT", -500.0, CARD),
           ("2025-04-07", "SALARY", 3000.0, SAL)]
    lines, blocked, doc, out = _gate(tmp_path, book_txns, stmt, imp)
    res = doc["result"]
    assert not blocked, lines
    assert res["status"] == "pass"
    assert res["unexplained"] == []
    # the other bank's entry in the shared category is NOT a twin: nothing was
    # treated as already booked, so no double-booking warning either.
    assert res["double_booking_risk"] == []
    assert doc["twins"] == [None, None]
    # and the exact-date dedup (scoped to THIS bank) leaves the row as New
    book = _write_book(tmp_path, book_txns, "b2.gnucash")
    scoped = parse_gnucash_for_reconcile(str(book), account_filter="Root Account:" + HSBC)
    rep, _ = reconcile([{"row_num": 1, "date": "2025-04-05", "description": "x",
                         "deposit": 0.0, "withdrawal": 500.0}], scoped)
    assert rep[0]["status"] == "New"
    # the bank-base file carries BOTH rows
    base = list(csv.DictReader(open(bg.bank_base_name(out), encoding="utf-8")))
    assert len(base) == 2


# ---------------------------------------------------------------------------
# (b) ATM withdrawal vs a same-day cash withdrawal from another bank
# ---------------------------------------------------------------------------

def test_b_atm_vs_other_bank_cash_withdrawal_is_not_contra_or_booked(tmp_path):
    book = _write_book(tmp_path, [
        OPEN_TXN,
        ("2025-04-06", "ATM CASH", [(ICICI, -5000.0), (CASH, 5000.0)])])
    full = parse_gnucash_for_reconcile(str(book))
    scoped = parse_gnucash_for_reconcile(str(book), account_filter="Root Account:" + HSBC)
    rows = [{"Date": "2025-04-06", "Description": "ATM WDL", "Deposit": "",
             "Withdrawal": "5000.00"}]
    # not a contra: the Cash entry's counter-account is the OTHER bank
    assert detect_contra_entries(rows, full, HSBC) == []
    # not already booked: nothing sits in THIS bank's account on that day
    recon_rows = [{"row_num": 1, "date": "2025-04-06", "description": "ATM WDL",
                   "deposit": 0.0, "withdrawal": 5000.0}]
    assert match_booked_own_transfers(recon_rows, scoped, HSBC) == []
    rep, _ = reconcile(recon_rows, scoped)
    assert rep[0]["status"] == "New"


# ---------------------------------------------------------------------------
# (c) a genuine own-bank transfer, booked from the other bank: skipped ONCE
# ---------------------------------------------------------------------------

def test_c_genuine_own_transfer_skipped_exactly_once(tmp_path):
    # the other bank's statement booked the transfer to THIS bank, a day later
    book_txns = [OPEN_TXN,
                 ("2025-04-11", "TRF TO ALPHA", [(ICICI, 20000.0), (HSBC, -20000.0)])]
    book = _write_book(tmp_path, book_txns)
    scoped = parse_gnucash_for_reconcile(str(book), account_filter="Root Account:" + HSBC)
    recon_rows = [
        {"row_num": 1, "date": "2025-04-10", "description": "TRF", "deposit": 0.0, "withdrawal": 20000.0},
        {"row_num": 2, "date": "2025-04-12", "description": "TRF", "deposit": 0.0, "withdrawal": 20000.0},
    ]
    m = match_booked_own_transfers(recon_rows, scoped, "Root Account:" + HSBC)
    assert len(m) == 1                       # one book entry pairs with ONE row only
    # gate: the first row is the skipped one (twin), the second must be imported
    stmt = [("2025-04-10", "TRF", -20000.0, -10000.0 + 20000.0 - 20000.0 + 10000.0 - 20000.0 + 20000.0),
            ]
    stmt = [("2025-04-10", "TRF", -20000.0, -10000.0),
            ("2025-04-12", "TRF", -20000.0, -30000.0)]
    imp = [("2025-04-12", "TRF", -20000.0, ICICI)]
    lines, blocked, doc, out = _gate(tmp_path, book_txns, stmt, imp)
    assert not blocked, lines
    twins = doc["twins"]
    assert sum(1 for t in twins if t) == 1   # exactly one row has a twin
    res = doc["result"]
    assert res["status"] == "pass"
    assert len(res["timing"]) == 1           # information only
    # importing the twin row as well would be a double booking: flagged loudly
    imp_both = [("2025-04-10", "TRF", -20000.0, ICICI), ("2025-04-12", "TRF", -20000.0, ICICI)]
    _l, _b, doc2, _o = _gate(tmp_path / "dbl" if (tmp_path / "dbl").mkdir() is None else tmp_path,
                             book_txns, stmt, imp_both)
    assert len(doc2["result"]["double_booking_risk"]) == 1
    assert any("double booking" in ln for ln in _l)


# ---------------------------------------------------------------------------
# (d) the gate blocks when a skipped row has no twin in THIS bank's account
# ---------------------------------------------------------------------------

def _d_scenario(tmp_path):
    book_txns = [OPEN_TXN,
                 ("2025-04-06", "ATM CASH", [(ICICI, -5000.0), (CASH, 5000.0)])]
    stmt = [("2025-04-06", "ATM WDL", -5000.0, 5000.0),
            ("2025-04-07", "SALARY", 3000.0, 8000.0)]
    imp = [("2025-04-07", "SALARY", 3000.0, SAL)]     # the ATM row was dropped
    return _gate(tmp_path, book_txns, stmt, imp)


def test_d_gate_blocks_when_skipped_row_has_no_twin(tmp_path):
    lines, blocked, doc, out = _d_scenario(tmp_path)
    res = doc["result"]
    assert blocked
    assert res["status"] == "fail"
    assert len(res["unexplained"]) == 1
    r = res["unexplained"][0]
    assert (r["date"], r["amount"]) == ("2025-04-06", -5000.0)
    assert r["reason"]
    joined = "\n".join(lines)
    assert "ATM WDL" in joined and "5000.00" in joined
    # the release file is header-only, never a stale or partial import
    base = list(csv.DictReader(open(bg.bank_base_name(out), encoding="utf-8")))
    assert base == []


def test_d_pipeline_banner_wording_lists_row_with_date_amount_account_reason(tmp_path):
    lines, blocked, doc, out = _d_scenario(tmp_path)
    row_line = next(ln for ln in lines if "ROW 2025-04-06" in ln)
    assert "intended account" in row_line and "ATM WDL" in row_line


# ---------------------------------------------------------------------------
# (e) timing-only differences that net to zero do not block
# ---------------------------------------------------------------------------

def test_e_timing_only_difference_does_not_block(tmp_path):
    # the book dates the same debit one day after the statement
    book_txns = [OPEN_TXN, ("2025-04-11", "TRF OUT", [(HSBC, -2000.0), (ICICI, 2000.0)])]
    stmt = [("2025-04-10", "TRF OUT", -2000.0, 8000.0),
            ("2025-04-30", "SALARY", 1000.0, 9000.0)]
    imp = [("2025-04-30", "SALARY", 1000.0, SAL)]
    lines, blocked, doc, out = _gate(tmp_path, book_txns, stmt, imp)
    assert not blocked, lines
    res = doc["result"]
    assert res["status"] == "pass"
    assert len(res["timing"]) == 1 and res["timing"][0]["days_off"] == 1
    assert all(c["ok"] for c in res["checks"])


# ---------------------------------------------------------------------------
# opening gap is reported separately
# ---------------------------------------------------------------------------

def test_opening_gap_reported_separately_not_as_a_row_failure(tmp_path):
    book_txns = [("2025-03-31", "Opening", [(HSBC, 9000.0), (EQ, -9000.0)])]   # book 9000, stmt 10000
    stmt = [("2025-04-07", "SALARY", 3000.0, 13000.0)]
    imp = [("2025-04-07", "SALARY", 3000.0, SAL)]
    lines, blocked, doc, out = _gate(tmp_path, book_txns, stmt, imp)
    res = doc["result"]
    assert res["opening"]["gap"] == -1000.0 and not res["opening"]["ok"]
    assert res["unexplained"] == []
    assert any("Opening gap" in ln for ln in lines)


# ---------------------------------------------------------------------------
# (f) post-import check
# ---------------------------------------------------------------------------

def _points(rows):
    return bg.statement_points(_stmt(rows))


def test_f_post_import_reports_missing_rows_exactly(tmp_path):
    stmt = [("2025-04-05", "CARD PAYMENT", -500.0, 9500.0),
            ("2025-04-06", "ATM WDL", -5000.0, 4500.0),
            ("2025-04-07", "SALARY", 3000.0, 7500.0)]
    # the book got the card payment and the salary, not the ATM row
    book = _write_book(tmp_path, [
        OPEN_TXN,
        ("2025-04-05", "c", [(HSBC, -500.0), (CARD, 500.0)]),
        ("2025-04-06", "other bank ATM", [(ICICI, -5000.0), (CASH, 5000.0)]),
        ("2025-04-07", "s", [(HSBC, 3000.0), (SAL, -3000.0)])])
    before = hashlib.sha256(book.read_bytes()).hexdigest()
    scoped = parse_gnucash_for_reconcile(str(book), account_filter="Root Account:" + HSBC)
    res = bg.post_import_check(_points(stmt), bg.own_splits(scoped, "Root Account:" + HSBC))
    assert res["status"] == "drift"
    assert res["first_drift_date"] == "2025-04-06"
    assert res["closing_diff"] == 5000.0
    assert [(m["date"], m["amount"]) for m in res["missing"]] == [("2025-04-06", -5000.0)]
    # missing-rows CSV: import_ready layout, only that row, category from the mapping
    mapped = list(csv.DictReader(open(_import_csv(
        tmp_path, [("2025-04-06", "ATM WDL", -5000.0, CASH)]), encoding="utf-8")))
    outp = tmp_path / "missing.csv"
    n = bg.write_missing_rows(res["missing"], HSBC, mapped, {}, outp)
    assert n == 1
    rows = list(csv.DictReader(open(outp, encoding="utf-8")))
    assert list(rows[0].keys()) == list(IMPORT_READY_HEADERS)
    assert rows[0]["Account"] == CASH and rows[0]["Transfer Account"] == HSBC
    assert rows[0][IMPORT_WITHDRAWAL_HEADER] == "5000.00"
    # read-only: the book file is byte-identical
    assert hashlib.sha256(book.read_bytes()).hexdigest() == before


def test_f_post_import_clean_book_reports_nothing(tmp_path):
    stmt = [("2025-04-05", "CARD PAYMENT", -500.0, 9500.0),
            ("2025-04-07", "SALARY", 3000.0, 12500.0)]
    book = _write_book(tmp_path, [
        OPEN_TXN,
        ("2025-04-05", "c", [(HSBC, -500.0), (CARD, 500.0)]),
        ("2025-04-07", "s", [(HSBC, 3000.0), (SAL, -3000.0)])])
    scoped = parse_gnucash_for_reconcile(str(book), account_filter="Root Account:" + HSBC)
    res = bg.post_import_check(_points(stmt), bg.own_splits(scoped, "Root Account:" + HSBC))
    assert res["status"] == "clean"
    assert res["missing"] == [] and res["first_drift_date"] is None
    assert res["closing_diff"] == 0.0


def test_f_post_import_timing_only_is_not_a_failure(tmp_path):
    stmt = [("2025-04-10", "TRF OUT", -2000.0, 8000.0),
            ("2025-04-12", "SALARY", 1000.0, 9000.0)]
    book = _write_book(tmp_path, [
        OPEN_TXN, ("2025-04-11", "t", [(HSBC, -2000.0), (ICICI, 2000.0)]),
        ("2025-04-12", "s", [(HSBC, 1000.0), (SAL, -1000.0)])])
    scoped = parse_gnucash_for_reconcile(str(book), account_filter="Root Account:" + HSBC)
    res = bg.post_import_check(_points(stmt), bg.own_splits(scoped, "Root Account:" + HSBC))
    assert res["status"] == "timing_only"
    assert res["first_drift_date"] == "2025-04-10" and res["closing_diff"] == 0.0
    assert res["missing"] == []


# ---------------------------------------------------------------------------
# (g) orientation: signs stay right
# ---------------------------------------------------------------------------

def _old_bank_delta(row):
    """GnuCash import, OLD preset: Account = category, deposit column mapped to
    Amount (Negated), withdrawal column to Amount. The bank is the transfer leg."""
    dep = float(row[IMPORT_DEPOSIT_HEADER] or 0)
    wd = float(row[IMPORT_WITHDRAWAL_HEADER] or 0)
    category_delta = wd - dep            # Amount - Amount(Negated)
    return -category_delta


def _new_bank_delta(row):
    """NEW preset: Account = bank, deposit column mapped to Amount, withdrawal
    column to Amount (Negated)."""
    dep = float(row[bg.BANK_BASE_DEPOSIT_HEADER] or 0)
    wd = float(row[bg.BANK_BASE_WITHDRAWAL_HEADER] or 0)
    return dep - wd


def test_g_orientation_keeps_signs_for_deposits_and_withdrawals(tmp_path):
    p = _import_csv(tmp_path, [("2025-04-05", "CARD", -500.0, CARD),
                               ("2025-04-07", "SALARY", 3000.0, SAL)])
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    out, problems = bg.to_bank_base_rows(rows)
    assert problems == []
    assert [_new_bank_delta(r) for r in out] == [-500.0, 3000.0]
    assert [_new_bank_delta(r) for r in out] == [_old_bank_delta(r) for r in rows]
    # the bank is the base Account, the category the transfer
    assert out[0]["Account"] == HSBC and out[0]["Transfer Account"] == CARD
    assert out[1]["Account"] == HSBC and out[1]["Transfer Account"] == SAL
    # a withdrawal is NOT under the plain-Amount header (that would book a deposit)
    assert out[0][bg.BANK_BASE_DEPOSIT_HEADER] == ""
    assert out[1][bg.BANK_BASE_WITHDRAWAL_HEADER] == ""


def test_g_row_without_bank_or_category_is_reported_never_guessed(tmp_path):
    rows = [{"Date": "2025-04-05", "Account": CARD, "Transfer Account": "",
             IMPORT_WITHDRAWAL_HEADER: "5.00"},
            {"Date": "2025-04-06", "Account": "", "Transfer Account": HSBC,
             IMPORT_WITHDRAWAL_HEADER: "6.00"}]
    out, problems = bg.to_bank_base_rows(rows)
    assert out == [] and len(problems) == 2
    dst = tmp_path / "x_bank_base.csv"
    n, probs = bg.write_bank_base_csv(rows, dst)
    assert n == 0 and probs
    assert list(csv.DictReader(open(dst, encoding="utf-8"))) == []


def test_g_bank_base_file_is_not_offered_as_a_review_csv(tmp_path):
    name = bg.bank_base_name(tmp_path / "HSBC_2025_GnuCash_import_ready.csv").name
    assert not fnmatch.fnmatch(name, "*GnuCash_import_ready*.csv")
    assert name.endswith(".csv") and "bank_base" in name


# ---------------------------------------------------------------------------
# contra: a transfer match needs the other entry's counter-account to be THIS bank
# ---------------------------------------------------------------------------

def test_contra_accepts_entry_whose_counter_account_is_this_bank(tmp_path):
    book = _write_book(tmp_path, [
        ("2025-04-10", "xfer", [(ICICI, 7000.0), (HSBC, -7000.0)])])
    full = parse_gnucash_for_reconcile(str(book))
    rows = [{"Date": "2025-04-10", "Description": "XFER", "Deposit": "", "Withdrawal": "7000.00"}]
    res = detect_contra_entries(rows, full, HSBC)
    assert len(res) == 1 and res[0]["status"] == "possible"


def test_contra_accepts_entry_with_unresolved_holding_counter(tmp_path):
    book = _write_book(tmp_path, [
        ("2025-04-10", "xfer", [(ICICI, 7000.0), ("Imbalance-INR", -7000.0)])])
    full = parse_gnucash_for_reconcile(str(book))
    rows = [{"Date": "2025-04-10", "Description": "XFER", "Deposit": "", "Withdrawal": "7000.00"}]
    assert len(detect_contra_entries(rows, full, HSBC)) == 1


def test_contra_rejects_entry_whose_counter_is_a_real_category(tmp_path):
    book = _write_book(tmp_path, [
        ("2025-04-10", "spend", [(ICICI, 7000.0), (SAL, -7000.0)])])
    full = parse_gnucash_for_reconcile(str(book))
    rows = [{"Date": "2025-04-10", "Description": "XFER", "Deposit": "", "Withdrawal": "7000.00"}]
    assert detect_contra_entries(rows, full, HSBC) == []


# ---------------------------------------------------------------------------
# Review tab: the gate decides whether the export is released
# ---------------------------------------------------------------------------

def test_review_export_blocked_when_gate_fails_and_released_when_it_passes(tmp_path):
    from ui.tabs import gnucash_review as rv
    lines, blocked, doc, out = _d_scenario(tmp_path)
    assert blocked
    # the user keeps the ATM row unticked (excluded): still blocked, with the row named
    rows_now = list(csv.DictReader(open(out, encoding="utf-8")))
    msg, note, base = rv._gate_on_export(out, rows_now)
    assert msg and "EXPORT BLOCKED" in msg and "ATM WDL" in msg and base is None
    # the user re-ticks it (the row joins the export): released, bank as base
    rows_now.append({"Date": "2025-04-06", "Description": "ATM WDL", "Account": CASH,
                     "Transfer Account": HSBC, IMPORT_WITHDRAWAL_HEADER: "5000.00",
                     IMPORT_DEPOSIT_HEADER: ""})
    msg, note, base = rv._gate_on_export(out, rows_now)
    assert msg is None and base
    released = list(csv.DictReader(open(base, encoding="utf-8")))
    assert len(released) == 2 and all(r["Account"] == HSBC for r in released)


def test_review_export_without_gate_record_keeps_the_old_behaviour(tmp_path):
    from ui.tabs import gnucash_review as rv
    p = _import_csv(tmp_path, [("2025-04-05", "CARD", -500.0, CARD)])
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    assert rv._gate_on_export(p, rows) == (None, "", None)
