"""BNK-01 RED FLAG fix: the book-account resolution must ASK, never guess, when

  1. the statement's account number fits none of several accounts and the
     remaining candidates are IMP-10 ASSET-type fallbacks (no BANK-type account
     at the bank): an empty non-bank account is never auto-picked; and
  2. the only "evidence" is a ZERO opening balance equal to a ZERO book
     balance: that fits every empty account, so it is not evidence.

Synthetic data only. Account names and numbers are made up.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "src", ROOT / "src" / "agents", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import gnc_book_fixture as fx  # noqa: E402
from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402

NO = "55667788990011"          # the statement's number; fits no account name below
START = "2025-04-01"


def _book(tmp_path, accounts, txns=()):
    accs = [fx.account_xml("root", "Root Account", "ROOT", None),
            fx.account_xml("assets", "Assets", "ASSET", "root", ["placeholder"]),
            fx.account_xml("cab", "Cash and Bank", "ASSET", "assets", ["placeholder"]),
            fx.account_xml("eq", "Equity", "EQUITY", "root", ["placeholder"]),
            fx.account_xml("obe", "Opening Balances", "EQUITY", "eq",
                           [("equity-type", "opening-balance")])]
    for aid, name, typ in accounts:
        accs.append(fx.account_xml(aid, name, typ, "cab"))
    return fx.write_book(tmp_path / "b.gnucash", accs, list(txns))


def _bal(acc, paise):
    return fx.txn_xml("OPEN", "2025-03-01", [(acc, paise), ("obe", -paise)])


def _res(book, number=NO, opening=0.0, narrations=None):
    return pipe._get_gnucash_account_balance(
        book, "Kotak", number, opening_balance=opening, start_date=START,
        narrations=narrations)


# ---- rule 1: ASSET-fallback accounts are never auto-picked on a no-fit -------

def test_zero_opening_two_empty_asset_accounts_and_a_number_that_fits_none_asks(tmp_path):
    book = _book(tmp_path, [("a1", "Kotak Savings Old", "ASSET"), ("a2", "Kotak Deposit Old", "ASSET")])
    res = _res(book)
    assert res["found"] is False
    assert res["ambiguous"] is True
    assert res["account_name"] == ""


def test_one_asset_account_left_after_dropping_a_numbered_one_still_asks(tmp_path):
    # the numbered ASSET account carries other digits (dropped); one plain
    # ASSET account remains. Its book balance equals the (non-zero) opening --
    # real evidence on a BANK account, but not enough to pick a non-bank one.
    book = _book(tmp_path,
                 [("n", "Kotak Old - 999900001111", "ASSET"), ("p", "Kotak Savings Old", "ASSET")],
                 [_bal("p", 100000)])
    res = _res(book, opening=1000.00)
    assert res["found"] is False and res["ambiguous"] is True


def test_asset_fallback_with_nothing_in_the_narrations_also_asks_on_a_no_fit(tmp_path):
    book = _book(tmp_path, [("n", "Kotak Old - 999900001111", "ASSET"),
                            ("p", "Kotak Savings Old", "ASSET")])
    res = _res(book, narrations=["UPI/SYN"])
    assert res["found"] is False and res["ambiguous"] is True


# ---- rule 2: zero fits every empty account, so it is not evidence -------------

def test_zero_opening_two_empty_bank_accounts_no_number_asks(tmp_path):
    book = _book(tmp_path, [("b1", "Kotak Savings", "BANK"), ("b2", "Kotak Current", "BANK")])
    res = _res(book, number=None)
    assert res["found"] is False and res["ambiguous"] is True


def test_zero_opening_never_picks_a_plain_bank_account_on_a_no_fit(tmp_path):
    # the commit 1/2 mis-pick: numbered account (other digits) + one empty plain
    # BANK account, zero opening -> the plain one used to be picked on 0 == 0.
    book = _book(tmp_path, [("n", "Kotak Old - 999900001111", "BANK"),
                            ("p", "Kotak Savings", "BANK")])
    res = _res(book)
    assert res["found"] is False and res["ambiguous"] is True


def test_zero_opening_does_not_pick_between_two_zero_balance_accounts_after_activity(tmp_path):
    # both close at zero before the start date (activity netted out)
    book = _book(tmp_path, [("b1", "Kotak Savings", "BANK"), ("b2", "Kotak Current", "BANK")],
                 [_bal("b1", 5000), fx.txn_xml("OUT", "2025-03-05", [("b1", -5000), ("obe", 5000)])])
    res = _res(book, number=None)
    assert res["found"] is False and res["ambiguous"] is True


# ---- unchanged: real evidence on a BANK-type account still picks -------------

def test_a_non_zero_opening_matching_one_bank_account_still_picks_it(tmp_path):
    book = _book(tmp_path, [("b1", "Kotak Savings", "BANK"), ("b2", "Kotak Current", "BANK")],
                 [_bal("b1", 100000), _bal("b2", 500000)])
    res = _res(book, number=None, opening=1000.00)
    assert res["found"] is True and res["account_name"].endswith("Kotak Savings")
    assert "opening balance" in (res["match_note"] or "")


def test_a_non_zero_opening_picks_the_plain_bank_account_on_a_no_fit_with_its_warning(tmp_path):
    book = _book(tmp_path, [("n", "Kotak Old - 999900001111", "BANK"),
                            ("p", "Kotak Savings", "BANK")],
                 [_bal("p", 100000), _bal("n", 777700)])
    res = _res(book, opening=1000.00)
    assert res["found"] is True and res["account_name"].endswith("Kotak Savings")
    assert "account name" in (res["match_warning"] or "")


def test_a_narration_match_on_a_bank_account_still_picks_even_with_a_zero_opening(tmp_path):
    book = _book(tmp_path, [("b1", "Kotak Savings 4444555566", "BANK"),
                            ("b2", "Kotak Current 7777888899", "BANK")])
    res = _res(book, number=None, opening=0.0, narrations=["TRF TO 4444555566 SYN"])
    assert res["found"] is True and res["account_name"].endswith("4444555566")
    assert "narrations" in (res["match_note"] or "")


@pytest.mark.parametrize("typ", ["BANK", "ASSET"])
def test_a_single_candidate_is_unchanged(tmp_path, typ):
    book = _book(tmp_path, [("a", "Kotak Savings", typ)])
    res = _res(book, number=None)
    assert res["found"] is True and res["account_name"].endswith("Kotak Savings")
    if typ == "ASSET":
        assert "ASSET-type" in (res["match_warning"] or "")      # the IMP-10 warning stays
