"""
MAP-34: employer reimbursement credits go to the entity's configured Drawings
account, and money IN to a configured Drawings account is not a direction clash.

Synthetic fixtures only (no real employer, person or account). Every behaviour
carries NEGATIVE tests.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402

DRAW = "Expenses:Drawings"            # an EXPENSE-typed Drawings: the clash case
WELFARE = "Expenses:Staff Welfare"
SALARY = "Income:Salary"
CCP = "Liabilities:Credit Card Payment"

REIMB = "NEFT FROM SYNTHCORP LTD | REF123 | KOMH"
SALARY_NARR = "TRANSFER FROM 000111XXXX | SOF NRE | JAN"


@pytest.fixture(autouse=True)
def _reset_state():
    m._DRAWINGS_ACCOUNTS.clear()
    yield
    m._DRAWINGS_ACCOUNTS.clear()


# ---------------------------------------------------------------- the rule

def test_reimbursement_pattern_matches_money_in_with_the_marker():
    assert m._is_employer_reimbursement_credit(REIMB, {"Deposit": "500", "Withdrawal": ""})
    assert m._is_employer_reimbursement_credit(
        "neft from other co pvt ltd komh 77", {"Deposit": "5", "Withdrawal": ""})


def test_salary_transfer_and_look_alikes_do_not_match():                      # NEGATIVE
    dep = {"Deposit": "500", "Withdrawal": ""}
    for d in (SALARY_NARR, "NEFT FROM SYNTHCORP LTD | SALARY JAN",
              "NEFT TO SYNTHCORP LTD | KOMH", "UPI/KOMH/123", "KOMH"):
        assert not m._is_employer_reimbursement_credit(d, dep), d


def test_money_out_with_the_marker_is_never_a_reimbursement():                # NEGATIVE
    assert not m._is_employer_reimbursement_credit(REIMB, {"Deposit": "", "Withdrawal": "500"})
    assert not m._is_employer_reimbursement_credit(REIMB, {"Deposit": "5", "Withdrawal": "5"})


# ----------------------------------------------------- direction exemption

def test_money_in_to_a_configured_drawings_account_is_not_a_clash():
    m._DRAWINGS_ACCOUNTS.add(DRAW)
    assert m._direction_clash(DRAW, 100.0, 0.0, {DRAW: "EXPENSE"}) is False
    assert m._direction_clash("Root Account:" + DRAW, 100.0, 0.0, {DRAW: "EXPENSE"}) is False


def test_an_account_not_in_the_list_gets_no_exemption():                      # NEGATIVE
    m._DRAWINGS_ACCOUNTS.add(DRAW)
    types = {DRAW: "EXPENSE", WELFARE: "EXPENSE"}
    assert m._direction_clash(WELFARE, 100.0, 0.0, types) is True
    # nothing configured: the old behaviour for Drawings itself
    m._DRAWINGS_ACCOUNTS.clear()
    assert m._direction_clash(DRAW, 100.0, 0.0, types) is True


def test_the_exemption_never_relaxes_money_out_to_income():                   # NEGATIVE
    m._DRAWINGS_ACCOUNTS.add(SALARY)
    assert m._direction_clash(SALARY, 0.0, 100.0, {SALARY: "INCOME"}) is True


# ------------------------------------------------------------- end to end

def _book(tmp_path, history=()):
    extra = [
        fx.account_xml("draw", "Drawings", "EXPENSE", "exp"),
        fx.account_xml("welf", "Staff Welfare", "EXPENSE", "exp"),
        fx.account_xml("liab", "Liabilities", "LIABILITY", "root", ["placeholder"]),
        fx.account_xml("ccp", "Credit Card Payment", "LIABILITY", "liab"),
        fx.account_xml("sal", "Salary", "INCOME", "inc"),
    ]
    txns = []
    for d, desc, paise, acct, money_in in history:
        legs = ([(fx.HSBC1, paise), (acct, -paise)] if money_in
                else [(fx.HSBC1, -paise), (acct, paise)])
        txns.append(fx.txn_xml(desc, d, legs))
    return fx.write_book(tmp_path / "book.gnucash", fx.standard_accounts() + extra, txns)


def _run(tmp_path, book, rows, **kw):
    csv_path = fx.canonical_csv(tmp_path / "in.csv", rows)
    out = tmp_path / "mapped.csv"
    m.run(book, csv_path, str(out), bank_name="HSBC", gnucash_bank_account=fx.P_HSBC1, **kw)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _salary_history():
    return [(f"2025-{i:02d}-05", SALARY_NARR.replace("JAN", f"M{i}"), 100000, "sal", True)
            for i in range(1, 9)]


def test_reimbursement_credit_lands_on_the_configured_drawings(tmp_path):
    book = _book(tmp_path)
    got = _run(tmp_path, book, [("2026-01-10", REIMB, "1200", "")], drawings_accounts=[DRAW])
    assert got[0]["Account"] == DRAW
    assert got[0]["Confidence"] == "medium"
    assert got[0]["MatchReason"].startswith("employer reimbursement = drawings")


def test_reimbursement_never_lands_on_credit_card_payment_or_welfare(tmp_path):   # NEGATIVE
    hist = [(f"2025-0{i}-05", "CREDIT CARD PAYMENT SYNTH", 50000, "ccp", False) for i in range(1, 7)]
    book = _book(tmp_path, hist)
    got = _run(tmp_path, book, [("2026-01-10", REIMB, "1200", "")], drawings_accounts=[DRAW])
    assert got[0]["Account"] == DRAW
    assert got[0]["Account"] not in (CCP, WELFARE)


def test_salary_transfer_stays_on_salary(tmp_path):                           # NEGATIVE
    book = _book(tmp_path, _salary_history())
    rows = [("2026-01-10", SALARY_NARR.replace("JAN", "M9"), "100000", "")]
    before = _run(tmp_path, book, rows)
    after = _run(tmp_path, book, rows, drawings_accounts=[DRAW])
    assert before[0]["Account"] == SALARY, before[0]
    assert after[0]["Account"] == SALARY
    assert not after[0]["MatchReason"].startswith("employer reimbursement")


def test_nothing_configured_means_no_reimbursement_rule(tmp_path):            # NEGATIVE
    book = _book(tmp_path)
    got = _run(tmp_path, book, [("2026-01-10", REIMB, "1200", "")])
    assert got[0]["Account"] != DRAW
    assert not got[0]["MatchReason"].startswith("employer reimbursement")


def test_two_configured_drawings_accounts_do_not_guess(tmp_path):              # NEGATIVE
    book = _book(tmp_path)
    got = _run(tmp_path, book, [("2026-01-10", REIMB, "1200", "")],
               drawings_accounts=[DRAW, "Expenses:Staff Welfare"])
    assert not got[0]["MatchReason"].startswith("employer reimbursement")


def test_a_configured_path_not_in_the_book_is_ignored(tmp_path):              # NEGATIVE
    book = _book(tmp_path)
    got = _run(tmp_path, book, [("2026-01-10", REIMB, "1200", "")],
               drawings_accounts=["Equity:No Such Drawings"])
    assert got[0]["Account"] != "Equity:No Such Drawings"
    assert not got[0]["MatchReason"].startswith("employer reimbursement")
