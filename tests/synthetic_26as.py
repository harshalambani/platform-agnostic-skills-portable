"""
tests/synthetic_26as.py -- a wholly synthetic Form 26AS, built at test time.

Nothing here is a real person, deductor or amount. The PAN is the public
test pattern ABCDE1234X; the TANs are made-up letter/digit patterns; the
deductors are invented names. The text mirrors the layout the extractor reads
from `pdftotext -layout` (assessee front matter, PART-I/II/VI headers, a
deductor header row and its transaction rows), so tests can feed it through
the real parser and the real workbook builder without a PDF or a real
statement ever being committed.
"""
from __future__ import annotations

from pathlib import Path

TEST_PAN = "ABCDE1234X"

SYNTHETIC_26AS_TEXT = f"""\
Annual Tax Statement (Form 26AS)
Data updated till 30-Jun-2025
Permanent Account Number (PAN)   {TEST_PAN}
Financial Year                   2024-25
Assessment Year                  2025-26
Name of Assessee                 TEST ASSESSEE ONE

PART-I Details of Tax Deducted at Source
Sr. No. Name of Deductor TAN of Deductor Total Amount Paid/Credited Total Tax Deducted Total TDS Deposited
Sr. No. Section Transaction Date Status of Booking Date of Booking Remarks Amount Paid/Credited Tax Deducted TDS Deposited
1   SAMPLE BANK ONE LIMITED   ABCD12345E   30,000.00   3,000.00   3,000.00
1   194A   10-Apr-2024   F   05-May-2024   -   10,000.00   1,000.00   1,000.00
2   194A   10-Jul-2024   F   05-Aug-2024   -   20,000.00   2,000.00   2,000.00
2   SAMPLE FINANCE TWO LIMITED   WXYZ98765K   50,000.00   5,000.00   5,000.00
1   194A   15-Oct-2024   F   05-Nov-2024   -   50,000.00   5,000.00   5,000.00

PART-II Details of Tax Deducted at Source for 15G / 15H
Sr. No. Name of Deductor TAN of Deductor Total Amount Paid/Credited Total Tax Deducted Total TDS Deposited
1   SAMPLE FINANCE THREE LIMITED   LMNO54321P   25,000.00   0.00   0.00
1   194A   10-Apr-2024   05-May-2024   -   25,000.00   0.00   0.00
"""


def patch_pdf_to_text(monkeypatch, module, text: str = SYNTHETIC_26AS_TEXT) -> None:
    """Make the extractor read `text` instead of running pdftotext."""
    monkeypatch.setattr(module, "pdf_to_text", lambda _p: (text, "/synthetic/pdftotext"))


def extract_synthetic_xlsx(module, monkeypatch, out_path: Path, text: str = SYNTHETIC_26AS_TEXT) -> dict:
    """Run the real extractor's `run()` on the synthetic text; returns its stats."""
    patch_pdf_to_text(monkeypatch, module, text)
    return module.run(Path("synthetic-26as.pdf"), out_path)


def load_extractor():
    """Load the real extractor script as a module (same way the 26AS tests do)."""
    import importlib.util
    import sys
    root = Path(__file__).resolve().parent.parent
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    script = src / "agents" / "skill_26as" / "scripts" / "extract_26as_to_xlsx.py"
    name = "extract_26as_to_xlsx_synthetic"
    spec = importlib.util.spec_from_file_location(name, script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def write_synthetic_book(path: Path) -> str:
    """A small synthetic GnuCash book (invented accounts, no transactions)."""
    import gnc_book_fixture as fx
    extra = [
        fx.account_xml("tdsr", "TDS Receivable", "ASSET", "ca"),
        fx.account_xml("intinc", "Interest Income", "INCOME", "inc"),
    ]
    return fx.write_book(path, fx.standard_accounts(extra))
