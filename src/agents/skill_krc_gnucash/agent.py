"""
agent.py — KR Choksey -> GnuCash (Part III) — DIRECT mode, no LLM.

Deterministically turns the Part II "Bills" sheet into importable GnuCash
multi-split CSVs (Purchase.csv, SLBM.csv, Sale.csv, and Charges.csv for demat
charges once an expense account is chosen) by invoking
build_krc_gnucash.py. FIFO cost basis + LTCG/STCG come from the supplied
.gnucash book; the holding-period threshold and account paths are read from
Data/settings/krc_gnucash_config.yaml (editable; see the Usage Guide).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..outputs import ReplyWithOutputs, extra_output

SCRIPT = Path(__file__).parent / "scripts" / "build_krc_gnucash.py"


def run(
    bills_xlsx: str,
    gnucash_path: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
    entity: str = "",
    demat_expense_account: str = "",
) -> str:
    """
    Build Purchase/SLBM/Sale GnuCash import CSVs from a Part II Bills workbook
    and a .gnucash book, writing them into the output_path folder. Returns the
    run summary. Direct mode — no LLM. config_path / model_override (the
    framework's LLM settings) are accepted for interface compatibility and
    ignored; this skill reads its own config from Data/settings/.
    """
    cmd = [sys.executable, str(SCRIPT), bills_xlsx, gnucash_path, output_path]
    if entity:
        cmd += ["--entity", str(entity)]
    if demat_expense_account:
        cmd += ["--demat-account", str(demat_expense_account)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if result.returncode == 1:
        return f"ERROR: {err or out}"
    # UI-28: one download button per file THIS run wrote (never a folder scan
    # across runs; only files that exist in this run's own folder).
    outdir = Path(output_path)
    extras = tuple(
        extra_output(key, outdir / fname if (outdir / fname).is_file() else None,
                     "Not written by this run.")
        for key, fname in _FILES
    )
    if result.returncode == 2:
        tail = f"\n\n{err}" if err else ""
        return ReplyWithOutputs(f"Completed with items to review:\n\n{out}{tail}", extras)
    return ReplyWithOutputs(out or "GnuCash CSVs generated.", extras)


_FILES = (
    ("purchase_csv", "Purchase.csv"),
    ("slbm_csv", "SLBM.csv"),
    ("sale_csv", "Sale.csv"),
    ("charges_csv", "Charges.csv"),
    ("new_securities_csv", "NewSecurities.csv"),
    ("review_csv", "Review.csv"),
)
