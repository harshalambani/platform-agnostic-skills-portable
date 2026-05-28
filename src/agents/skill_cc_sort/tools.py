"""
tools.py - LangChain tools for the Extract and Sort CC PDFs skill (v3.0).
"""
import subprocess
import sys
from pathlib import Path
from langchain_core.tools import tool

SCRIPT = Path(__file__).parent / "scripts" / "extract_sort_cc_pdfs.py"


@tool
def sort_cc_pdfs(
    input_folder: str,
    output_folder: str,
    password: str = "",
    folder_structure: str = "",
) -> str:
    """
    Extract PDFs from MSG emails and/or decrypt and organize credit card PDFs by bank.

    v3.0 capabilities:
    - Auto-detects input type: MSG email files, PDF files, or mixed
    - Supports MIME and CDFV2 binary Outlook MSG formats (requires extract-msg for CDFV2)
    - Decrypts password-protected PDFs using qpdf
    - Identifies bank/card type from filenames (Axis-Flipkart, Axis-IndianOil, HDFC-Regalia,
      HDFC-TataNeu, YES-Bank, SBM-Global, ICICI-Amazon, ICICI-Sapphiro)
    - Removes duplicates by MD5 hash; archives to Duplicates_Backup/
    - Generates verification_checksums.json

    Args:
        input_folder:     Folder containing MSG files and/or PDFs.
        output_folder:    Folder where organized output will be created.
        password:         Decryption password(s). Options:
                          - Single password: e.g. "HARS2806"
                          - Multiple passwords: comma-separated, e.g. "HARS2806,HAR28061,INABM123"
                          - Leave empty to auto-detect from input folder:
                              * passwords.txt with one password per line (preferred for multiple)
                              * A .txt file whose filename stem is the password (single password)
        folder_structure: Optional intermediate grouping when extracting from MSG files.
                          Values: 'by_date' | 'by_sender' | 'by_subject' | '' (none).

    Returns a summary string with counts (MSG files, PDFs extracted, decrypted,
    duplicates, bank breakdown), or an error message prefixed with ERROR:.
    """
    cmd = [sys.executable, str(SCRIPT), input_folder, output_folder]
    if password:
        cmd.append(password)
    if folder_structure == "by_date":
        cmd.append("--folder-by-date")
    elif folder_structure == "by_sender":
        cmd.append("--folder-by-sender")
    elif folder_structure == "by_subject":
        cmd.append("--folder-by-subject")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return f"ERROR: {result.stderr.strip()}"
    return result.stdout.strip() or "Sort complete."


@tool
def check_qpdf_available() -> str:
    """
    Check whether qpdf is installed and available on PATH.
    qpdf is required for PDF decryption. Call this first if decryption fails.
    Returns 'OK: qpdf <version>' or installation instructions.
    """
    result = subprocess.run(["qpdf", "--version"], capture_output=True, text=True)
    if result.returncode == 0:
        return f"OK: {result.stdout.strip()}"
    return (
        "qpdf not found. Install it:\n"
        "  Windows: choco install qpdf   OR   scoop install qpdf\n"
        "           OR download from https://github.com/qpdf/qpdf/releases\n"
        "  macOS:   brew install qpdf\n"
        "  Linux:   apt-get install qpdf"
    )


@tool
def check_extract_msg_available() -> str:
    """
    Check whether the extract-msg Python library is installed.
    extract-msg is required for CDFV2 binary Outlook MSG files (the default format
    when emails are saved directly from Outlook). MIME-format MSG files do not need it.
    Returns 'OK: extract-msg <version>' or installation instructions.
    """
    # Direct import check — the old subprocess approach used
    # `sys.executable -c "import ..."` which breaks in frozen mode
    # because sys.executable is pa_skills.exe, not python.exe.
    try:
        import extract_msg
        version = getattr(extract_msg, "__version__", "unknown")
        return f"OK: extract-msg {version}"
    except ImportError:
        return (
            "extract-msg not installed. Required for CDFV2 binary Outlook MSG files.\n"
            "Install with: pip install extract-msg\n"
            "Note: MIME-format MSG files work without this library."
        )
