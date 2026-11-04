"""
balance_utils.py — Shared balance verification utilities for all bank skills.

Three checks:
  1. verify_running_balance()  — row-by-row: prev + deposit - withdrawal == current
  2. extract_opening_closing() — derive opening & closing balances from canonical rows
  3. verify_closing_balance()  — confirm CSV closing balance matches source statement

All functions work on canonical-schema rows (list of dicts with keys:
Date, Transaction ID, Description, Account, Deposit, Withdrawal, Balance, Currency).
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Rounding tolerance in currency units (2 paise for INR)
DEFAULT_TOLERANCE = 0.02


def _safe_float(value: Any) -> float:
    """Convert a value to float, handling empty strings, commas, None."""
    if value is None:
        return 0.0
    s = str(value).strip().replace(',', '')
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def extract_opening_closing(rows: list[dict]) -> dict:
    """
    Derive opening and closing balances from canonical rows.

    Opening balance = first_row_balance - first_row_deposit + first_row_withdrawal

    Returns:
        {
            "opening_balance": float,
            "closing_balance": float,
            "row_count": int,
        }
    """
    if not rows:
        return {"opening_balance": 0.0, "closing_balance": 0.0, "row_count": 0}

    first = rows[0]
    first_bal = _safe_float(first.get('Balance', 0))
    first_dep = _safe_float(first.get('Deposit', 0))
    first_wdl = _safe_float(first.get('Withdrawal', 0))
    opening = first_bal - first_dep + first_wdl

    last = rows[-1]
    closing = _safe_float(last.get('Balance', 0))

    return {
        "opening_balance": round(opening, 2),
        "closing_balance": round(closing, 2),
        "row_count": len(rows),
    }


def verify_running_balance(
    rows: list[dict],
    tolerance: float = DEFAULT_TOLERANCE,
    max_warnings: int = 10,
) -> dict:
    """
    Row-by-row running balance check.

    For each row i:
        expected = prev_balance + deposit_i - withdrawal_i
        actual   = balance_i
        mismatch if |expected - actual| > tolerance

    The opening balance is derived from row 0:
        opening = balance_0 - deposit_0 + withdrawal_0

    Returns:
        {
            "ok": bool,
            "mismatches": int,
            "details": [str, ...],     # up to max_warnings human-readable lines
            "opening_balance": float,
            "closing_balance": float,
        }
    """
    if not rows:
        return {
            "ok": True,
            "mismatches": 0,
            "details": [],
            "opening_balance": 0.0,
            "closing_balance": 0.0,
        }

    mismatches = 0
    details: list[str] = []

    for i, row in enumerate(rows):
        cur_bal = _safe_float(row.get('Balance', 0))
        cur_dep = _safe_float(row.get('Deposit', 0))
        cur_wdl = _safe_float(row.get('Withdrawal', 0))

        if i == 0:
            opening_balance = round(cur_bal - cur_dep + cur_wdl, 2)
            prev_bal = opening_balance
        else:
            prev_bal = _safe_float(rows[i - 1].get('Balance', 0))

        expected = round(prev_bal + cur_dep - cur_wdl, 2)
        diff = abs(expected - cur_bal)

        if diff > tolerance:
            mismatches += 1
            if mismatches <= max_warnings:
                date = row.get('Date', '?')
                desc = (row.get('Description', '') or '')[:40]
                details.append(
                    f"Row {i+1} ({date} {desc}): "
                    f"expected {expected:.2f}, got {cur_bal:.2f} "
                    f"(diff {diff:.2f})"
                )

    closing_balance = _safe_float(rows[-1].get('Balance', 0))

    if mismatches == 0:
        log.info("Running balance check: OK (%d rows)", len(rows))
    else:
        log.warning(
            "Running balance check: %d mismatch(es) in %d rows",
            mismatches, len(rows),
        )
        if mismatches > max_warnings:
            details.append(f"... and {mismatches - max_warnings} more")

    return {
        "ok": mismatches == 0,
        "mismatches": mismatches,
        "details": details,
        "opening_balance": opening_balance,
        "closing_balance": round(closing_balance, 2),
    }


def verify_closing_balance(
    rows: list[dict],
    expected_closing: float,
    tolerance: float = DEFAULT_TOLERANCE,
) -> dict:
    """
    Confirm that the last row's Balance matches the expected closing balance
    from the source statement.

    Returns:
        {
            "ok": bool,
            "csv_closing": float,
            "expected_closing": float,
            "diff": float,
            "message": str,
        }
    """
    if not rows:
        return {
            "ok": False,
            "csv_closing": 0.0,
            "expected_closing": expected_closing,
            "diff": abs(expected_closing),
            "message": "No rows in CSV — cannot verify closing balance.",
        }

    csv_closing = round(_safe_float(rows[-1].get('Balance', 0)), 2)
    expected = round(expected_closing, 2)
    diff = abs(csv_closing - expected)
    ok = diff <= tolerance

    if ok:
        msg = f"Closing balance verified: {csv_closing:.2f} (matches source)"
    else:
        msg = (
            f"CLOSING BALANCE MISMATCH: CSV shows {csv_closing:.2f}, "
            f"source statement shows {expected:.2f} (diff {diff:.2f}). "
            f"Possible dropped/duplicated entries."
        )

    return {
        "ok": ok,
        "csv_closing": csv_closing,
        "expected_closing": expected,
        "diff": diff,
        "message": msg,
    }


def format_balance_summary(
    running: dict,
    closing: dict | None = None,
) -> str:
    """
    Format a human-readable balance verification summary for the UI return string.

    Args:
        running:  Result from verify_running_balance()
        closing:  Result from verify_closing_balance() (optional)

    Returns:
        Multi-line summary string.
    """
    lines = []

    # Running balance
    if running["ok"]:
        lines.append(
            f"Running balance: OK ({running.get('opening_balance', 0):.2f} → "
            f"{running.get('closing_balance', 0):.2f})"
        )
    else:
        lines.append(
            f"Running balance: {running['mismatches']} mismatch(es)"
        )
        for d in running["details"]:
            lines.append(f"  {d}")

    # Closing balance
    if closing is not None:
        if closing["ok"]:
            lines.append(f"Closing balance: VERIFIED ({closing['csv_closing']:.2f})")
        else:
            lines.append(f"Closing balance: MISMATCH — {closing['message']}")

    return "\n".join(lines)
