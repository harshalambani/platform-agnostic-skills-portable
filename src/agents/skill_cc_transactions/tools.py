"""
tools.py - LangChain tools for the Create CC Transaction List skill.
"""
import subprocess
import sys
from pathlib import Path
from langchain_core.tools import tool

SCRIPT = Path(__file__).parent / "scripts" / "create_cc_transaction_list.py"


@tool
def extract_cc_transactions(pdf_dir: str, output_excel: str) -> str:
    """
    Extract credit card transactions from organized bank statement PDFs into Excel.

    Scans Bank-CardType/ subfolders in pdf_dir, applies bank-specific parsing
    (SBM, YES Bank, HDFC, Axis, ICICI, HSBC), extracts all transactions, and writes
    a consolidated Excel workbook with a Transactions sheet and a Summary sheet.

    Args:
        pdf_dir:      Path to folder containing Bank-CardType/ subfolders with PDFs.
                      Typically the Decrypted_PDFs_Correct/ folder from the sort skill.
        output_excel: Full path for the output .xlsx file.

    Returns a summary string (transaction count, breakdown by bank) or an error message.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), pdf_dir, output_excel],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"ERROR: {result.stderr.strip()}"
    return result.stdout.strip() or "Extraction complete."


@tool
def check_pdftotext_available() -> str:
    """
    Check whether pdftotext (from poppler-utils) is installed and available on PATH.
    pdftotext is required for PDF text extraction. Call this if extraction produces no results.
    Returns 'OK: pdftotext <version>' or installation instructions.
    """
    result = subprocess.run(["pdftotext", "-v"], capture_output=True, text=True)
    # pdftotext prints version to stderr
    output = result.stderr.strip() or result.stdout.strip()
    if result.returncode == 0 or "pdftotext" in output.lower():
        return f"OK: {output}"
    return (
        "pdftotext not found. Install poppler-utils:\n"
        "  Windows: download from https://github.com/oschwartz10612/poppler-windows\n"
        "           Extract and add the bin/ folder to your PATH\n"
        "  macOS:   brew install poppler\n"
        "  Linux:   apt-get install poppler-utils"
    )
