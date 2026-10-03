"""
MAP-35: a card spend that nothing else matched goes to the entity's configured
default account at LOW confidence ("default for card spends"), not Suspense.

Synthetic fixtures only. Every behaviour carries NEGATIVE tests.
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

DRAW = "Expenses:Drawings"
REASON = "default for card spends"
POS = "POS 123456XXXXXX7890 SYNTHSHOP MUMBAI"


def _book(tmp_path, history=()):
    extra = [fx.account_xml("draw", "Drawings", "EXPENSE", "exp"),
             fx.account_xml("hdraw", "Hidden Drawings", "EXPENSE", "exp", ["hidden"])]
    txns = [fx.txn_xml(desc, d, [(fx.HSBC1, -p), (acct, p)]) for d, desc, p, acct in history]
    return fx.write_book(tmp_path / "book.gnucash", fx.standard_accounts() + extra, txns)


def _run(tmp_path, book, rows, **kw):
    csv_path = fx.canonical_csv(tmp_path / "in.csv", rows)
    out = tmp_path / "mapped.csv"
    m.run(book, csv_path, str(out), bank_name="HSBC", gnucash_bank_account=fx.P_HSBC1, **kw)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------- the pattern

def test_card_spend_pattern_matches_card_wording_on_money_out():
    out = {"Deposit": "", "Withdrawal": "100"}
    for d in (POS, "ECOM PUR SYNTHSHOP", "DEBIT CARD PURCHASE X", "CREDIT CARD TXN Y",
              "ecomm/synth/123"):
        assert m._is_card_spend(d, out), d


def test_upi_atm_cash_and_plain_narrations_are_not_card_spends():           # NEGATIVE
    out = {"Deposit": "", "Withdrawal": "100"}
    for d in ("UPI/123/SYNTHSHOP/POS", "ATM CASH W/D DEBIT CARD 12", "DEBIT CARD CASH WDL 55",
              "NEFT TO SOMEONE", "CHEQUE 000123", "IMPOSING FEE"):
        assert not m._is_card_spend(d, out), d


def test_money_in_is_never_a_card_spend():                                  # NEGATIVE
    assert not m._is_card_spend(POS, {"Deposit": "100", "Withdrawal": ""})
    assert not m._is_card_spend(POS, {"Deposit": "100", "Withdrawal": "100"})


# ------------------------------------------------------------- end to end

def test_unmatched_card_spend_goes_to_the_default_at_low(tmp_path):
    got = _run(tmp_path, _book(tmp_path), [("2026-01-10", POS, "", "900")],
               card_default_account=DRAW)
    assert got[0]["Account"] == DRAW
    assert got[0]["Confidence"] == "low"
    assert got[0]["MatchReason"].startswith(REASON)


def test_card_spend_with_a_real_match_keeps_it(tmp_path):                    # NEGATIVE
    hist = [(f"2025-0{i}-05", "POS 1111XXXXXX2222 SYNTHGROCER", 50000, "groc") for i in range(1, 7)]
    got = _run(tmp_path, _book(tmp_path, hist),
               [("2026-01-10", "POS 1111XXXXXX2222 SYNTHGROCER", "", "900")],
               card_default_account=DRAW)
    assert got[0]["Account"] == "Expenses:Groceries"
    assert not got[0]["MatchReason"].startswith(REASON)


def test_money_in_never_uses_the_default(tmp_path):                          # NEGATIVE
    got = _run(tmp_path, _book(tmp_path), [("2026-01-10", POS, "900", "")],
               card_default_account=DRAW)
    assert got[0]["Account"] != DRAW
    assert not got[0]["MatchReason"].startswith(REASON)


def test_non_card_row_still_goes_to_suspense(tmp_path):                      # NEGATIVE
    got = _run(tmp_path, _book(tmp_path), [("2026-01-10", "UPI/123/SYNTHSHOP", "", "900"),
                                           ("2026-01-11", "MISC DEBIT 77", "", "900")],
               card_default_account=DRAW)
    for r in got:
        assert r["Account"] == "Assets:Suspense" or "Suspense" in r["Account"], r
        assert r["Confidence"] == "suspense"


def test_no_default_configured_behaves_exactly_as_today(tmp_path):           # NEGATIVE
    book = _book(tmp_path)
    rows = [("2026-01-10", POS, "", "900"), ("2026-01-11", "MISC DEBIT 77", "", "50")]
    a = _run(tmp_path, book, rows)
    b = _run(tmp_path, book, rows, card_default_account=None)
    assert a == b
    assert all(r["Confidence"] == "suspense" for r in a)


def test_a_hidden_or_unknown_default_is_ignored(tmp_path):                   # NEGATIVE
    book = _book(tmp_path)
    for bad in ("Expenses:Hidden Drawings", "Expenses:No Such Account"):
        got = _run(tmp_path, book, [("2026-01-10", POS, "", "900")], card_default_account=bad)
        assert got[0]["Confidence"] == "suspense", bad
        assert got[0]["Account"] != bad


def test_confidence_counts_stay_consistent(tmp_path):
    book = _book(tmp_path)
    csv_path = fx.canonical_csv(tmp_path / "in.csv", [
        ("2026-01-10", POS, "", "900"), ("2026-01-11", "MISC DEBIT 77", "", "50")])
    out = tmp_path / "mapped.csv"
    m.run(book, csv_path, str(out), bank_name="HSBC", gnucash_bank_account=fx.P_HSBC1,
          card_default_account=DRAW)
    with open(out, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["Confidence"] for r in rows] == ["low", "suspense"]
