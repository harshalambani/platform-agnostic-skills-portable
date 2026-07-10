"""tests/test_hdfc.py — regression coverage for HDFC's Value Dt preference.

HDFC statement rows carry both a posting "Date" and a "Value Dt". The canonical
"Date" field must be emitted from Value Dt (falling back to the posting date
only when Value Dt is blank/unparseable) on all three input paths: XLS/XLSX,
PDF text (pdfplumber), and PDF OCR.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_hdfc.agent import (
    _PB_TAIL_RE,
    _TAIL_RE,
    _build_pb_txn,
    _build_pdf_txn,
    _parse_xls_transactions,
)


# ---------------------------------------------------------------------------
# XLS/XLSX path
# ---------------------------------------------------------------------------

def test_xls_transactions_use_value_dt_not_posting_date():
    header = ["Date", "Narration", "Chq./Ref.No.", "Value Dt",
              "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]
    # Posting date (01/04/25) and Value Dt (02/04/25) deliberately differ.
    row = ["01/04/25", "NEFT SALARY CREDIT", "REF123", "02/04/25",
           "", "5000.00", "105000.00"]
    rows = [header, row]

    transactions = _parse_xls_transactions(rows)

    assert len(transactions) == 1
    assert transactions[0]["Date"] == "2025-04-02"  # Value Dt, not posting date


def test_xls_transactions_use_posting_date_when_no_value_dt_column():
    header = ["Date", "Narration", "Chq./Ref.No.", "Withdrawal Amt.",
              "Deposit Amt.", "Closing Balance"]
    row = ["01/04/25", "NEFT SALARY CREDIT", "REF123", "", "5000.00", "105000.00"]
    rows = [header, row]

    transactions = _parse_xls_transactions(rows)

    assert len(transactions) == 1
    assert transactions[0]["Date"] == "2025-04-01"  # only date available


# ---------------------------------------------------------------------------
# PDF text (pdfplumber) path
# ---------------------------------------------------------------------------

def test_pdfplumber_line_parser_uses_value_dt_not_posting_date():
    # Line shape after the leading "DD/MM/YY " is stripped off by the caller:
    # "<narration...> <ref> <value_dt> <withdrawal> <deposit> <balance>"
    posting_date = "01/04/25"
    rest = "NEFT SALARY CREDIT REF1234567890 02/04/25 0.00 5000.00 105000.00"

    m_tail = _PB_TAIL_RE.search(rest)
    assert m_tail is not None

    txn = _build_pb_txn(posting_date, rest, m_tail, cont=[])

    assert txn["Date"] == "2025-04-02"  # Value Dt, not posting date (01/04/25)
    assert txn["Transaction ID"] == "REF1234567890"
    assert txn["Deposit"] == "5000.00"
    assert txn["Withdrawal"] == ""  # 0.00 -> suppressed


class _FakeMatch:
    """Minimal stand-in for a regex Match, used to exercise the fallback branch
    (value date group blank) without needing a real string that satisfies the
    strict \\d{2}/\\d{2}/\\d{2} value-date group.
    """

    def __init__(self, groups):
        self._groups = groups

    def group(self, n):
        return self._groups[n - 1]


def test_pdfplumber_falls_back_to_posting_date_when_value_dt_blank():
    posting_date = "01/04/25"
    m_tail = _FakeMatch(["REF1234567890", "", "0.00", "5000.00", "105000.00"])

    txn = _build_pb_txn(posting_date, "REF1234567890  0.00 5000.00 105000.00", m_tail, cont=[])

    assert txn["Date"] == "2025-04-01"  # falls back to posting date


# ---------------------------------------------------------------------------
# PDF OCR path
# ---------------------------------------------------------------------------

def test_ocr_line_parser_uses_value_dt_not_posting_date():
    current = {"date": "01/04/25"}
    text = "01/04/25 | NEFT SALARY CREDIT REF1234567890|02/04/25 0 5000.00 105000.00"

    m_tail = _TAIL_RE.search(text)
    assert m_tail is not None

    txn = _build_pdf_txn(current, text, m_tail)

    assert txn["Date"] == "2025-04-02"  # Value Dt, not posting date (01/04/25)
    assert txn["Transaction ID"] == "REF1234567890"


def test_ocr_falls_back_to_posting_date_when_value_dt_blank():
    current = {"date": "01/04/25"}
    m_tail = _FakeMatch(["REF1234567890", "", "0", "5000.00", "105000.00"])

    txn = _build_pdf_txn(current, "01/04/25 | REF1234567890| 0 5000.00 105000.00", m_tail)

    assert txn["Date"] == "2025-04-01"  # falls back to posting date
