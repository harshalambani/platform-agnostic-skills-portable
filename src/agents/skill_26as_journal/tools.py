"""
tools.py — LangChain tools for the 26AS TDS Journal skill.

The LLM orchestrates these; they delegate to the deterministic builder script.
Flow: build_tds_journals -> (optionally) apply_journal_overrides for any
deductor the matcher sent to Suspense -> verify_journal_csv.
"""
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from langchain_core.tools import tool

SCRIPT = Path(__file__).parent / "scripts" / "build_tds_journals.py"


def _run_script(args: list[str]) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"ERROR: {result.stderr.strip() or result.stdout.strip()}"
    return result.stdout.strip() or "Done."


@tool
def build_tds_journals(xlsx_path: str, gnucash_path: str, output_path: str) -> str:
    """
    Build GnuCash TDS journal entries from a 26AS Convert workbook and a
    .gnucash file. Writes the journal CSV to output_path and a *-review.csv
    sidecar next to it. Returns a per-deductor summary including matched
    credit accounts, confidence, any deductors that need review (with their
    candidate accounts), and any accounts you must create before import.
    Call this first.
    """
    return _run_script([xlsx_path, gnucash_path, output_path])


@tool
def apply_journal_overrides(xlsx_path: str, gnucash_path: str, output_path: str,
                            overrides_json: str) -> str:
    """
    Re-build the journals applying credit-account overrides for deductors the
    deterministic matcher could not resolve. overrides_json is a JSON object
    mapping the deductor Sr.No (as a string) to the chosen full account path,
    e.g. {"7": "Income:Interest Income:Interest on EPF Taxable"}. Only override
    deductors flagged NEEDS REVIEW; pick from their listed candidates, or leave
    them on Suspense if none fit. Rewrites the CSV and review sidecar.
    """
    try:
        overrides = json.loads(overrides_json)
        if not isinstance(overrides, dict):
            return "ERROR: overrides_json must be a JSON object {sr: account_path}."
    except Exception as e:
        return f"ERROR: could not parse overrides_json: {e}"

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(overrides, f)
        ov_path = f.name
    return _run_script([xlsx_path, gnucash_path, output_path, ov_path])


@tool
def verify_journal_csv(csv_path: str) -> str:
    """
    Verify the journal CSV: every transaction must balance (its signed Amount
    splits sum to zero) and no split may have a blank Account. Returns 'OK ...'
    if all transactions balance, else a list of problems. Call this last.
    """
    p = Path(csv_path)
    if not p.is_file():
        return f"ERROR: file not found: {csv_path}"
    # Group splits by Transaction ID (rows of one transaction share the same
    # ID; Date/Description are also repeated). Falls back to Date+Description.
    groups: dict = {}
    order: list = []
    with p.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            acct = (row.get("Account") or "").strip()
            amt = float(row["Amount"]) if (row.get("Amount") or "").strip() else 0.0
            key = (row.get("Transaction ID") or "").strip() or (
                (row.get("Date") or "") + "|" + (row.get("Description") or ""))
            if key not in groups:
                groups[key] = [key, 0.0, False]
                order.append(key)
            g = groups[key]
            g[1] += amt
            if not acct:
                g[2] = True

    problems = []
    for key in order:
        label, total, blank = groups[key]
        if abs(total) >= 0.01:
            problems.append(f"{label}: does not balance (splits sum to {total:.2f}, expected 0)")
        if blank:
            problems.append(f"{label}: a split has a blank Account")
    if problems:
        return "PROBLEMS:\n" + "\n".join(problems)
    return f"OK — {len(order)} transactions, all balanced."
