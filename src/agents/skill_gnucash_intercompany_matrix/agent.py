"""
agent.py -- OPTIONAL all-family intercompany matrix. DIRECT mode, no LLM.

Reconciles every pair among the supplied GnuCash books and writes a single
roll-up workbook (Matrix grid, Pairs summary, All Exceptions). Reuses the
pairwise engine from skill_gnucash_intercompany.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _tol(value, default: int = 7) -> int:
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def run(
    books,
    output_path: str,
    period: str = "Auto (detect FY from filenames)",
    custom_start: str = "",
    custom_end: str = "",
    date_tolerance: str = "7",
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Reconcile every pair among `books` (a list of .gnucash paths) and write the
    matrix workbook to output_path. Returns a text summary for the UI.
    """
    from matrix_recon import run_matrix
    from matrix_report import write_matrix_workbook

    paths = [books] if isinstance(books, str) else list(books)
    if len(paths) < 2:
        return "ERROR: select at least two .gnucash books for a matrix."

    start = (custom_start or "").strip()
    end = (custom_end or "").strip()
    if "custom" in (period or "").lower() and not (start and end):
        return ("ERROR: 'Custom date range' selected but Start/End dates are "
                "missing. Enter both as YYYY-MM-DD, or pick a FY option.")

    try:
        m = run_matrix(
            paths,
            period="" if "auto" in (period or "").lower() else period,
            start=start, end=end, tol_days=_tol(date_tolerance),
        )
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"

    write_matrix_workbook(m, output_path)

    ok = [p for p in m["pairs"] if p.get("ok")]
    na = [p for p in m["pairs"] if not p.get("ok")]
    out_of_balance = [p for p in ok if p["res"]["difference"] != 0]
    with_exc = [p for p in ok if p["res"]["a_exc"] or p["res"]["b_exc"]]

    lines = [
        f"Intercompany matrix [{m['fy_label']}] -- {len(paths)} books, "
        f"{len(m['pairs'])} pairs.",
        f"  Reconciled: {len(ok)}   |   n/a (no mutual contra): {len(na)}",
        f"  Out of balance: {len(out_of_balance)}   |   With exceptions: {len(with_exc)}",
    ]
    for p in out_of_balance:
        r = p["res"]
        lines.append(f"  OUT OF BALANCE  {p['a_owner']} <-> {p['b_owner']} "
                     f"by {r['difference']:,.2f}")
    lines.append("  See the Matrix grid, the Pairs sheet, and All Exceptions for detail.")
    lines.append(f"  Workbook: {output_path}")
    return "\n".join(lines)
