"""
canonical_io.py — The shared IO tail for every bank parser.

Banks differ only in how they *read* their native statement. Everything that
happens *after* a statement has become canonical rows is identical across banks:
write the 8-column CSV, derive opening/closing balances, run the running-balance
check, and emit the ``*.csv_summary.json`` sidecar the GnuCash pipeline reads.

Centralising that tail here removes the per-adapter duplication (the same
``csv.DictWriter`` block previously lived in ``adapter_bob`` and ``adapter_hsbc``,
and the sidecar logic in ``skill_gnucash_pipeline``).

Row-level math is delegated to ``balance_utils`` — this module owns IO and the
canonical schema, not the arithmetic.
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from agents import balance_utils
from agents.bank_contract import BalanceCheck

log = logging.getLogger(__name__)

# The canonical 8-column schema, in order. Single source of truth — adapters and
# skills import this instead of repeating the literal column list.
CANONICAL_FIELDS: tuple[str, ...] = (
    "Date",
    "Transaction ID",
    "Description",
    "Account",
    "Deposit",
    "Withdrawal",
    "Balance",
    "Currency",
)

# Suffix of the per-statement summary sidecar written next to a canonical CSV.
SIDECAR_SUFFIX = ".csv_summary.json"


def write_canonical_csv(rows: list[dict], output_path: str | Path) -> Path:
    """Write canonical rows to ``output_path`` as an 8-column CSV.

    Behaviour is byte-for-byte identical to the inline writers it replaces:
    parent dirs are created, the file is opened with ``newline=""`` and UTF-8,
    and a ``csv.DictWriter`` over :data:`CANONICAL_FIELDS` writes the header then
    the rows.

    Returns the path written.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(CANONICAL_FIELDS))
        writer.writeheader()
        writer.writerows(rows)
    return output_path


def derive_opening_closing(rows: list[dict]) -> dict:
    """Derive opening/closing balances + row count from canonical rows.

    Thin pass-through to ``balance_utils.extract_opening_closing`` so callers
    have a single import surface for the shared tail.
    """
    return balance_utils.extract_opening_closing(rows)


def run_balance_check(rows: list[dict]) -> BalanceCheck:
    """Run the running-balance verification and return a typed BalanceCheck."""
    return BalanceCheck.from_running(balance_utils.verify_running_balance(rows))


def write_sidecar(
    canonical_path: str | Path,
    bank: str,
    source: str,
    opening: float,
    closing: float,
    row_count: int,
) -> Path | None:
    """Write a ``*.csv_summary.json`` sidecar next to the canonical CSV.

    The GnuCash pipeline reads this at final-verification time to obtain an
    explicit expected closing balance instead of the tautological last-row value.
    ``source`` is "statement_summary" when the figure comes from the statement
    itself, or "derived" when computed from the rows.

    Returns the sidecar path on success, or None if it could not be written.
    """
    sidecar = Path(canonical_path).with_suffix(SIDECAR_SUFFIX)
    try:
        data = {
            "bank": bank,
            "source": source,
            "opening_balance": opening,
            "closing_balance": closing,
            "row_count": row_count,
        }
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.info("Wrote sidecar summary: %s", sidecar)
        return sidecar
    except Exception as e:
        log.warning("Could not write sidecar summary: %s", e)
        return None


def read_sidecar(canonical_path: str | Path) -> dict | None:
    """Read the ``*.csv_summary.json`` sidecar next to ``canonical_path``."""
    sidecar = Path(canonical_path).with_suffix(SIDECAR_SUFFIX)
    if sidecar.is_file():
        try:
            with open(sidecar, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning("Could not read sidecar summary: %s", e)
    return None
