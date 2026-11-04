"""
Tests for the KR Choksey Bills reconciliation parser (Part II).

Focus: security-name extraction from a *forced square-off* equity contract note.
That layout carries a rotated "Remark" column which garbles pdfplumber's text —
the 16-digit order number collides with the trade time and the ISIN-anchored
security name wraps across lines — so the two original security parses (the
equity-header regex and the order-number trade-line regex) both miss the name.
`anchored_securities` recovers it from the trade line's economic shape.

The fixture below is a REDACTED reconstruction of the real note's extracted
text (client PII removed); the numbers/structure that drive the parse are
faithful. We never commit the real PAN-protected PDF.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def _load_parser():
    script = SRC / "agents" / "skill_krc_recon" / "scripts" / "parse_krc_bills.py"
    spec = importlib.util.spec_from_file_location("parse_krc_bills_test", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# A square-off equity note whose text extraction is garbled by the rotated
# "Remark" column: order number collides with the time (1111222233334449:33:0),
# and the security name wraps ("THE" / "NORTHGATE CEMENTS LIMITED" / "LIMITED").
SQUARE_OFF_TEXT = """\
CONTRACT NOTE CUM TAX INVOICE
KRCHOKSEY SHARES AND SECURITIES PRIVATE LIMITED
UCC & Client Code : A027 Branch Code : HO
Name of the Client : REDACTED CLIENT
PAN of Client : AB******9X CONTRACT NOTE NO : 15963
Trade Date : 26/05/2025
* Remark: Squaring off positions for non-compliance of margin norms
ICCLCM M 2526637
(NORTHCEM- NSE) THE
1111222233334449:33:0 09:40:1 NORTHGATE CEMENTS LIMITED S 100 500.0000 3.0000 497.0000 49700.00
Symbol :100000 ISIN : INE000Z01010 Net LIMITED ISIN -100 497.0000 49700.00
Total Total Sell : 100 @ 500.0000 = 50000.00
Pay In/Pay Out Obligation 50000.00 CR 0.00 CR 50000.00 CR
Taxable Value Of Supply (Brokerage) 150.00 DR 0.00 CR 150.00 DR
CGST* RATE:9% AMOUNT (RS.) 13.50 DR 0.00 CR 13.50 DR
SGST* RATE:9% AMOUNT (RS.) 13.50 DR 0.00 CR 13.50 DR
Securities Transactions Tax (Rs.) 50.00 DR 0.00 CR 50.00 DR
Net Amount Receivable/Payable By Client 49773.00 CR 0.00 CR 49773.00 CR
"""


def test_square_off_note_recovers_security():
    """The end-to-end trade parse must recover the security on this layout."""
    P = _load_parser()
    rec, _lines = P.parse_trade(SQUARE_OFF_TEXT, "CN_A027_Grp1_15963.PDF")
    assert rec["security"] == "NORTHGATE CEMENTS LIMITED"
    # The other bill fields must still parse as before.
    assert rec["cn_no"] == "15963"
    assert rec["net_amount"] == 49773.00
    assert rec["direction"] == "Receivable (Cr)"


def test_structured_regex_misses_but_anchored_recovers():
    """Documents WHY the anchored fallback is needed: on the garbled layout the
    header regex and the order-number trade-line regex both yield nothing, so the
    anchored recovery must step in and produce a trade line (with quantity)."""
    P = _load_parser()
    import re
    header = [m.group(1) for m in re.finditer(
        r"[A-Z]{2}\w{9,10}\s+([A-Z][A-Z .]+?(?:LTD|LIMITED)\.?)", SQUARE_OFF_TEXT)]
    assert header == []
    # The structured (order-number/time) trade-line regex still matches nothing;
    # the recovered line comes from the anchored fallback, not that regex.
    structured = list(re.finditer(
        r"(\d{16})\s+(\d{2}:\d{2}:\d{2})\s+(\d+)\s+(\d{2}:\d{2}:\d{2})", SQUARE_OFF_TEXT))
    assert structured == []
    _rec, lines = P.parse_trade(SQUARE_OFF_TEXT, "x.pdf")
    assert len(lines) == 1  # recovered by the anchored fallback


def test_square_off_note_recovers_quantity():
    """The regression this fix targets: the share quantity (and side) must be
    recovered on a square-off note so Part III can book a FIFO sale. Before the
    fix the trade line was missing entirely and the bill quantity was blank."""
    P = _load_parser()
    _rec, lines = P.parse_trade(SQUARE_OFF_TEXT, "CN_A027_Grp1_15963.PDF")
    assert len(lines) == 1
    leg = lines[0]
    assert leg["security"] == "NORTHGATE CEMENTS LIMITED"
    assert leg["bs"] == "SELL"
    assert leg["quantity"] == 100.0
    assert leg["rate"] == 500.0


def test_anchored_trade_lines_excludes_broker_and_reads_side():
    """The anchored leg recovery reads side/qty/rate and skips the broker's own
    letterhead name (KRCHOKSEY ... PRIVATE LIMITED)."""
    P = _load_parser()
    legs = P.anchored_trade_lines(SQUARE_OFF_TEXT)
    assert [leg["security"] for leg in legs] == ["NORTHGATE CEMENTS LIMITED"]
    assert legs[0]["bs"] == "SELL"
    assert legs[0]["quantity"] == 100.0


def test_anchored_securities_excludes_broker():
    """The anchored recovery must not pick up the broker's own letterhead name
    (KRCHOKSEY ... PRIVATE LIMITED), only the traded security."""
    P = _load_parser()
    assert P.anchored_securities(SQUARE_OFF_TEXT) == ["NORTHGATE CEMENTS LIMITED"]


def test_missing_security_degrades_gracefully():
    """If the security genuinely can't be parsed, the bill is still built with a
    blank security (and a valid amount) so the workbook can be written and the
    user can fill it in by hand."""
    P = _load_parser()
    rec, _ = P.parse_trade(
        "Net Amount Receivable/Payable By Client 100.00 CR 0.00 CR 100.00 CR\n",
        "mystery.pdf",
    )
    assert rec["security"] is None
    assert rec["net_amount"] == 100.0


def test_workbook_written_with_blank_security(tmp_path):
    """The reconciliation workbook must be produced even when a bill's security
    is blank; the Security cell is empty (not the string 'None')."""
    openpyxl = pytest.importorskip("openpyxl")
    P = _load_parser()
    bill = {
        "cn_no": "15963", "type": "TRADE", "file": "CN.PDF", "date": "26/05/2025",
        "settlement": None, "payinout": None, "security": None, "quantity": -250,
        "direction": "Receivable (Cr)", "brokerage": 1250.0, "lending_fees": None,
        "processing": None, "cgst": 112.5, "sgst": 112.5, "igst": None,
        "stt": 200.0, "stamp": None, "net_amount": 123456.75,
    }
    diag = {"n_bills": 1, "n_rows": 0, "matched_both": 0,
            "bills_total": 123456.75, "matched_total": 0.0}
    out = tmp_path / "recon.xlsx"
    P.write_workbook(str(out), [bill], [], [], [], diag)
    assert out.is_file()

    wb = openpyxl.load_workbook(out)
    ws = wb["Bills"]
    header = [c.value for c in ws[1]]
    sec_col = header.index("Security") + 1
    # Row 2 is the single data row; its Security cell must be truly blank.
    assert ws.cell(2, sec_col).value is None
    # The bill amount lands in the Net/Bill Amount cell unchanged (a wrong or
    # dropped amount must not pass silently).
    net_col = header.index("Net/Bill Amount") + 1
    assert ws.cell(2, net_col).value == 123456.75
    assert ws.cell(2, net_col).value != 0
