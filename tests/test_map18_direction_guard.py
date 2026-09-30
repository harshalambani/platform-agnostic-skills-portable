"""
MAP-18 -- MAP-12's direction rule applied to the keyword/smart/weak pass.
A clash leaves the row unmapped, keeps it away from the AI pass, and puts it
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


def _run(tmp_path, monkeypatch, dep, wd, account, confidence="smart"):
    monkeypatch.setattr(
        m, "smart_pattern_match",
        lambda *a, **k: {"account": account, "reason": "keyword 'zzq'", "confidence": confidence})
    monkeypatch.setattr(m, "_historical_prefix_match", lambda *a, **k: None)
    book = fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts(), [])
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
    assert m._direction_clash("Liabilities:X", 5, 0)      # inflow to a liability
    assert not m._direction_clash("Liabilities:X", 0, 5)  # repayment allowed
    assert not m._direction_clash("Income:X", 5, 0)
    assert not m._direction_clash("Expenses:X", 0, 5)
    assert not m._direction_clash("Income:X", 5, 5)       # ambiguous row
    assert not m._direction_clash("", 0, 5)
