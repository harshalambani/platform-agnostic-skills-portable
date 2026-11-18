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

from agents.secret_redact import redact_text

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
    # The password goes to the script on stdin, never in its argv.
    args = [sys.executable, str(SCRIPT), pdf_path, output_path]
    if password:
        args.append("--password-stdin")
    result = subprocess.run(
        args, input=(password + "\n") if password else None,
        capture_output=True, text=True,
    )
    out = redact_text((result.stdout or "").strip(), [password])
    err = redact_text((result.stderr or "").strip(), [password])
    if result.returncode == 1:
        return f"ERROR: {err or out}"
    if result.returncode == 2:
        tail = f"\n{err}" if err else ""
        return f"WARNING (closing balance did not reconcile):\n{out}{tail}"
    return out or "Simplification complete."
