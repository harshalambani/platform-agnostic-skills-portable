"""
agent.py -- Intercompany reconciliation between two GnuCash books.
DIRECT mode, deterministic, no LLM (so the output file is always written and the
UI download button appears reliably).

Reconciles the counter-party ("contra") accounts that two people keep for each
other -- matching the current-FY movements, carrying forward the opening
balance, and hunting the other book for probable mis-postings behind any
unmatched item. Writes an Excel workbook to output_path.
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
    book_a: str,
    book_b: str,
    output_path: str,
    period: str = "Auto (detect FY from filenames)",
    custom_start: str = "",
    custom_end: str = "",
    date_tolerance: str = "7",
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Reconcile two GnuCash books and write the reconciliation workbook to
    output_path. Returns a text summary for the UI.

    config_path and model_override are accepted for runner compatibility and
    ignored (this skill uses no LLM).
    """
    from reconcile_intercompany import reconcile
    from excel_report import write_workbook

    start = (custom_start or "").strip()
    end = (custom_end or "").strip()
    # A "custom" period selection only takes effect if both dates are supplied.
    if "custom" in (period or "").lower() and not (start and end):
        return ("ERROR: 'Custom date range' selected but Start/End dates are "
                "missing. Enter both as YYYY-MM-DD, or pick a FY option.")

    try:
        result = reconcile(
            book_a, book_b,
            period="" if "auto" in (period or "").lower() else period,
            start=start, end=end,
            tol_days=_tol(date_tolerance),
        )
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"

    write_workbook(result, output_path)

    a, b = result["book_a"], result["book_b"]
    diff = result["difference"]
    tie = "TIES OUT" if diff == 0 else f"OUT OF BALANCE by {diff:,.2f}"
    lines = [
        f"Intercompany reconciliation: {a.owner} <-> {b.owner}  [{result['fy_label']}]",
        f"  Contra in {a.path.name}: {', '.join(x.name for x in result['a_contra'])}",
        f"  Contra in {b.path.name}: {', '.join(x.name for x in result['b_contra'])}",
        f"  Opening b/f : {a.owner} {result['a_open']:,.2f}  |  {b.owner} {result['b_open']:,.2f}",
        f"  Closing c/f : {a.owner} {result['a_close']:,.2f}  |  {b.owner} {result['b_close']:,.2f}",
        f"  Matched pairs: {len(result['pairs'])}",
        f"  Exceptions   : {len(result['a_exc'])} in {a.owner}'s book, "
        f"{len(result['b_exc'])} in {b.owner}'s book",
        f"  Balance      : {tie}",
    ]
    if result["a_exc"] or result["b_exc"]:
        lines.append("  Review the Exceptions sheets -- each unmatched item lists "
                     "probable postings found in the other book.")
    lines.append(f"  Workbook: {output_path}")
    return "\n".join(lines)
