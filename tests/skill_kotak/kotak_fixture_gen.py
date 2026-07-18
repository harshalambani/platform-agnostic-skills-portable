"""
tests/skill_kotak/kotak_fixture_gen.py -- synthetic Kotak Mahindra Bank
savings-account statement fixtures for the bank-abstraction P4 tests.

All data below is INVENTED (account number, names, amounts, dates). It does
NOT reference the real sample statement in any way.

The synthetic PDF is built from bordered ``reportlab.platypus.Table``
flowables (GRID style), matching a real bank statement's ruled-line layout,
so ``pdfplumber.Page.extract_tables()`` — the extraction strategy
``extract_kotak_statement.py`` uses — recovers real per-cell text. It
exercises every documented Kotak quirk:

  - 7 columns: # | Date | Description | Chq/Ref. No. | Withdrawal (Dr.) |
    Deposit (Cr.) | Balance
  - DD Mon YYYY dates (e.g. "03 Jun 2026")
  - Indian-grouped amounts (e.g. "1,00,000.00"); Dr and Cr are SEPARATE
    columns (no Cr/Dr suffix on the amount itself)
  - An "Opening Balance" pseudo-row: no #, no Date, only a Balance
  - A page break with NO repeated header row on the continuation page
    (two separate Table flowables, only the first carries a header)
  - Two "Sweep transfer to/from ..." rows -- real transactions (Kotak's
    auto-sweep to/from a linked FD), which must NOT be filtered out
  - A trailing abbreviation LEGEND table (14 rows, 2 columns) that
    ``extract_tables()`` also recovers as a real table -- it must be
    excluded from parsed transaction rows because it has the wrong column
    count (2, not 7), not because of a content blocklist
"""
from __future__ import annotations

import csv
import io

SYN_ACCOUNT_NUMBER = "9988776655"
SYN_PERIOD_FROM = "2026-06-01"
SYN_PERIOD_TO = "2026-06-30"
SYN_PERIOD_FROM_DISPLAY = "01 Jun 2026"
SYN_PERIOD_TO_DISPLAY = "30 Jun 2026"

SYN_OPENING_BALANCE = 200000.00
SYN_CLOSING_BALANCE = 239000.00

# (date_display, description, chq_ref, withdrawal, deposit, balance)
# -- amounts are Indian-grouped strings exactly as they'd render on the
# statement; balance is a running balance so canonical_io's balance check
# must pass over this sequence unmodified.
SYN_TRANSACTIONS = [
    ("03 Jun 2026", "NEFT SALARY CREDIT-SYNCO",          "",       "",           "50,000.00",   "2,50,000.00"),
    ("05 Jun 2026", "UPI-SYNMART GROCERY-SYN",             "",       "3,000.00",   "",            "2,47,000.00"),
    ("09 Jun 2026", "Sweep transfer to FD-SYN0001",        "",       "1,00,000.00", "",           "1,47,000.00"),
    ("15 Jun 2026", "ACH D-SYNTH MF-SIP0001",              "",       "5,000.00",   "",            "1,42,000.00"),
    ("20 Jun 2026", "Sweep transfer from FD-SYN0001",      "",       "",           "1,00,000.00", "2,42,000.00"),
    ("24 Jun 2026", "UPI-REFUND ORDER-SYNSHOP",            "",       "",           "500.00",      "2,42,500.00"),
    ("28 Jun 2026", "CC AUTOPAY SI-SYN",                   "654321", "3,500.00",   "",            "2,39,000.00"),
]

# Rows that land on page 1 (WITH the header) vs. the continuation page
# (NO repeated header) -- exercises the multi-page-no-repeated-header quirk.
_PAGE1_TXN_COUNT = 4

HEADER_ROW = ["#", "Date", "Description", "Chq/Ref. No.", "Withdrawal (Dr.)", "Deposit (Cr.)", "Balance"]

# The trailing abbreviation legend -- 14 rows, 2 columns. NOT transaction
# data; must never appear in parsed output.
LEGEND_ROWS = [
    ["MB", "Transaction done on Mobile Banking"],
    ["NEFT", "National Electronic Funds Transfer"],
    ["RTGS", "Real Time Gross Settlement"],
    ["IMPS", "Immediate Payment Service"],
    ["UPI", "Unified Payments Interface"],
    ["ACH", "Automated Clearing House debit"],
    ["CLG", "Clearing"],
    ["CMS", "Cash Management Service"],
    ["POS", "Point of Sale transaction"],
    ["ATM", "Automated Teller Machine withdrawal"],
    ["CHQ", "Cheque"],
    ["SI", "Standing Instruction"],
    ["FD", "Fixed Deposit"],
    ["TXN", "Transaction"],
]


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


# ---------------------------------------------------------------------------
# Shape 1: synthetic PDF (bordered tables, multi-page, legend page, password
# optional).
# ---------------------------------------------------------------------------

def build_pdf(password: str | None = None) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (
        BaseDocTemplate, Frame, NextPageTemplate, PageTemplate,
        Paragraph, PageBreak, Spacer, Table, TableStyle,
    )

    buf = io.BytesIO()
    styles = getSampleStyleSheet()

    encrypt = StandardEncryption(password, canPrint=1, canModify=0) if password else None
    doc = BaseDocTemplate(
        buf, pagesize=A4,
        topMargin=40, bottomMargin=40, leftMargin=40, rightMargin=40,
        encrypt=encrypt,
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="main")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame])])

    grid_style = TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ])

    story = []
    story.append(Paragraph("KOTAK MAHINDRA BANK", styles["Title"]))
    story.append(Paragraph("Savings Account Statement", styles["Normal"]))
    story.append(Paragraph(f"Account No : {SYN_ACCOUNT_NUMBER}", styles["Normal"]))
    story.append(Paragraph(
        f"Statement Period : {SYN_PERIOD_FROM_DISPLAY} to {SYN_PERIOD_TO_DISPLAY}",
        styles["Normal"],
    ))
    story.append(Spacer(1, 14))

    # Page 1: header row + Opening Balance pseudo-row + first N transactions.
    page1_rows = [HEADER_ROW]
    page1_rows.append(["", "", "Opening Balance", "", "", "", _indian_group(f"{SYN_OPENING_BALANCE:.2f}")])
    for i, (date_s, desc, chq, wdl, dep, bal) in enumerate(SYN_TRANSACTIONS[:_PAGE1_TXN_COUNT], 1):
        page1_rows.append([str(i), date_s, desc, chq, wdl, dep, bal])
    t1 = Table(page1_rows, repeatRows=0)
    t1.setStyle(grid_style)
    story.append(t1)
    story.append(PageBreak())

    # Page 2: remaining transactions, NO header row repeated.
    page2_rows = []
    for i, (date_s, desc, chq, wdl, dep, bal) in enumerate(
        SYN_TRANSACTIONS[_PAGE1_TXN_COUNT:], _PAGE1_TXN_COUNT + 1
    ):
        page2_rows.append([str(i), date_s, desc, chq, wdl, dep, bal])
    t2 = Table(page2_rows, repeatRows=0)
    t2.setStyle(grid_style)
    story.append(t2)
    story.append(PageBreak())

    # Page 3: trailing abbreviation legend -- 2-column table, NOT transaction
    # data. extract_tables() will recover it as a real ~14-row table.
    story.append(Paragraph("Abbreviations used in this statement", styles["Heading3"]))
    story.append(Spacer(1, 6))
    legend_table_rows = [["Code", "Meaning"]] + LEGEND_ROWS
    t3 = Table(legend_table_rows, repeatRows=0)
    t3.setStyle(grid_style)
    story.append(t3)

    doc.build(story)
    return buf.getvalue()


def build_garbled_pdf() -> bytes:
    """A PDF whose text layer has no structural anchors -- simulates a
    scanned/custom-font statement that text_layer_usable() must reject."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 9)
    y = A4[1] - 50
    for i in range(20):
        c.drawString(40, y, f"{i} 12345.00 67890.00")
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shape 2: canonical-shaped CSV rendering of the SAME statement, used by the
# Part 2 generic "Other Bank (CSV)" cross-check -- a plausible raw CSV a
# Kotak net-banking CSV export might look like (its own header vocabulary,
# not skill_kotak's canonical schema).
# ---------------------------------------------------------------------------

def build_raw_csv_text() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Sl No", "Transaction Date", "Description", "Chq/Ref No", "Withdrawal (Dr.)", "Deposit (Cr.)", "Balance"])
    writer.writerow(["", "", "Opening Balance", "", "", "", _indian_group(f"{SYN_OPENING_BALANCE:.2f}")])
    for i, (date_s, desc, chq, wdl, dep, bal) in enumerate(SYN_TRANSACTIONS, 1):
        writer.writerow([str(i), date_s, desc, chq, wdl, dep, bal])
    return buf.getvalue()


if __name__ == "__main__":
    import pathlib
    out_dir = pathlib.Path(__file__).parent / "fixtures"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "syn_kotak.pdf").write_bytes(build_pdf())
    (out_dir / "syn_kotak_encrypted.pdf").write_bytes(build_pdf(password="SYNPWD1"))
    (out_dir / "syn_kotak_raw.csv").write_text(build_raw_csv_text(), encoding="utf-8")
    print("Wrote fixtures to", out_dir)
