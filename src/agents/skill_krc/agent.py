"""
agent.py — KR Choksey ledger-simplification (Part I) — DIRECT mode, no LLM.

Calls the deterministic parser parse_krc_ledger.py and returns its summary.
No language model is involved, so the Simplified Ledger workbook (with the
References sheet) is always written to output_path; runs are reproducible
and work fully offline.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "scripts" / "parse_krc_ledger.py"


def run(
    pdf_path: str,
    password: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Simplify a KR Choksey broker ledger statement PDF into an Excel workbook at
    output_path. Direct mode — no LLM. config_path / model_override are accepted
    for interface compatibility and ignored.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), pdf_path, password, output_path],
        capture_output=True, text=True,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode == 1:
        return f"ERROR: {err or out}"
    if result.returncode == 2:
        tail = f"\n{err}" if err else ""
        return f"WARNING (closing balance did not reconcile):\n{out}{tail}"
    return out or "Simplification complete."
