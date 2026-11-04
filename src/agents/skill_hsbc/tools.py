"""
tools.py — LangChain tools for the HSBC skill.
Two tools: full pipeline and a fast skip-OCR re-run.
"""
import subprocess
import sys
from pathlib import Path
from langchain_core.tools import tool

PIPELINE = Path(__file__).parent / "scripts" / "run_pipeline.py"


@tool
def run_hsbc_pipeline(pdf_dir: str, work_dir: str, output_path: str, title: str) -> str:
    """
    Run the full 4-stage HSBC pipeline: OCR → parse → enrich → build Excel.
    Use this on the first run, or when new PDFs are added.

    Args:
        pdf_dir:     Directory containing one or more HSBC PDF statements.
        work_dir:    Scratch directory for intermediate files (TSVs, JSON).
        output_path: Full path for the output .xlsx file.
        title:       Workbook title (e.g. 'HSBC Savings Apr2025-Mar2026').

    Returns a summary string or an error message.
    """
    result = subprocess.run(
        [
            sys.executable, str(PIPELINE),
            "--pdf-dir", pdf_dir,
            "--work-dir", work_dir,
            "--out", output_path,
            "--title", title,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"ERROR: {result.stderr.strip()}"
    return result.stdout.strip() or "Pipeline complete."


@tool
def skip_ocr_pipeline(work_dir: str, output_path: str, title: str) -> str:
    """
    Re-run stages 2–4 only (parse → enrich → build Excel), skipping OCR.
    Use this when OCR has already run and you want to iterate on parsing or enrichment.

    Args:
        work_dir:    Scratch directory containing existing TSV files from a prior OCR run.
        output_path: Full path for the output .xlsx file.
        title:       Workbook title.

    Returns a summary string or an error message.
    """
    result = subprocess.run(
        [
            sys.executable, str(PIPELINE),
            "--work-dir", work_dir,
            "--out", output_path,
            "--title", title,
            "--skip-ocr",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"ERROR: {result.stderr.strip()}"
    return result.stdout.strip() or "Pipeline (skip-OCR) complete."
