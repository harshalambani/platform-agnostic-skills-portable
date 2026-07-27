"""
reconcile_26as.py — Phase B: AIS <-> 26AS TDS-credit tie-out.

Cross-checks the TDS credit AIS reports (tdsTcs l1 rows' `amountDeposited` /
`amountDeducted`) against the TDS credit 26AS reports (each As26Transaction's
`tds_deposited`, from src/agents/skill_itr_workbook/scripts/as26.py). Pure
functions over an already-normalized NormalizedAis and an already-parsed
As26Data — no I/O, no filesystem access, no Data/ access.

Three tie-outs, from coarsest to finest grain:
  1. AGGREGATE: total AIS TDS credit vs total 26AS TDS deposited. The one
     number that matters most — this is what actually offsets tax payable.
  2. PER-QUARTER: same comparison split by financial-year quarter (Q1 Apr-Jun
     .. Q4 Jan-Mar), which is the grain 234B/234C interest computations care
     about. AIS carries its own free-text `quarter` field per l1 row; 26AS
     transactions carry a `txn_date` that is bucketed into the same FY-quarter
     scheme here (NOT the five 234C sub-windows in quarters.py — those track
     a different, narrower concern: interest-computation sub-periods, not
     AIS's own Q1..Q4 grain — so this module buckets 26AS directly rather
     than importing quarters.bucket_as26_transactions).
  3. PER-INCOME-CATEGORY: AIS credit bucketed by a keyword-classified income
     category (interest/dividend/other, derived from the element `category`
     string) vs 26AS credit bucketed by as26.classify_section(section,
     tds_sections) (which already returns 'interest'/'dividend'/None, mapped
     to "other" here). Only runs when `tds_sections` is supplied — with no
     rules config there's no section->category mapping to classify the 26AS
     side by, so the category tie-out is simply skipped (aggregate + quarter
     still run regardless).

     NOTE: there used to be a per-deductor best-effort match here (title vs
     26AS deductor_name). Removed — confirmed against real files that AIS's
     tdsTcs element `title` is the INCOME-TYPE CATEGORY ("Interest from
     deposit", "Dividend", "Salary", ...), not a deductor/payer name. AIS
     exposes no deductor identity and no TAN anywhere in normalized data, so
     that match could never succeed (0/0 on the real pair) and was pure
     noise. The per-category tie-out below is the real replacement: it's the
     grain AIS's own `title` actually carries.

Field semantics (per the real AIS schema, matching reconcile.py's Phase A):
  - tdsTcs l1 rows expose: tsnId, quarter (free string), transactionDate
    (free string), amtPaid (income), amountDeducted, amountDeposited (=TDS
    credit; amountDeducted is the fallback when amountDeposited is absent —
    same TDS_CREDIT_FIELD_CANDIDATES order as reconcile.py). There is no TAN
    and no deductor-name field on AIS l1 rows — none is invented here.
  - The element's `category` (AisRow.category, from normalize.py) is the AIS
    INCOME-TYPE label (e.g. "Interest from deposit", "Dividend"), used for
    the per-income-category tie-out via simple keyword classification.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agents.skill_ais_reconcile.normalize import AisRow, NormalizedAis
from agents.skill_ais_reconcile.reconcile import TDS_CREDIT_FIELD_CANDIDATES, TOLERANCE, _as_decimal
from agents.skill_itr_workbook.scripts.as26 import classify_section

# Financial-year quarters, 1-indexed by calendar month: Apr-Jun=1, Jul-Sep=2,
# Oct-Dec=3, Jan-Mar=4. Used for both sides of the per-quarter tie-out so AIS
# (whose own `quarter` field uses this same FY convention) and 26AS (whose
# transactions only carry a calendar txn_date) line up on the same grain.
_MONTH_TO_FY_QUARTER = {
    4: 1, 5: 1, 6: 1,
    7: 2, 8: 2, 9: 2,
    10: 3, 11: 3, 12: 3,
    1: 4, 2: 4, 3: 4,
}

_QUARTER_DIGIT_RE = re.compile(r"[1-4]")


@dataclass
class QuarterTieOut:
    """AIS vs 26AS TDS-credit comparison for one FY quarter (1-4)."""
    quarter: int
    ais_tds_credit: Decimal
    as26_tds_deposited: Decimal
    delta: Decimal
    flagged: bool


@dataclass
class CategoryTieOut:
    """AIS vs 26AS TDS-credit comparison for one income category
    ('interest' / 'dividend' / 'other'), for whichever categories appear on
    EITHER side (union) -- a category present only on one side still shows
    up, with a zero on the other, so it's visible rather than silently
    dropped."""
    category: str
    ais_credit: Decimal
    as26_deposited: Decimal
    delta: Decimal
    flagged: bool


@dataclass
class Ais26asReport:
    total_ais_tds_credit: Decimal
    total_26as_tds_deposited: Decimal
    delta_aggregate: Decimal
    flag_aggregate_mismatch: bool

    quarters: list[QuarterTieOut] = field(default_factory=list)
    # Empty when tds_sections was not supplied to reconcile_ais_vs_26as --
    # there's no section->category mapping to classify the 26AS side by, so
    # the tie-out is skipped rather than guessed at.
    categories: list[CategoryTieOut] = field(default_factory=list)

    @property
    def flagged_quarters(self) -> list[int]:
        return [q.quarter for q in self.quarters if q.flagged]

    @property
    def flagged_categories(self) -> list[str]:
        return [c.category for c in self.categories if c.flagged]


def _extract_fy_quarter(raw: Any) -> int | None:
    """AIS's `quarter` field is free text ("Q1", "Quarter 1", "1", "QTR-1",
    ...). Lenient extraction: the first digit 1-4 found in the string wins."""
    if raw is None:
        return None
    m = _QUARTER_DIGIT_RE.search(str(raw))
    return int(m.group()) if m else None


def _fy_quarter_from_month(month: int) -> int | None:
    return _MONTH_TO_FY_QUARTER.get(month)


def _classify_ais_category(title: str | None) -> str:
    """Keyword-based classification of an AIS tdsTcs element `title` (the
    income-type category, e.g. "Interest from deposit", "Dividend
    (Domestic)", "Salary received") into 'interest' / 'dividend' / 'salary' /
    'other'. These named buckets mirror the 26AS side's classify_section
    categories (tds_sections: interest/dividend/salary), so both sides land in
    the same bucket for the per-category tie-out. Deliberately NOT a fixed
    enumerated list of known titles -- new/unfamiliar titles just fall to
    'other' rather than needing a code change every time AIS phrasing
    shifts."""
    low = (title or "").lower()
    if "interest" in low:
        return "interest"
    if "dividend" in low:
        return "dividend"
    if "salary" in low:
        return "salary"
    return "other"


def _tds_credit(row: AisRow) -> Decimal | None:
    for candidate in TDS_CREDIT_FIELD_CANDIDATES:
        v = _as_decimal(row.fields.get(candidate))
        if v is not None:
            return v
    return None


def _ais_tdstcs_l1_rows(normalized: NormalizedAis) -> list[AisRow]:
    return [r for r in normalized.rows if r.section_key == "tdsTcs" and r.level == "l1"]


def _sum(values) -> Decimal:
    total = Decimal("0")
    for v in values:
        if v is not None:
            total += v
    return total


def _ais_quarterly_credit(rows: list[AisRow]) -> dict[int, Decimal]:
    out: dict[int, Decimal] = {}
    for r in rows:
        credit = _tds_credit(r)
        if credit is None:
            continue
        q = _extract_fy_quarter(r.fields.get("quarter"))
        if q is None:
            continue
        out[q] = out.get(q, Decimal("0")) + credit
    return out


def _as26_quarterly_credit(transactions: list) -> dict[int, Decimal]:
    out: dict[int, Decimal] = {}
    for txn in transactions:
        if txn.txn_date is None:
            continue
        q = _fy_quarter_from_month(txn.txn_date.month)
        if q is None:
            continue
        deposited = _as_decimal(txn.tds_deposited)
        if deposited is None:
            continue
        out[q] = out.get(q, Decimal("0")) + deposited
    return out


def _ais_credit_by_category(rows: list[AisRow]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for r in rows:
        credit = _tds_credit(r)
        if credit is None:
            continue
        cat = _classify_ais_category(r.category)
        out[cat] = out.get(cat, Decimal("0")) + credit
    return out


def _as26_deposited_by_category(transactions: list, tds_sections: dict) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for txn in transactions:
        deposited = _as_decimal(txn.tds_deposited)
        if deposited is None:
            continue
        cat = classify_section(txn.section, tds_sections) or "other"
        out[cat] = out.get(cat, Decimal("0")) + deposited
    return out


def reconcile_ais_vs_26as(normalized: NormalizedAis, as26: Any,
                           tds_sections: dict | None = None) -> Ais26asReport:
    """Tie out AIS's reported TDS credit (tdsTcs l1 amountDeposited /
    amountDeducted) against 26AS's tds_deposited, at three grains:
    aggregate, per-FY-quarter, and per-income-category (interest/dividend/
    other).

    `tds_sections` is the Rules-config-driven section-code->category map
    (see as26.classify_section) needed to classify the 26AS side into
    interest/dividend/other. When it's not supplied, the per-category
    tie-out is skipped entirely (`categories` stays an empty list) -- the
    aggregate and per-quarter tie-outs need no section classification and
    still run.
    """
    ais_rows = _ais_tdstcs_l1_rows(normalized)
    transactions = list(getattr(as26, "transactions", None) or [])

    # 1. Aggregate.
    total_ais = _sum(_tds_credit(r) for r in ais_rows)
    total_26as = _sum(_as_decimal(t.tds_deposited) for t in transactions)
    delta_aggregate = total_ais - total_26as
    flag_aggregate_mismatch = abs(delta_aggregate) > TOLERANCE

    # 2. Per-quarter.
    ais_q = _ais_quarterly_credit(ais_rows)
    as26_q = _as26_quarterly_credit(transactions)
    quarters: list[QuarterTieOut] = []
    for q in (1, 2, 3, 4):
        a = ais_q.get(q, Decimal("0"))
        b = as26_q.get(q, Decimal("0"))
        d = a - b
        quarters.append(QuarterTieOut(
            quarter=q, ais_tds_credit=a, as26_tds_deposited=b, delta=d,
            flagged=abs(d) > TOLERANCE,
        ))

    # 3. Per-income-category (only when tds_sections is available to
    # classify the 26AS side).
    categories: list[CategoryTieOut] = []
    if tds_sections is not None:
        ais_cat = _ais_credit_by_category(ais_rows)
        as26_cat = _as26_deposited_by_category(transactions, tds_sections)
        for cat in sorted(set(ais_cat) | set(as26_cat)):
            a = ais_cat.get(cat, Decimal("0"))
            b = as26_cat.get(cat, Decimal("0"))
            d = a - b
            categories.append(CategoryTieOut(
                category=cat, ais_credit=a, as26_deposited=b, delta=d,
                flagged=abs(d) > TOLERANCE,
            ))

    return Ais26asReport(
        total_ais_tds_credit=total_ais,
        total_26as_tds_deposited=total_26as,
        delta_aggregate=delta_aggregate,
        flag_aggregate_mismatch=flag_aggregate_mismatch,
        quarters=quarters,
        categories=categories,
    )
