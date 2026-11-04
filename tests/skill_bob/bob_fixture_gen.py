"""
tests/skill_bob/bob_fixture_gen.py -- synthetic Bank of Baroda statement
fixtures for the P2 cross-format golden-family test. Both shapes BoB actually
produces (PDF input; native CSV it emits via extract_bob_statement.py) encode
the SAME 5 synthetic transactions, so an identity test can assert they parse
to identical canonical rows. No real account data appears here.

The synthetic PDF also exercises two BoB-specific quirks: a Cr-suffixed
balance column, and a transaction table that overflows onto a second page
WITHOUT the column-header row being repeated.
"""
from __future__ import annotations

import csv
import io

# ---------------------------------------------------------------------------
# The 5 synthetic transactions shared by every shape.
# (date DD-MM-YYYY, particulars, cheque_no, withdrawal, deposit, balance)
# Balances carry the real-world "Cr" suffix BoB prints on every row.
# ---------------------------------------------------------------------------

SYN_ACCOUNT_NUMBER = "12345678901234"
SYN_PERIOD_FROM = "01-04-2025"
SYN_PERIOD_TO = "30-04-2025"

SYN_OPENING_BALANCE = 100000.00
SYN_CLOSING_BALANCE = 140000.00

SYN_TRANSACTIONS = [
    ("01-04-2025", "NEFT SALARY CREDIT-SYNCO",      "",       "",         "50,000.00", "1,50,000.00Cr"),
    ("02-04-2025", "UPI-GROCERY STORE-SYN",          "",       "2,000.00", "",          "1,48,000.00Cr"),
    ("03-04-2025", "ACH D-BD-SYNTH MF-SIP0001",      "",       "5,000.00", "",          "1,43,000.00Cr"),
    ("04-04-2025", "UPI-REFUND ORDER-SYNSHOP",       "",       "",         "500.00",    "1,43,500.00Cr"),
    ("05-04-2025", "CC AUTOPAY SI-SYN",              "654321", "3,500.00", "",          "1,40,000.00Cr"),
]


# ---------------------------------------------------------------------------
# Shape 1: native CSV, as emitted by extract_bob_statement.write_csv() and
# consumed by agent._native_csv_to_canonical(). Mirrors BOB_NATIVE in
# tests/test_bank_skills.py: DD-MM-YYYY dates, quoted Indian-grouped amounts,
# a synthetic "Opening Balance" row that the canonical mapper drops.
# ---------------------------------------------------------------------------

def _indian_group(amount_str: str) -> str:
    """'100000.00' -> '1,00,000.00' (2-digit grouping after the first 3)."""
    whole, _, frac = amount_str.partition(".")
    neg = whole.startswith("-")
    if neg:
        whole = whole[1:]
    if len(whole) <= 3:
        grouped = whole
    else:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join(parts) + "," + tail
    return ("-" if neg else "") + grouped + ("." + frac if frac else "")


def build_native_csv_text() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["DATE", "PARTICULARS", "CHQ.NO.", "WITHDRAWALS", "DEPOSITS", "BALANCE"])
    writer.writerow([
        "01-04-2025", "Opening Balance", "", "", "",
        _indian_group(f"{SYN_OPENING_BALANCE:.2f}") + "Cr",
    ])
    for date, narr, chq, wdl, dep, bal in SYN_TRANSACTIONS:
        writer.writerow([date, narr, chq, wdl, dep, bal])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shape 2: text PDF (pdfplumber-readable via a real text layer, not a raster
# image), optionally password-protected. Column x-positions are chosen so
# ColumnMap.from_header_words() classifies withdrawal/deposit/balance tokens
# the same way a real BoB statement would. The table is split across two
# pages with NO header repeated on page 2, exercising the multi-page path.
# ---------------------------------------------------------------------------

_X_DATE = 40
_X_PARTICULARS = 90
_X_CHQ = 245
_X_WITHDRAWALS = 300
_X_DEPOSITS = 390
_X_BALANCE = 460

_ROW_HEIGHT = 14


def _draw_front_matter(c, y: float) -> float:
    c.setFont("Helvetica", 10)
    c.drawString(40, y, "BANK OF BARODA")
    y -= 16
    c.drawString(40, y, f"A/C Number : {SYN_ACCOUNT_NUMBER}")
    y -= 16
    c.drawString(
        40, y,
        f"Statement of account for the period of {SYN_PERIOD_FROM} to {SYN_PERIOD_TO}",
    )
    y -= 24
    return y


def _draw_header_row(c, y: float) -> float:
    c.setFont("Helvetica-Bold", 9)
    c.drawString(_X_DATE, y, "DATE")
    c.drawString(_X_PARTICULARS, y, "PARTICULARS")
    c.drawString(_X_CHQ, y, "CHQ.NO.")
    c.drawString(_X_WITHDRAWALS, y, "WITHDRAWALS")
    c.drawString(_X_DEPOSITS, y, "DEPOSITS")
    c.drawString(_X_BALANCE, y, "BALANCE")
    y -= _ROW_HEIGHT
    return y


def _draw_row(c, y: float, date: str, narration: str, chq: str, wdl: str, dep: str, bal: str) -> float:
    c.setFont("Helvetica", 9)
    c.drawString(_X_DATE, y, date)
    c.drawString(_X_PARTICULARS, y, narration)
    if chq:
        c.drawString(_X_CHQ, y, chq)
    if wdl:
        c.drawString(_X_WITHDRAWALS, y, wdl)
    if dep:
        c.drawString(_X_DEPOSITS, y, dep)
    c.drawString(_X_BALANCE, y, bal)
    y -= _ROW_HEIGHT
    return y


def _pdf_date(date_ddmmyyyy: str) -> str:
    """'01-04-2025' -> '01-04-25'. The raw PDF layer's DATE_RE only matches
    2-digit-year dates (extract_bob_statement._expand_date reinflates them);
    the native-CSV shape (build_native_csv_text) keeps the 4-digit year."""
    return date_ddmmyyyy[:6] + date_ddmmyyyy[8:10]


def build_pdf(password: str | None = None) -> bytes:
    """Synthetic BoB text PDF encoding SYN_TRANSACTIONS across 2 pages, the
    second WITHOUT a repeated header row. `password` encrypts it with
    reportlab's StandardEncryption when given."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    encrypt = StandardEncryption(password, canPrint=1, canModify=0) if password else None
    c = canvas.Canvas(buf, pagesize=A4, encrypt=encrypt)
    _, height = A4

    # Page 1: front matter + header + opening balance row + first 3 txns.
    y = height - 50
    y = _draw_front_matter(c, y)
    y = _draw_header_row(c, y)
    y = _draw_row(c, y, _pdf_date("01-04-2025"), "Opening Balance", "", "", "",
                  _indian_group(f"{SYN_OPENING_BALANCE:.2f}") + "Cr")
    for date, narr, chq, wdl, dep, bal in SYN_TRANSACTIONS[:3]:
        y = _draw_row(c, y, _pdf_date(date), narr, chq, wdl, dep, bal)
    c.showPage()

    # Page 2: remaining transactions, NO header repeated.
    y = height - 50
    for date, narr, chq, wdl, dep, bal in SYN_TRANSACTIONS[3:]:
        y = _draw_row(c, y, _pdf_date(date), narr, chq, wdl, dep, bal)
    c.showPage()

    c.save()
    return buf.getvalue()


def build_garbled_pdf() -> bytes:
    """A PDF whose text layer is dense (cid:NN) junk -- simulates a
    custom-font-encoded scan that text_layer_usable() must reject."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 9)
    # reportlab doesn't literally emit "(cid:NN)" text -- instead simulate an
    # unusable layer by omitting the structural anchors entirely: a page of
    # numbers with no "date"/"particulars" tokens fails the anchor check.
    y = A4[1] - 50
    for i in range(20):
        c.drawString(40, y, f"{i} 12345.00 67890.00")
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Parameterized single-page PDF builder for multi-statement consolidation
# tests (P3b): lets a caller supply its own date/txn list so several
# single-month statements can be combined into a synthetic multi-file batch.
# ---------------------------------------------------------------------------

def build_pdf_for(
    transactions: list[tuple[str, str, str, str, str, str]],
    period_from: str,
    period_to: str,
    account_number: str = SYN_ACCOUNT_NUMBER,
) -> bytes:
    """Build a single-page synthetic BoB PDF for an arbitrary transaction
    list (same 6-tuple shape as SYN_TRANSACTIONS), with its own period."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4

    y = height - 50
    c.setFont("Helvetica", 10)
    c.drawString(40, y, "BANK OF BARODA")
    y -= 16
    c.drawString(40, y, f"A/C Number : {account_number}")
    y -= 16
    c.drawString(40, y, f"Statement of account for the period of {period_from} to {period_to}")
    y -= 24
    y = _draw_header_row(c, y)
    for date, narr, chq, wdl, dep, bal in transactions:
        y = _draw_row(c, y, _pdf_date(date), narr, chq, wdl, dep, bal)
    c.showPage()
    c.save()
    return buf.getvalue()


if __name__ == "__main__":
    import pathlib
    out_dir = pathlib.Path(__file__).parent / "fixtures"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "syn_bob.pdf").write_bytes(build_pdf())
    (out_dir / "syn_bob_encrypted.pdf").write_bytes(build_pdf(password="SYNPWD1"))
    (out_dir / "syn_bob.csv").write_text(build_native_csv_text(), encoding="utf-8")
    print("Wrote fixtures to", out_dir)
