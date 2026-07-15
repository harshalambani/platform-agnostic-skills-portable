"""
tests/skill_hdfc/hdfc_fixture_gen.py -- synthetic HDFC statement fixtures for the
HDFC multi-format rewire tests. All 4 shapes (text PDF, password-protected
PDF, XLS/XLSX with preamble+separator, CSV with renamed headers) encode the
SAME 5 synthetic transactions, so a cross-format identity test can assert
they parse to identical canonical rows. No real account data appears here.
"""
from __future__ import annotations

import csv
import io

# ---------------------------------------------------------------------------
# The 5 synthetic transactions shared by every shape.
# ---------------------------------------------------------------------------

SYN_OPENING_BALANCE = 100000.00
SYN_CLOSING_BALANCE = 140000.00

# (posting_date DD/MM/YY, value_date DD/MM/YY, narration, ref, withdrawal, deposit, balance)
SYN_TRANSACTIONS = [
    ("01/04/25", "01/04/25", "NEFT SALARY CREDIT-SYNCO",       "000000500000001", "",        "50000.00", "150000.00"),
    ("02/04/25", "02/04/25", "UPI-GROCERY STORE-GROC@OKHDFC",   "000000500000002", "2000.00", "",         "148000.00"),
    ("03/04/25", "03/04/25", "ACH D- BD-SYNTH MF-SIP0001",      "000000500000003", "5000.00", "",         "143000.00"),
    ("04/04/25", "04/04/25", "UPI-REFUND ORDER-SYNSHOP@OKHDFC", "000000500000004", "",        "500.00",   "143500.00"),
    ("05/04/25", "05/04/25", "CC 000485498XXXXXX0000 AUTOPAY SI-SYN", "000000500000005", "3500.00", "",   "140000.00"),
]

SYN_DR_COUNT = 3
SYN_CR_COUNT = 2
SYN_DEBITS = 10500.00
SYN_CREDITS = 50500.00


# ---------------------------------------------------------------------------
# Shape 1/2: text PDF (pdfplumber-readable), optionally password-protected.
# Mirrors the real HDFC layout: "DD/MM/YY <narration> <ref> <value_dt> <amt(s)> <balance>"
# transaction lines, plus a STATEMENT SUMMARY block and Date/Narration anchors.
# ---------------------------------------------------------------------------

def _pdf_lines() -> list[str]:
    lines = [
        "HDFC BANK LIMITED",
        "Statement of account",
        "Date  Narration  Chq./Ref.No.  Value Dt  Withdrawal Amt.  Deposit Amt.  Closing Balance",
    ]
    for posting, value, narr, ref, wdl, dep, bal in SYN_TRANSACTIONS:
        amt = wdl if wdl else dep
        lines.append(f"{posting} {narr} {ref} {value} {amt} {bal}")
    lines.append(
        "STATEMENT SUMMARY Opening Balance Dr Count Cr Count Debits Credits Closing Bal "
        f"{SYN_OPENING_BALANCE:.2f} {SYN_DR_COUNT} {SYN_CR_COUNT} {SYN_DEBITS:.2f} "
        f"{SYN_CREDITS:.2f} {SYN_CLOSING_BALANCE:.2f}"
    )
    return lines


def build_pdf(password: str | None = None) -> bytes:
    """Synthetic HDFC text PDF (real text layer via reportlab, not a raster
    image) encoding SYN_TRANSACTIONS. `password` encrypts it with reportlab's
    StandardEncryption when given, for testing skill_hdfc's pdf_password path."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    encrypt = StandardEncryption(password, canPrint=1, canModify=0) if password else None
    c = canvas.Canvas(buf, pagesize=A4, encrypt=encrypt)
    _, height = A4
    c.setFont("Helvetica", 9)
    y = height - 50
    for line in _pdf_lines():
        c.drawString(40, y, line)
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shape 3: XLS/XLSX net-banking export -- preamble rows + '****' separator
# before the real header row (HDFC's own quirk, see Khyati sample).
# ---------------------------------------------------------------------------

def _tabular_rows() -> list[list[str]]:
    rows: list[list[str]] = [
        ["Statement of Account"],
        ["Account No", "SYN0001234567"],
        ["Customer Name", "SYNTHETIC TEST ACCOUNT"],
        [],
        ["****", "****", "****", "****", "****", "****", "****"],
        ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"],
    ]
    for posting, value, narr, ref, wdl, dep, bal in SYN_TRANSACTIONS:
        rows.append([posting, narr, ref, value, wdl, dep, bal])
    return rows


def build_xlsx_bytes() -> bytes:
    """Synthetic HDFC XLSX export (openpyxl) with preamble rows and a '****'
    separator above the real header row, encoding SYN_TRANSACTIONS."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for row in _tabular_rows():
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shape 4: CSV export with renamed headers (Value Date/Description/Number),
# ISO dates -- mirrors the Khyati "clean" CSV export.
# ---------------------------------------------------------------------------

def build_csv_text() -> str:
    """Synthetic HDFC CSV export with renamed headers and ISO dates, encoding
    SYN_TRANSACTIONS (same rows/balances as the PDF/XLSX fixtures)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Value Date", "Description", "Number", "Withdrawal", "Deposit", "Balance"])
    for posting, value, narr, ref, wdl, dep, bal in SYN_TRANSACTIONS:
        dd, mm, yy = value.split("/")
        iso_date = f"20{yy}-{mm}-{dd}"
        writer.writerow([iso_date, narr, ref, wdl, dep, bal])
    return buf.getvalue()


if __name__ == "__main__":
    import pathlib
    out_dir = pathlib.Path(__file__).parent / "fixtures"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "syn_hdfc.pdf").write_bytes(build_pdf())
    (out_dir / "syn_hdfc_encrypted.pdf").write_bytes(build_pdf(password="SYNPWD1"))
    (out_dir / "syn_hdfc.xlsx").write_bytes(build_xlsx_bytes())
    (out_dir / "syn_hdfc.csv").write_text(build_csv_text(), encoding="utf-8")
    print("Wrote fixtures to", out_dir)
