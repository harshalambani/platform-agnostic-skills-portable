"""tests/test_icici.py — regression coverage for ICICI's Value Date preference.

transform_row() (the per-row transform factored out of transform_icici_statement)
must emit the canonical "date" field from the Value Date column, falling back to
Transaction Date only when Value Date is blank/unparseable.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_icici.agent import (
    COL_BALANCE,
    COL_CHEQUE,
    COL_DEPOSIT,
    COL_REMARKS,
    COL_TXN_DATE,
    COL_VALUE_DATE,
    COL_WITHDRAWAL,
    transform_row,
)


def _make_row(value_date: str, txn_date: str) -> list:
    """Build a synthetic ICICI data row with the real column layout:
    [offset, S No., Value Date, Transaction Date, Cheque, Remarks, Withdrawal, Deposit, Balance]
    """
    row = [''] * 9
    row[1] = '1'
    row[COL_VALUE_DATE] = value_date
    row[COL_TXN_DATE] = txn_date
    row[COL_CHEQUE] = '-'
    row[COL_REMARKS] = 'NEFT-REF123-Test Payment'
    row[COL_WITHDRAWAL] = '0'
    row[COL_DEPOSIT] = '1000.00'
    row[COL_BALANCE] = '5000.00'
    return row


def test_transform_row_prefers_value_date_over_transaction_date():
    # Value Date and Transaction Date deliberately differ (e.g. cheque clearing).
    row = _make_row(value_date='31,Mar,2025', txn_date='01,Apr,2025')
    result, issue = transform_row(row)
    assert issue is None
    assert result['date'] == '2025-03-31'  # Value Date, not Transaction Date


def test_transform_row_falls_back_to_transaction_date_when_value_date_blank():
    row = _make_row(value_date='', txn_date='01,Apr,2025')
    result, issue = transform_row(row)
    assert issue is None
    assert result['date'] == '2025-04-01'


def test_transform_row_reports_issue_when_both_dates_unparseable():
    row = _make_row(value_date='garbage', txn_date='also garbage')
    result, issue = transform_row(row)
    assert result is None
    assert issue is not None
