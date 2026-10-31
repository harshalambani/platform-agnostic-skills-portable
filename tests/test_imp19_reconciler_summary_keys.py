"""IMP-19: the reconciler's UI summary reads the keys reconcile() returns."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from agents.skill_gnucash_reconciler import agent as R  # noqa: E402


def _row(n, date, dep=0.0, wd=0.0):
    return {"row_num": n, "date": date, "description": "synthetic", "deposit": dep, "withdrawal": wd}


def _txn(date, amount):
    return {"date": date, "amount": amount, "account": "Assets:Bank"}


def _go(monkeypatch, tmp_path, csv_rows, txns):
    monkeypatch.setattr(R, "parse_gnucash_for_reconcile", lambda f, c: {"transactions": txns})
    monkeypatch.setattr(R, "parse_csv", lambda p: csv_rows)
    return str(R.run("x.csv", "x.gnucash", str(tmp_path / "rep.csv")))


def test_new_rows_are_counted_not_printed_as_zero(monkeypatch, tmp_path):
    text = _go(monkeypatch, tmp_path,
               [_row(1, "2025-04-01", dep=100.0), _row(2, "2025-04-02", dep=50.0)], [])
    assert "New (will be imported): 2" in text
    assert "New (will be imported): 0" not in text


def test_missing_rows_are_counted_not_printed_as_zero(monkeypatch, tmp_path):
    text = _go(monkeypatch, tmp_path, [_row(1, "2025-04-01", dep=100.0)],
               [_txn("2025-04-01", 100.0), _txn("2025-04-09", 7.0), _txn("2025-04-10", 8.0)])
    assert "Missing from CSV: 2" in text
    assert "Missing from CSV: 0" not in text
    assert "Matched (already in GnuCash): 1" in text


def test_a_run_with_none_prints_zero(monkeypatch, tmp_path):
    text = _go(monkeypatch, tmp_path, [_row(1, "2025-04-01", dep=100.0)],
               [_txn("2025-04-01", 100.0)])
    assert "New (will be imported): 0" in text and "Missing from CSV: 0" in text
