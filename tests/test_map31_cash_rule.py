"""
Built-in "ATM cash withdrawal -> Cash" rule (MEDIUM, money out only).

Synthetic fixtures only. Every behaviour carries NEGATIVE tests: the rule must
NOT fire on fees, deposits, reversals, ambiguous or unusable targets, and must
NOT override a better saved/history match.
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

CASH = "Assets:Current Assets:Cash in Hand"
ATM = "ATM CASH W/D 123456 SYNTHETIC BRANCH"


# ------------------------------------------------------------- pure pattern

def test_patterns_match_the_known_bank_forms_on_money_out():
    for d in ("ATM CASH W/D 1234 MUMBAI", "atm cash w/d 99", "CASH WDL/123/BRANCH",
              "CAM/0123/CASH WDL/SYNTH/01", "ATM CASH WDL 55"):
        assert m._is_cash_withdrawal(d, 0.0, 500.0), d


def test_patterns_do_not_match_fees_cashback_or_look_alikes():                 # NEGATIVE
    for d in ("ATM TRANSFER | ATM CASH W/D markup fee",
              "ATM TRANSFER | CGST on markup fee", "ATM TRANSFER | SGST on markup fee",
              "DEBIT CARD CASH BACK | FOR ELIGIBLE SPENDS", "POS PURCHASE WITH CASH BACK",
              "REVERSAL OF ATM CASH W/D", "UPI/CASH WDL/1234", "CASH WITHDRAWALS CHARGES"):
        assert not m._is_cash_withdrawal(d, 0.0, 500.0), d


def test_a_deposit_with_an_atm_pattern_is_never_a_withdrawal():               # NEGATIVE
    assert not m._is_cash_withdrawal(ATM, 500.0, 0.0)
    assert not m._is_cash_withdrawal(ATM, 0.0, 0.0)
    assert not m._is_cash_withdrawal(ATM, 500.0, 500.0)


# ------------------------------------------------------------ target choice

def _acct(path, typ="CASH", flags=()):
    from agents.gnucash_accounts import GncAccount
    return GncAccount(id=path, name=path.split(":")[-1], type=typ, parent_id=None,
                      path=path, special_flags=tuple(flags))


def test_single_cash_account_is_the_target():
    got, why = m.find_cash_account([_acct("Assets:Cash in Hand")])
    assert got == "Assets:Cash in Hand" and why == ""


def test_no_cash_account_no_target():                                          # NEGATIVE
    got, why = m.find_cash_account([_acct("Assets:Bank", typ="BANK")])
    assert got is None and "no usable CASH" in why


def test_two_cash_accounts_no_target():                                        # NEGATIVE
    got, why = m.find_cash_account([_acct("Assets:Cash A"), _acct("Assets:Cash B")])
    assert got is None and "2 CASH accounts" in why


def test_hidden_or_placeholder_cash_is_never_the_target():                     # NEGATIVE
    assert m.find_cash_account([_acct("Assets:Old Cash", flags=("hidden",))])[0] is None
    assert m.find_cash_account([_acct("Assets:Group Cash", flags=("placeholder",))])[0] is None
    # a hidden second account does not make the single usable one ambiguous
    got, _ = m.find_cash_account([_acct("Assets:Old Cash", flags=("hidden",)),
                                  _acct("Assets:Cash in Hand")])
    assert got == "Assets:Cash in Hand"


# --------------------------------------------------------------- end to end

def _book(tmp_path, cash_accounts=((CASH, "CASH", ()),), history=()):
    """standard chart + the given CASH accounts + HSBC history txns
    (date, description, amount_paise_out, expense_account_id)."""
    extra = [fx.account_xml("cih", "Cash in Hand", "CASH", "ca")]
    txns = []
    accts = fx.standard_accounts()
    # replace the single default cash with the requested list
    extra = []
    for i, (path, typ, flags) in enumerate(cash_accounts):
        extra.append(fx.account_xml(f"cash{i}", path.split(":")[-1], typ, "ca", list(flags)))
    for d, desc, paise, acct in history:
        txns.append(fx.txn_xml(desc, d, [(fx.HSBC1, -paise), (acct, paise)]))
    return fx.write_book(tmp_path / "book.gnucash", accts + extra, txns)


def _run(tmp_path, book, rows):
    csv_path = fx.canonical_csv(tmp_path / "in.csv", rows)
    out = tmp_path / "mapped.csv"
    m.run(book, csv_path, str(out), bank_name="HSBC", gnucash_bank_account=fx.P_HSBC1)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _acc(row):
    return row.get("Account") or ""


def test_atm_withdrawal_goes_to_the_single_cash_account_at_medium(tmp_path):
    book = _book(tmp_path)
    got = _run(tmp_path, book, [("2026-01-10", ATM, "", "2000")])
    assert _acc(got[0]) == "Assets:Current Assets:Cash in Hand"
    assert got[0]["Confidence"] == "medium"
    assert got[0]["MatchReason"].startswith(
        "built-in: ATM cash withdrawal -> Assets:Current Assets:Cash in Hand")


def test_markup_fee_and_cashback_and_deposit_stay_suspense(tmp_path):          # NEGATIVE
    book = _book(tmp_path)
    got = _run(tmp_path, book, [
        ("2026-01-10", "ATM TRANSFER | ATM CASH W/D markup fee", "", "20"),
        ("2026-01-10", "ATM TRANSFER | CGST on markup fee", "", "4"),
        ("2026-01-11", "DEBIT CARD CASH BACK | FOR ELIGIBLE SPENDS", "25", ""),
        ("2026-01-12", ATM, "2000", ""),
    ])
    for r in got:
        assert "Cash in Hand" not in _acc(r)
        assert "built-in: ATM cash" not in r["MatchReason"]


def test_no_cash_account_gives_suspense_with_the_reason(tmp_path):             # NEGATIVE
    book = _book(tmp_path, cash_accounts=())
    got = _run(tmp_path, book, [("2026-01-10", ATM, "", "2000")])
    assert "Cash" not in _acc(got[0])
    assert got[0]["Confidence"] in ("none", "", "suspense")
    assert "no usable CASH account" in got[0]["MatchReason"]


def test_two_cash_accounts_give_suspense_with_the_reason(tmp_path):            # NEGATIVE
    book = _book(tmp_path, cash_accounts=((CASH, "CASH", ()),
                                          ("Assets:Current Assets:Petty Cash", "CASH", ())))
    got = _run(tmp_path, book, [("2026-01-10", ATM, "", "2000")])
    assert "Cash" not in _acc(got[0])
    assert "2 CASH accounts" in got[0]["MatchReason"]


def test_hidden_cash_account_is_never_the_target_end_to_end(tmp_path):         # NEGATIVE
    book = _book(tmp_path, cash_accounts=((CASH, "CASH", ("hidden",)),))
    got = _run(tmp_path, book, [("2026-01-10", ATM, "", "2000")])
    assert "Cash in Hand" not in _acc(got[0])
    assert "no usable CASH account" in got[0]["MatchReason"]


def test_recent_history_to_another_account_still_wins(tmp_path):              # NEGATIVE
    hist = [(f"2025-{mo:02d}-05", ATM, 200000, "groc") for mo in range(1, 9)]
    book = _book(tmp_path, history=hist)
    got = _run(tmp_path, book, [("2026-01-10", ATM, "", "2000")])
    assert "Cash in Hand" not in _acc(got[0])
    assert "built-in: ATM cash" not in got[0]["MatchReason"]


def test_aged_old_history_loses_to_the_builtin_rule(tmp_path):
    hist = [("2012-03-05", ATM, 200000, "groc")]
    book = _book(tmp_path, history=hist)
    got = _run(tmp_path, book, [("2026-01-10", ATM, "", "2000")])
    assert _acc(got[0]) == "Assets:Current Assets:Cash in Hand"
    assert got[0]["Confidence"] == "medium"
