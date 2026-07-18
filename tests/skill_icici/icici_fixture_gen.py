"""
tests/skill_icici/icici_fixture_gen.py -- synthetic ICICI statement fixture
for the P2 golden-family test. ICICI only ever emits one input shape (the
.xls net-banking download; xlrd cannot even read .xlsx), so there is no
cross-format identity to prove -- instead this builds a single synthetic
.xls encoding 5 fake transactions in ICICI's real preamble/header/data-row
layout, for a byte-identical-output regression test. No real account data
appears here.
"""
from __future__ import annotations

import io

SYN_ACCOUNT_NUMBER = "000000000001"
SYN_PERIOD_FROM_RAW = "01,Apr,2025"
SYN_PERIOD_TO_RAW = "30,Apr,2025"
SYN_PERIOD_FROM = "2025-04-01"
SYN_PERIOD_TO = "2025-04-30"

SYN_OPENING_BALANCE = 100000.00
SYN_CLOSING_BALANCE = 138500.00

# (value_date, txn_date, cheque, remarks, withdrawal, deposit, balance)
SYN_TRANSACTIONS = [
    ("01,Apr,2025", "01,Apr,2025", "-", "NEFT-REF0000000001-SYN SALARY CREDIT", "", "50000.00", "150000.00"),
    ("02,Apr,2025", "02,Apr,2025", "-", "UPI/300000000002/NA/synshop//SYNabc0000ef", "2000.00", "", "148000.00"),
    ("03,Apr,2025", "03,Apr,2025", "-", "BIL/ONL/REF0000000003/SYN UTILITY BILL", "5000.00", "", "143000.00"),
    ("04,Apr,2025", "04,Apr,2025", "-", "UPI/300000000004/NA/synrefund//SYNdef0001gh", "", "500.00", "143500.00"),
    ("05,Apr,2025", "05,Apr,2025", "654321", "CLG/SYN CHEQUE DEPOSIT", "5000.00", "", "138500.00"),
]

_HEADER_ROW = [
    "", "S No.", "Value Date", "Transaction Date", "Cheque Number",
    "Transaction Remarks", "Withdrawal Amount (INR )", "Deposit Amount (INR )",
    "Balance (INR )",
]


def build_xls() -> bytes:
    """Synthetic ICICI .xls (BIFF) encoding SYN_TRANSACTIONS, matching the
    real 12-row preamble + header-row-13 + data-from-row-14 layout."""
    import xlwt

    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")

    # Rows 0-11: preamble (12 rows). Only rows 3 (Account Number) and 4
    # (Transaction Date from ... to ...) are meaningful for meta extraction;
    # the rest are filler labels matching the real front-matter shape.
    ws.write(0, 1, "DETAILED STATEMENT")
    ws.write(1, 1, "Search")
    ws.write(2, 1, "")
    ws.write(3, 1, "Account Number")
    ws.write(3, 3, f"{SYN_ACCOUNT_NUMBER}(INR)  - SYNTHETIC BRANCH")
    ws.write(4, 1, "Transaction Date from")
    ws.write(4, 3, SYN_PERIOD_FROM_RAW)
    ws.write(4, 4, "to")
    ws.write(4, 5, SYN_PERIOD_TO_RAW)
    ws.write(5, 1, "Transaction Period")
    ws.write(6, 1, "Advanced Search")
    ws.write(7, 1, "Amount from")
    ws.write(8, 1, "Cheque number from")
    ws.write(9, 1, "Transaction remarks")
    ws.write(10, 1, "Transaction type")
    ws.write(11, 1, "Transactions List -")
    ws.write(11, 8, "")  # widen sheet to 9 columns (0-8) to match real ncols

    # Row 12: data header.
    for col, val in enumerate(_HEADER_ROW):
        ws.write(12, col, val)

    # Rows 13+: 5 synthetic data rows.
    for i, (vdate, tdate, cheque, remarks, wdl, dep, bal) in enumerate(SYN_TRANSACTIONS, start=1):
        r = 12 + i
        ws.write(r, 1, str(i))
        ws.write(r, 2, vdate)
        ws.write(r, 3, tdate)
        ws.write(r, 4, cheque)
        ws.write(r, 5, remarks)
        ws.write(r, 6, wdl)
        ws.write(r, 7, dep)
        ws.write(r, 8, bal)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Parameterized builder for multi-statement consolidation tests (P3b): lets a
# caller supply its own transaction list/period so several single-month
# statements can be combined into a synthetic multi-file batch.
# ---------------------------------------------------------------------------

def build_xls_for(
    transactions: list[tuple[str, str, str, str, str, str, str]],
    period_from_raw: str,
    period_to_raw: str,
    account_number: str = SYN_ACCOUNT_NUMBER,
) -> bytes:
    """Build a synthetic ICICI .xls for an arbitrary transaction list (same
    7-tuple shape as SYN_TRANSACTIONS), with its own period."""
    import xlwt

    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")

    ws.write(0, 1, "DETAILED STATEMENT")
    ws.write(1, 1, "Search")
    ws.write(2, 1, "")
    ws.write(3, 1, "Account Number")
    ws.write(3, 3, f"{account_number}(INR)  - SYNTHETIC BRANCH")
    ws.write(4, 1, "Transaction Date from")
    ws.write(4, 3, period_from_raw)
    ws.write(4, 4, "to")
    ws.write(4, 5, period_to_raw)
    ws.write(5, 1, "Transaction Period")
    ws.write(6, 1, "Advanced Search")
    ws.write(7, 1, "Amount from")
    ws.write(8, 1, "Cheque number from")
    ws.write(9, 1, "Transaction remarks")
    ws.write(10, 1, "Transaction type")
    ws.write(11, 1, "Transactions List -")
    ws.write(11, 8, "")

    for col, val in enumerate(_HEADER_ROW):
        ws.write(12, col, val)

    for i, (vdate, tdate, cheque, remarks, wdl, dep, bal) in enumerate(transactions, start=1):
        r = 12 + i
        ws.write(r, 1, str(i))
        ws.write(r, 2, vdate)
        ws.write(r, 3, tdate)
        ws.write(r, 4, cheque)
        ws.write(r, 5, remarks)
        ws.write(r, 6, wdl)
        ws.write(r, 7, dep)
        ws.write(r, 8, bal)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


if __name__ == "__main__":
    import pathlib
    out_dir = pathlib.Path(__file__).parent / "fixtures"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "syn_icici.xls").write_bytes(build_xls())
    print("Wrote fixtures to", out_dir)
