"""
ais_categories.py -- ITR-22: one rule for which AIS tdsTcs l1 rows are a TDS
CREDIT in the taxpayer's hands.

The AIS tdsTcs section mixes three different things:
  * TDS deducted from the taxpayer's income (a credit),
  * TCS collected from the taxpayer (belongs to the return's TCS schedule),
  * TDS the taxpayer deducted as payer (e.g. on rent paid) -- never a credit.

Rows are classified by EXACT category label (case and whitespace normalised,
whole-label match, no keywords). A label that is not listed is "unknown": it is
never summed and is reported by label and count, so a new AIS wording is seen
rather than silently counted or silently dropped.

Pure functions; no import of the other reconcile modules (they import this).
"""
from __future__ import annotations

from typing import Any, Iterable

CREDIT = "credit"
TCS = "tcs"
PAYER_SIDE = "payer_side"
UNKNOWN = "unknown"


def normalise_label(label: Any) -> str:
    return " ".join(str(label or "").lower().split())


_CREDIT_LABELS = frozenset(normalise_label(x) for x in (
    "Dividend",
    "Interest from deposit",
    "Interest from others",
    "Salary",
    # section 194T: still a credit; the return tie-out handles it as a partner row
    "Receipt of amount by Partners from Partnership Firm",
))
_TCS_LABELS = frozenset(normalise_label(x) for x in (
    "Foreign travel",
    "Outward foreign remittance/purchase of foreign currency",
))
_PAYER_SIDE_LABELS = frozenset(normalise_label(x) for x in (
    "Rent payment",
))

NOT_COUNTED_TEXT = "Not counted, unrecognised AIS category"


def classify_label(label: Any) -> str:
    key = normalise_label(label)
    if key in _CREDIT_LABELS:
        return CREDIT
    if key in _TCS_LABELS:
        return TCS
    if key in _PAYER_SIDE_LABELS:
        return PAYER_SIDE
    return UNKNOWN


def is_credit_row(row: Any) -> bool:
    return classify_label(getattr(row, "category", "")) == CREDIT


def tdstcs_l1_rows(normalized: Any) -> list:
    if normalized is None:
        return []
    return [r for r in normalized.rows if r.section_key == "tdsTcs" and r.level == "l1"]


def credit_rows(rows: Iterable[Any]) -> list:
    return [r for r in rows if is_credit_row(r)]


def rows_of_kind(rows: Iterable[Any], kind: str) -> list:
    return [r for r in rows if classify_label(getattr(r, "category", "")) == kind]


def uncounted_categories(rows: Iterable[Any]) -> list[tuple[str, int]]:
    """Unknown tdsTcs l1 categories as (label, row count), sorted by label."""
    counts: dict[str, int] = {}
    for r in rows:
        if classify_label(getattr(r, "category", "")) == UNKNOWN:
            lab = " ".join(str(getattr(r, "category", "") or "").split()) or "(blank)"
            counts[lab] = counts.get(lab, 0) + 1
    return sorted(counts.items())


def uncounted_text(items: list[tuple[str, int]]) -> str:
    return "; ".join(f"{lab} ({n} row{'s' if n != 1 else ''})" for lab, n in items)
