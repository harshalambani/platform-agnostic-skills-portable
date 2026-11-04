"""
MAP-18 -- MAP-12's direction rule applied to the keyword/smart/weak pass.
A clash (INCOME-type on an outflow, EXPENSE-type on an inflow, judged by account TYPE not path since 3 Oct) leaves the row unmapped, keeps it away from the AI pass, and puts it
in Suspense with a visible reason. Same-direction matches are unchanged.
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


def _run(tmp_path, monkeypatch, dep, wd, account, confidence="smart", extra=()):
    monkeypatch.setattr(
        m, "smart_pattern_match",
        lambda *a, **k: {"account": account, "reason": "keyword 'zzq'", "confidence": confidence})
    monkeypatch.setattr(m, "_historical_prefix_match", lambda *a, **k: None)
    book = fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts(extra), [])
    monkeypatch.chdir(tmp_path)
    csv_in = fx.canonical_csv(tmp_path / "in.csv", [("2025-08-01", "ZZQ PAYMENT", dep, wd)])
    out = tmp_path / "out.csv"
    m.run(book, csv_in, str(out), config_path=None, bank_name="HDFC",
          gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[0]


def test_outflow_never_maps_to_income_by_keyword(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "", "100.00", "Income:Interest")
    assert "Income" not in r["Account"]
    assert r["Confidence"] == "suspense"
    assert "Direction clash" in r["MatchReason"]


def test_inflow_never_maps_to_expense_by_keyword(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "100.00", "", "Expenses:Food:Dining")
    assert "Expenses" not in r["Account"]
    assert r["Confidence"] == "suspense"
    assert "Direction clash" in r["MatchReason"]


def test_weak_guess_is_also_rejected(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "", "100.00", "Income:Interest", confidence="weak")
    assert r["Confidence"] == "suspense" and "Direction clash" in r["MatchReason"]


def test_same_direction_keyword_match_is_unchanged(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "", "100.00", "Expenses:Food:Dining")
    assert r["Account"].endswith("Dining") and r["Confidence"] == "smart"
    r = _run(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path,
             monkeypatch, "100.00", "", "Income:Interest")
    assert r["Account"].endswith("Interest") and r["Confidence"] == "smart"


def test_clash_helper_rules():
    assert m._direction_clash("Income:X", 0, 5)
    assert m._direction_clash("Expenses:X", 5, 0)
    assert not m._direction_clash("Liabilities:X", 5, 0)  # NOT a clash: MAP-12 rule only
    assert not m._direction_clash("Equity:X", 5, 0)
    assert not m._direction_clash("Liabilities:X", 0, 5)  # repayment allowed
    assert not m._direction_clash("Income:X", 5, 0)
    assert not m._direction_clash("Expenses:X", 0, 5)
    assert not m._direction_clash("Income:X", 5, 5)       # ambiguous row
    assert not m._direction_clash("", 0, 5)


def test_inflow_to_liabilities_by_keyword_is_not_sent_to_suspense(tmp_path, monkeypatch):
    """NEGATIVE: the guard is MAP-12's Income/Expenses rule only; money in to a
    Liabilities account through a keyword match maps as it did before MAP-18."""
    r = _run(tmp_path, monkeypatch, "100.00", "", "Liabilities:Loans:Family Loan")
    assert r["Account"].endswith("Family Loan")
    assert r["Confidence"] == "smart"
    assert "Direction clash" not in r["MatchReason"]


# ---------------------------------------------------------------------------
# MAP-18 (reopened): direction is judged by account TYPE
# ---------------------------------------------------------------------------

# an EXPENSE-type account that lives under the Income top level, and an
# expense root named "Expense" (singular), as in the real book's shape
_EXTRA = [
    fx.account_xml("biz", "Biz Income", "INCOME", "inc", ["placeholder"]),
    fx.account_xml("bizx", "Business Expenses", "EXPENSE", "biz", ["placeholder"]),
    fx.account_xml("welf", "Staff Welfare", "EXPENSE", "bizx"),
    fx.account_xml("exps", "Expense", "EXPENSE", "root", ["placeholder"]),
    fx.account_xml("tele", "Telephone", "EXPENSE", "exps"),
    fx.account_xml("liab", "Liabilities", "LIABILITY", "root", ["placeholder"]),
    fx.account_xml("cc", "Card Payable", "CREDIT", "liab"),
]
_P_WELF = "Income:Biz Income:Business Expenses:Staff Welfare"
_P_TELE = "Expense:Telephone"
_P_CC = "Liabilities:Card Payable"
_TYPES = {
    _P_WELF: "EXPENSE", _P_TELE: "EXPENSE", _P_CC: "CREDIT",
    "Income:Interest": "INCOME", "Assets:Suspense": "ASSET",
}


def test_expense_type_under_income_path_out_is_not_a_clash_by_type():
    assert not m._direction_clash(_P_WELF, 0, 5, _TYPES)
    assert not m._direction_mismatch(_P_WELF, 0, 5, _TYPES)
    # NEGATIVE: the old path rule (no type known) would have said clash
    assert m._direction_clash(_P_WELF, 0, 5, {})


def test_money_in_to_expense_type_under_singular_expense_root_clashes():
    assert m._direction_clash(_P_TELE, 5, 0, _TYPES)
    assert m._direction_mismatch(_P_TELE, 5, 0, _TYPES)
    # unknown type: the path fallback recognises "Expense" as well as "Expenses"
    assert m._direction_clash(_P_TELE, 5, 0, {})
    assert m._direction_clash("Expenses:X", 5, 0, {})


def test_money_out_to_income_type_still_clashes_and_in_is_fine():
    assert m._direction_clash("Income:Interest", 0, 5, _TYPES)
    assert not m._direction_clash("Income:Interest", 5, 0, _TYPES)
    assert not m._direction_clash(_P_TELE, 0, 5, _TYPES)


def test_asset_and_liability_targets_never_clash_either_way():
    t = {_P_CC: "CREDIT", "Assets:Suspense": "ASSET", "Equity:Opening Balances": "EQUITY"}
    for acct in t:
        assert not m._direction_clash(acct, 5, 0, t)
        assert not m._direction_clash(acct, 0, 5, t)


def test_keyword_match_to_expense_under_income_path_is_kept(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "", "100.00", _P_WELF, extra=_EXTRA)
    assert r["Account"].endswith("Staff Welfare") and r["Confidence"] == "smart"
    assert "Direction clash" not in r["MatchReason"]


def test_keyword_money_in_to_singular_expense_root_is_rejected(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "100.00", "", _P_TELE, extra=_EXTRA)
    assert r["Confidence"] == "suspense" and "Direction clash" in r["MatchReason"]
    assert "Telephone" not in r["Account"]


def test_credit_card_payment_target_is_not_rejected_either_way(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, "", "100.00", _P_CC, extra=_EXTRA)
    assert r["Account"].endswith("Card Payable") and "Direction clash" not in r["MatchReason"]
    (tmp_path / "y").mkdir()
    r = _run(tmp_path / "y", monkeypatch, "100.00", "", _P_CC, extra=_EXTRA)
    assert r["Account"].endswith("Card Payable") and "Direction clash" not in r["MatchReason"]
