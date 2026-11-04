"""
agent.py — KR Choksey Bills reconciliation (Part II) — DIRECT mode, no LLM.

Deterministically reconciles a folder of KR Choksey contract notes against the
Part I "Simplified Ledger" workbook by invoking parse_krc_bills.py, and returns
the run summary. No language model is involved, so the output file is always
written to output_path and the UI's download button appears reliably.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "scripts" / "parse_krc_bills.py"


def run(
    cn_dir: str,
    ledger_xlsx: str,
    password: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Reconcile the contract notes in cn_dir against the Part I Simplified Ledger
    workbook (ledger_xlsx) and write a [Bills, Trade Lines, Reconciliation]
    workbook to output_path. Returns the run summary.

    config_path and model_override are accepted for interface compatibility
    with the generic runner and are ignored (this skill uses no LLM).
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), cn_dir, ledger_xlsx, password, output_path],
        capture_output=True, text=True,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode == 1:
        return f"ERROR: {err or out}"
    if result.returncode == 2:
        tail = f"\n\n{err}" if err else ""
        return ("Completed with warnings (unmatched bills or rows need review):\n\n"
                f"{out}{tail}")
    return out or "Reconciliation complete."
