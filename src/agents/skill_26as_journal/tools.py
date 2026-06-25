"""
tools.py — LangChain tools for the 26AS TDS Journal skill.

Designed to be robust on small local models: the deterministic builder always
writes a valid, balanced CSV (unmatched deductors go to Suspense), and each
tool VERIFIES its own output, so the model never has to call a separate verify
step with a path it might garble. The only optional LLM step is choosing
accounts for the NEEDS REVIEW deductors — passed as a plain object.

Flow: build_tds_journals -> (optionally) apply_journal_overrides.
"""
import ast
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


def _verify(csv_path: str) -> str:
    """Balance check used internally by build/apply. Every transaction's signed
    Amount splits must sum to zero and no split may have a blank Account."""
    p = Path(csv_path)
    if not p.is_file():
        return f"NOTE: could not re-open {p.name} to verify (it was still saved)."
    groups: dict = {}
    order: list = []
    with p.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            acct = (row.get("Account") or "").strip()
            amt = float(row["Amount"]) if (row.get("Amount") or "").strip() else 0.0
            key = (row.get("Transaction ID") or "").strip() or (
                (row.get("Date") or "") + "|" + (row.get("Description") or ""))
            if key not in groups:
                groups[key] = [0.0, False]
                order.append(key)
            groups[key][0] += amt
            if not acct:
                groups[key][1] = True
    problems = []
    for key in order:
        total, blank = groups[key]
        if abs(total) >= 0.01:
            problems.append(f"{key}: does not balance (sum {total:.2f})")
        if blank:
            problems.append(f"{key}: a split has a blank Account")
    if problems:
        return "VERIFY PROBLEMS:\n" + "\n".join(problems)
    return f"VERIFIED — {len(order)} transactions, all balanced."


def _normalize_overrides(overrides) -> "dict | str":
    """Accept the overrides as a dict (object) OR a JSON / python-dict string,
    and return a {str(sr): account_path} dict, or an error string."""
    if isinstance(overrides, str):
        s = overrides.strip()
        if not s:
            return {}
        try:
            overrides = json.loads(s)
        except Exception:
            try:
                overrides = ast.literal_eval(s)
            except Exception:
                return ('ERROR: overrides must be an object like '
                        '{"2": "Income:Interest Income:Interest on HDFC - FD"}.')
    if not isinstance(overrides, dict):
        return 'ERROR: overrides must be an object {sr: account_path}.'
    return {str(k): v for k, v in overrides.items() if v}


@tool
def build_tds_journals(xlsx_path: str, gnucash_path: str, output_path: str) -> str:
    """
    Build GnuCash TDS journal entries from a 26AS Convert workbook and a
    .gnucash file. Writes the journal CSV to output_path plus a *-review.csv
    sidecar, and verifies the result. Returns a per-deductor summary (matched
    credit accounts, confidence, any NEEDS REVIEW deductors with their candidate
    accounts, accounts to create) followed by the verification result. This call
    alone produces a complete, valid CSV. Call it first.
    """
    out = _run_script([xlsx_path, gnucash_path, output_path])
    if out.startswith("ERROR"):
        return out
    return out + "\n\n" + _verify(output_path)


@tool
def apply_journal_overrides(xlsx_path: str, gnucash_path: str, output_path: str,
                            overrides: dict | str) -> str:
    """
    Optional. Re-build the journals applying credit-account choices for the
    deductors the matcher flagged NEEDS REVIEW. Pass `overrides` as an OBJECT
    mapping the deductor Sr.No to the chosen full account path, e.g.
    overrides={"2": "Income:Interest Income:Interest on HDFC - FD"}. Include only
    flagged Sr numbers; leave others on Suspense. Rewrites + re-verifies the CSV.
    If you are unsure, do NOT call this — the CSV from build_tds_journals is
    already valid with those deductors on Suspense.
    """
    norm = _normalize_overrides(overrides)
    if isinstance(norm, str):       # error message
        return norm
    if not norm:
        return "No overrides supplied; the existing CSV is unchanged and valid."
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(norm, f)
        ov_path = f.name
    out = _run_script([xlsx_path, gnucash_path, output_path, ov_path])
    if out.startswith("ERROR"):
        return out
    return out + "\n\n" + _verify(output_path)
