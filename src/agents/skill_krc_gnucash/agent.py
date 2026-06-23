"""
agent.py — KR Choksey -> GnuCash (Part III) — DIRECT mode, no LLM.

Deterministically turns the Part II "Bills" sheet into importable GnuCash
multi-split CSVs (Purchase.csv, SLBM.csv, Sale.csv) by invoking
build_krc_gnucash.py. FIFO cost basis + LTCG/STCG come from the supplied
.gnucash book; the holding-period threshold and account paths are read from
Data/settings/krc_gnucash_config.yaml (editable; see the Usage Guide).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "scripts" / "build_krc_gnucash.py"


def run(
    bills_xlsx: str,
    gnucash_path: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Build Purchase/SLBM/Sale GnuCash import CSVs from a Part II Bills workbook
    and a .gnucash book, writing them into the output_path folder. Returns the
    run summary. Direct mode — no LLM. config_path / model_override (the
    framework's LLM settings) are accepted for interface compatibility and
    ignored; this skill reads its own config from Data/settings/.
    """
    result = subprocess.run(
        [sys.executable, str(SCRIPT), bills_xlsx, gnucash_path, output_path],
        capture_output=True, text=True,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode == 1:
        return f"ERROR: {err or out}"
    if result.returncode == 2:
        tail = f"\n\n{err}" if err else ""
        return f"Completed with items to review:\n\n{out}{tail}"
    return out or "GnuCash CSVs generated."
