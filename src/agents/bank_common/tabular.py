"""
agents/bank_common/tabular.py — shared header-row detection + alias-table
column mapping for tabular (XLS/XLSX/CSV) bank statement exports.

Moved verbatim from ``skill_hdfc/agent.py`` (``_find_header_row`` /
``_map_columns``), which remains the reference implementation. Each bank
supplies its own alias tables; only the matching mechanics are shared.
"""
from __future__ import annotations

from typing import Sequence


def find_header_row(rows, date_aliases: Sequence[str], desc_aliases: Sequence[str]) -> int:
    """Find the header row: the FIRST row containing both a date-like column
    name (exact match against ``date_aliases``) and a description-like column
    name (substring match against ``desc_aliases``), regardless of position —
    tolerant of preamble rows and reordered/renamed exports. Returns -1 if no
    row matches."""
    for i, row in enumerate(rows):
        cells = [str(c).strip().lower() for c in row]
        has_date = any(c in date_aliases for c in cells)
        has_desc = any(any(k in c for k in desc_aliases) for c in cells)
        if has_date and has_desc:
            return i
    return -1


def map_columns(header_row, column_aliases: dict[str, Sequence[str]]) -> dict[str, int | None]:
    """Map each logical field in ``column_aliases`` to a column index in
    ``header_row``, via substring match against that field's alias list (first
    match wins, in header order). Fields with no match map to None."""
    headers = [str(h).strip().lower() for h in header_row]
    result: dict[str, int | None] = {}
    for field, candidates in column_aliases.items():
        idx = None
        for h_idx, h in enumerate(headers):
            if any(c in h for c in candidates):
                idx = h_idx
                break
        result[field] = idx
    return result
