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
    _PB_FOOTER_RE,
    _TAIL_RE,
    _build_pb_txn,
    _build_pdf_txn,
    _parse_pdf_pdfplumber,
    _parse_xls_transactions,
    _resolve_ambiguous_amounts,
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

    _PB_TAIL_RE has 3 groups: ref, value_date, and a single amounts "blob"
    string holding 2 or 3 whitespace-separated numbers (see _build_pb_txn).
    """

    def __init__(self, groups):
        self._groups = groups

    def group(self, n):
        return self._groups[n - 1]


def test_pdfplumber_falls_back_to_posting_date_when_value_dt_blank():
    posting_date = "01/04/25"
    m_tail = _FakeMatch(["REF1234567890", "", "0.00 5000.00 105000.00"])

    txn = _build_pb_txn(posting_date, "REF1234567890  0.00 5000.00 105000.00", m_tail, cont=[])

    assert txn["Date"] == "2025-04-01"  # falls back to posting date


def test_pdfplumber_two_number_line_is_flagged_ambiguous():
    """Some HDFC PDF exports omit the blank withdrawal/deposit column
    entirely instead of printing a literal "0.00" -- only 2 trailing numbers
    (amount, balance). This must be flagged for _resolve_ambiguous_amounts,
    not silently mis-assigned to Withdrawal or Deposit."""
    posting_date = "01/04/25"
    rest = "NEFT SALARY CREDIT REF1234567890 02/04/25 5000.00 105000.00"

    m_tail = _PB_TAIL_RE.search(rest)
    assert m_tail is not None

    txn = _build_pb_txn(posting_date, rest, m_tail, cont=[])

    assert txn["Withdrawal"] == ""
    assert txn["Deposit"] == ""
    assert txn["Balance"] == "105000.00"
    assert "_ambiguous_amount" in txn


def test_resolve_ambiguous_amounts_uses_running_balance_direction():
    transactions = [
        {"Balance": "105000.00", "Withdrawal": "", "Deposit": "",
         "_ambiguous_amount": "5000.00"},   # balance rose -> deposit
        {"Balance": "104000.00", "Withdrawal": "", "Deposit": "",
         "_ambiguous_amount": "1000.00"},   # balance fell -> withdrawal
    ]

    _resolve_ambiguous_amounts(transactions, opening_balance=100000.00)

    assert transactions[0]["Deposit"] == "5000.00"
    assert transactions[0]["Withdrawal"] == ""
    assert transactions[1]["Withdrawal"] == "1000.00"
    assert transactions[1]["Deposit"] == ""


# ---------------------------------------------------------------------------
# Regression: footer/skip filtering must not discard real transaction lines
# whose UPI narration happens to contain footer-like substrings (e.g. HDFC's
# own bank name appears in UPI VPA domains like "...@HDFCBANK"). This bug
# silently dropped 16 of 899 real transactions before being fixed.
# ---------------------------------------------------------------------------

def test_footer_regex_does_not_match_upi_hdfcbank_vpa_transaction_line():
    line = ("06/04/25 UPI-BLINKIT-BLINKIT.PAYU@HDFCBANK-HDFC0M "
            "0000102716301847 06/04/25 146.00 635,512.16")
    # The footer regex DOES match this line's "HDFCBANK" substring in
    # isolation -- that's expected and fine, since the parser only trusts
    # the footer/skip regexes when the line does NOT already look like a
    # complete transaction row.
    assert _PB_FOOTER_RE.search(line) is not None


def test_pdfplumber_parser_extracts_transaction_with_hdfcbank_in_narration(tmp_path):
    """End-to-end guard: a transaction line containing '@HDFCBANK' in its
    narration must still be extracted, not dropped as footer noise."""
    import pdfplumber
    from reportlab.pdfgen import canvas

    pdf_path = tmp_path / "sample.pdf"
    c = canvas.Canvas(str(pdf_path))
    lines = [
        "Statement of account",
        "Date  Narration  Chq./Ref.No.  Value Dt  Withdrawal Amt.  Deposit Amt.  Closing Balance",
        "06/04/25 UPI-BLINKIT-BLINKIT.PAYU@HDFCBANK-HDFC0M 0000102716301847 06/04/25 146.00 635,512.16",
        "ERUPI-102716301847-UPIINTENT",
        "07/04/25 ACH D- BD-AXIS MF-TXZS34497634 0000006952566647 07/04/25 2,500.00 633,012.16",
    ]
    y = 800
    for line in lines:
        c.drawString(50, y, line)
        y -= 20
    c.save()

    with pdfplumber.open(str(pdf_path)) as pdf:
        full_text = "\n".join(
            (page.extract_text(x_tolerance=1) or "") for page in pdf.pages
        )

    from agents.skill_hdfc.agent import _text_layer_usable
    assert _text_layer_usable(full_text)

    transactions, _summary, usable = _parse_pdf_pdfplumber(str(pdf_path))
    assert usable
    refs = [t["Transaction ID"] for t in transactions]
    assert "102716301847" in refs, (
        "transaction with '@HDFCBANK' in its narration must not be dropped"
    )


# ---------------------------------------------------------------------------
# PDF OCR path
# ---------------------------------------------------------------------------

def test_ocr_line_parser_uses_value_dt_not_posting_date():
    current = {"date": "01/04/25"}
    text = "01/04/25 | NEFT SALARY CREDIT REF1234567890|02/04/25 0.00 5000.00 105000.00"

    m_tail = _TAIL_RE.search(text)
    assert m_tail is not None

    txn = _build_pdf_txn(current, text, m_tail)

    assert txn["Date"] == "2025-04-02"  # Value Dt, not posting date (01/04/25)
    assert txn["Transaction ID"] == "REF1234567890"


def test_ocr_falls_back_to_posting_date_when_value_dt_blank():
    current = {"date": "01/04/25"}
    m_tail = _FakeMatch(["REF1234567890", "", "0.00 5000.00 105000.00"])

    txn = _build_pdf_txn(current, "01/04/25 | REF1234567890| 0.00 5000.00 105000.00", m_tail)

    assert txn["Date"] == "2025-04-01"  # falls back to posting date


def test_ocr_tail_re_tolerates_missing_pipe_before_value_date():
    """Tesseract inconsistently recognises the '|' separator between the ref
    number and the value date -- sometimes a real pipe, sometimes just
    whitespace. Both must parse to the same transaction."""
    current = {"date": "01/04/25"}
    text_with_pipe = "01/04/25 |UPI-MITTAL SHRENIK 0000102407936986| 01/04/25 8,000.00 700,111.94"
    text_without_pipe = "01/04/25 |UPI-MITTAL SHRENIK 0000102407936986 01/04/25 8,000.00 700,111.94"

    for text in (text_with_pipe, text_without_pipe):
        m_tail = _TAIL_RE.search(text)
        assert m_tail is not None, text
        txn = _build_pdf_txn(current, text, m_tail)
        assert txn["Date"] == "2025-04-01"
        assert txn["Transaction ID"] == "102407936986"  # leading zeros stripped


def test_ocr_two_number_line_is_flagged_ambiguous_and_resolved():
    """OCR'd lines routinely carry only 2 trailing numbers (amount, balance)
    rather than 3 (withdrawal, deposit, balance) -- same ambiguity as the
    pdfplumber path, resolved the same way via running-balance direction."""
    current = {"date": "01/04/25"}
    text = "01/04/25 |UPI-MITTAL SHRENIK 0000102407936986 01/04/25 8,000.00 700,111.94"
    m_tail = _TAIL_RE.search(text)
    assert m_tail is not None
    txn = _build_pdf_txn(current, text, m_tail)
    assert txn["Withdrawal"] == ""
    assert txn["Deposit"] == ""
    assert "_ambiguous_amount" in txn

    transactions = [txn]
    _resolve_ambiguous_amounts(transactions, opening_balance=708111.94)
    assert transactions[0]["Withdrawal"] == "8000.00"  # balance fell -> withdrawal
    assert transactions[0]["Deposit"] == ""
