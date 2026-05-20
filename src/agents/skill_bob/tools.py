"""
tools.py — LangChain tools for the Bank of Baroda skill.
"""
import subprocess
import sys
from pathlib import Path
from langchain_core.tools import tool

SCRIPT = Path(__file__).parent / "scripts" / "extract_bob_statement.py"


@tool
def extract_bob(pdf_path: str, output_path: str) -> str:
    """
    Extract transactions from a Bank of Baroda statement PDF into a CSV file.
    Handles multi-page statements where column headers are not repeated on continuation pages.
    Returns a summary (row count, any warnings) or an error message.
    Do NOT use this for other banks — the column geometry is BoB-specific.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), pdf_path, output_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"ERROR: {result.stderr.strip()}"
    output = result.stdout.strip()
    return output or "Extraction complete. Please verify row count against the PDF."
