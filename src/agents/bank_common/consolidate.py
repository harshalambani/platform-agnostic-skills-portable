"""
agents/bank_common/consolidate.py — shared multi-statement consolidation.

Lifted verbatim (in behavior) from ``skill_hsbc/scripts/parse_tsv.py``'s
original inline logic, which is the reference implementation: HSBC is the
only bank that has always ordered multi-statement batches by actual
transaction date (not filename) and flagged gaps/overlaps between them.
BoB and ICICI previously did a naive ``sorted(glob)`` + blind concat, which
silently mis-orders batches whenever filenames don't sort chronologically
and never reports missing/overlapping periods.

Pure: no I/O, no filesystem access. Callers do their own per-bank
extraction and pass in already-parsed row groups plus each group's
transaction date range (as ISO ``YYYY-MM-DD`` strings, or ``None`` if a
statement yielded no dated rows).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence


@dataclass(frozen=True)
class StatementGroup:
    """One parsed statement's rows plus its transaction date range.

    ``name`` is typically the source filename (or directory name) — used
    only for warning messages and as the tie-break/fallback order for
    statements with no readable dates.
    """
    name: str
    rows: list[dict[str, Any]]
    period_start: str | None
    period_end: str | None


@dataclass(frozen=True)
class ConsolidationResult:
    rows: list[dict[str, Any]]
    warnings: list[str]


def _natural_key(name: str) -> list[Any]:
    """Sort key that orders embedded numbers numerically, e.g. 'stmt2' <
    'stmt10'. Used only as the fallback order for undated statements."""
    return [int(tok) if tok.isdigit() else tok.lower()
            for tok in re.findall(r'\d+|\D+', name)]


def check_continuity(groups: Sequence[StatementGroup]) -> list[str]:
    """Flag likely-missing or overlapping statements between consecutive,
    already date-ordered groups.

    ``groups`` must already be sorted (e.g. via :func:`consolidate`'s
    ordering) — this only looks at consecutive pairs. A gap of more than 3
    days between one statement's end and the next one's start is flagged as
    a possible missing statement; a gap of less than -1 days (i.e. the next
    statement starts more than a day before the previous one ends) is
    flagged as an overlap. Statements with no readable dates get their own
    "could not be checked" warning instead of participating in gap/overlap
    checks.
    """
    warnings: list[str] = []
    dated = [g for g in groups if g.period_start and g.period_end]
    for prev, cur in zip(dated, dated[1:]):
        gap_days = (date.fromisoformat(cur.period_start) -
                    date.fromisoformat(prev.period_end)).days
        if gap_days > 3:
            warnings.append(
                f"POSSIBLE MISSING STATEMENT: {gap_days}-day gap between "
                f"'{prev.name}' (ends {prev.period_end}) and "
                f"'{cur.name}' (starts {cur.period_start})."
            )
        elif gap_days < -1:
            warnings.append(
                f"OVERLAPPING/OUT-OF-ORDER STATEMENTS: '{cur.name}' "
                f"(starts {cur.period_start}) overlaps '{prev.name}' "
                f"(ends {prev.period_end})."
            )
    undated = [g.name for g in groups if not (g.period_start and g.period_end)]
    if undated:
        warnings.append(
            f"{len(undated)} statement(s) had no readable dates and could "
            f"not be checked for continuity: " + ", ".join(undated)
        )
    return warnings


def consolidate(groups: Sequence[StatementGroup]) -> ConsolidationResult:
    """Order statement groups by first-transaction date and concatenate
    their rows.

    Undated statements (no readable transaction dates) sort last, in
    natural filename order, since there's nothing better to go on for them.
    Returns the merged row list plus any gap/overlap/undated warnings —
    never raises on a gap or overlap, only reports it.
    """
    ordered = sorted(
        groups,
        key=lambda g: (g.period_start is None, g.period_start or '',
                        _natural_key(g.name)),
    )
    warnings = check_continuity(ordered)
    rows: list[dict[str, Any]] = []
    for g in ordered:
        rows.extend(g.rows)
    return ConsolidationResult(rows=rows, warnings=warnings)
