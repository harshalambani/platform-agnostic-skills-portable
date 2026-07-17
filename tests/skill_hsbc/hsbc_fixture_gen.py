"""
tests/skill_hsbc/hsbc_fixture_gen.py -- synthetic ENRICHED HSBC workbook
fixture for the P2 golden-family test.

HSBC's OCR stage is not byte-deterministic across environments, so unlike
HDFC/BoB/ICICI we don't golden-test PDF-in -> canonical byte-for-byte.
Instead this builds a workbook shaped exactly like the real output of
scripts/build_xlsx.py (same headers, same column order, a brought-forward
row, a balance-corrected row) -- i.e. it fixes the DETERMINISTIC stage
(enriched workbook -> canonical rows, in agent.py's _read_enriched_rows) as
a golden, independent of OCR accuracy. No real account data appears here.
"""
from __future__ import annotations

import io
from datetime import date

SYN_OPENING_BALANCE = 100000.00
SYN_CLOSING_BALANCE = 138500.00

_HEADERS = [
    "Date", "Transaction Details", "Transaction Date", "Transaction Number",
    "Extra Information", "Deposit", "Withdrawals", "Balance",
]

# (date, details, txn_date, txn_no, extra_info, deposit, withdrawal, balance)
SYN_ROWS = [
    (date(2025, 4, 1), "BALANCE BROUGHT FORWARD", None, "", "", None, None, 100000.00),
    (date(2025, 4, 1), "SYN SALARY CREDIT", date(2025, 4, 1), "UPI3000000001", "",
     50000.00, None, 150000.00),
    (date(2025, 4, 2), "synshop purchase", date(2025, 4, 2), "", "",
     None, 2000.00, 148000.00),
    (date(2025, 4, 3), "SYN UTILITY BILL", date(2025, 4, 3), "NEFTSYN00000003", "",
     None, 5000.00, 143000.00),
    (date(2025, 4, 4), "SYN REFUND", date(2025, 4, 4), "IMPS300000004", "ELECTRO 12:30:00",
     500.00, None, 143500.00),
    (date(2025, 4, 5), "SYN CHEQUE DEPOSIT", date(2025, 4, 5), "654321", "",
     None, 5000.00, 138500.00),
]


def build_xlsx() -> bytes:
    """Synthetic enriched HSBC .xlsx, matching build_xlsx.py's real column
    layout: Date | Transaction Details | Transaction Date | Transaction
    Number | Extra Information | Deposit | Withdrawals | Balance."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "HSBC Savings Apr2025"
    ws.append(_HEADERS)
    for row in SYN_ROWS:
        ws.append(list(row))

    ws2 = wb.create_sheet("Summary")
    ws2.append(["Metric", "Value"])
    ws2.append(["Period", "2025-04-01 to 2025-04-05"])
    ws2.append(["Opening balance", SYN_OPENING_BALANCE])
    ws2.append(["Closing balance", SYN_CLOSING_BALANCE])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


if __name__ == "__main__":
    import pathlib
    out_dir = pathlib.Path(__file__).parent / "fixtures"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "syn_hsbc_enriched.xlsx").write_bytes(build_xlsx())
    print("Wrote fixtures to", out_dir)
