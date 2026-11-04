"""
reconcile.py — AIS-INTERNAL reconciliation: cross-check each AIS element's
l1 (transaction-level detail) rows against its l2 (aggregate rollup) row.

This is Phase A: purely internal to one AIS file (it never touches 26AS or
the books) — pure functions over an already-normalized NormalizedAis (see
normalize.py). No I/O.

Field semantics (per the real AIS schema, confirmed against real files):
  - l1 rows are transaction-level detail, one per source/deductor/seller/etc.
    The row's own `feedback_editable_field` (set by normalize.py from the
    file's `displayFeedback=True` column descriptor) names the field holding
    that row's reportable income amount — e.g. `amtPaid` for tdsTcs,
    `salesConsideration`/`eodValue`/`amtReceived`/`totalPurchaseAmount` for
    sft (category-specific), `grossSalary` for salary rows under
    other-info. Reading it off the row rather than hardcoding a per-section
    field name keeps this schema-driven, matching normalize.py's approach.
  - tdsTcs l1 rows additionally carry the TDS *credit* (as opposed to the
    income amount) under `amountDeposited` or `amountDeducted` depending on
    AIS utility version — hardcoded here as known field names because this
    one rollup (total tax credit) has no per-row flag to read it off of.
  - l2 rows are aggregate rollups keyed by header STRING fields: `Amount`
    (as originally reported), `Derived Amount` (after AIS de-duplication /
    feedback processing — usually None when no derivation happened), `Count`.
  - Some sections (notably paymentOfTaxes in a modern AIS with no advance /
    self-assessment tax) emit no rows at all. That's handled by simply never
    producing a group for them — reconcile_internal never sees an empty
    group, so there is nothing to special-case.

Extension points for later phases (NOT built here):
  - Phase B (AIS <-> 26AS): populate ElementReco.external_matches with
    26AS-side match records — the field exists now, defaulting to [].
  - Phase C (books/ITR completeness + feedback suggestions): consumes
    AisRecoReport (flagged elements, `effective` amounts) to drive suggested
    feedback and books postings; needs no shape change here.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agents.skill_ais_reconcile.normalize import AisRow, NormalizedAis

# Small absolute tolerance for detail-vs-reported comparisons -- AIS amounts
# are rupee-and-paise decimals and can carry immaterial rounding drift that
# should not be reported as a difference.
TOLERANCE = Decimal("1")

# tdsTcs l1 rows carry the TDS *credit* under one of these field names
# (varies by AIS utility version); first one present on the row wins.
TDS_CREDIT_FIELD_CANDIDATES = ("amountDeposited", "amountDeducted")


@dataclass
class ElementReco:
    """Reconciliation result for one AIS element, keyed by
    (section_key, category, info_src_id)."""
    section_key: str
    category: str
    info_src_id: str | None

    l1_row_count: int
    detail_sum: Decimal | None          # sum of l1 rows' feedback-editable amount
    reported: Decimal | None            # l2 "Amount"
    derived: Decimal | None             # l2 "Derived Amount"
    effective: Decimal | None           # derived if present else reported
    l2_count: Any = None                # l2 "Count", as normalized (may be a raw string)

    delta_detail_vs_reported: Decimal | None = None
    flag_detail_mismatch: bool = False
    flag_derivation: bool = False
    # INFORMATIONAL ONLY -- deliberately NOT part of `flagged`. On real AIS
    # files the l2 "Count" is a coarser grain than the l1 detail rows (e.g.
    # tdsTcs "Count" counts deductors/entries while l1 is per-transaction, so
    # l1 rows routinely and legitimately outnumber Count -- 42 vs 17 etc.).
    # Treating len(l1) != Count as a difference would fire a loud false
    # positive on nearly every taxpayer and drown the real (money) call-outs,
    # so this is surfaced as a column but never raises the "DIFFERENCE FOUND"
    # banner. The meaningful reconciliations are the two money checks.
    flag_count_mismatch: bool = False

    status_breakdown: Counter = field(default_factory=Counter)

    # Phase B extension point (AIS <-> 26AS matching). Empty until then.
    external_matches: list[Any] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        # Count is intentionally excluded (see flag_count_mismatch note above);
        # only the two money differences constitute a loud call-out.
        return self.flag_detail_mismatch or self.flag_derivation


@dataclass
class AisRecoReport:
    json_version: str | None
    download_date: str | None
    logged_in_pan: str | None
    taxpayer_name: str | None

    elements: list[ElementReco] = field(default_factory=list)

    total_reported_by_section: dict[str, Decimal] = field(default_factory=dict)
    total_tds_credit: Decimal = Decimal("0")
    flagged_element_count: int = 0


def _sum_decimal(values) -> Decimal | None:
    """Sum an iterable of Decimal|None, skipping None. Returns None (not
    Decimal("0")) if every value was missing, so callers can tell "no data"
    apart from "sums to zero"."""
    total = None
    for v in values:
        if v is None:
            continue
        total = (total or Decimal("0")) + v
    return total


def _element_key(row: AisRow) -> tuple[str, str, str | None]:
    return (row.section_key, row.category, row.info_src_id)


def _taxpayer_name(part_a: dict) -> str | None:
    """The AIS partA name label is not a fixed string -- real exports use
    "Name of Assessee" (individuals) / "Name of Entity" etc., not "Name". Pick
    the first partA field whose label mentions "name" (and isn't the PAN/
    Aadhaar identity line) rather than hardcoding one label."""
    for label, value in part_a.items():
        low = label.lower()
        if "name" in low and "pan" not in low and "aadhaar" not in low and value:
            return value
    return None


def _l2_field(l2_rows: list[AisRow], field_name: str) -> Any:
    """An element normally carries exactly one l2 aggregate row. If more than
    one is present (not expected, but not fatal either), sum Decimal values
    and otherwise take the first non-None value rather than crashing."""
    values = [r.fields[field_name] for r in l2_rows
              if field_name in r.fields and r.fields[field_name] is not None]
    if not values:
        return None
    if all(isinstance(v, Decimal) for v in values):
        return _sum_decimal(values)
    return values[0]


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _reco_for_element(key: tuple[str, str, str | None], rows: list[AisRow]) -> ElementReco:
    section_key, category, info_src_id = key
    l1_rows = [r for r in rows if r.level == "l1"]
    l2_rows = [r for r in rows if r.level == "l2"]

    # Coerce each detail amount through _as_decimal: the feedback-editable
    # field is chosen by normalize.py from the column's displayFeedback flag,
    # not from its type, so on real files it can arrive as a numeric STRING
    # (e.g. other-info/Salary `grossSalary`, whose columnDataType is "string"
    # in the portal export even though it holds a money amount). _as_decimal
    # recovers a numeric string as Decimal and yields None for genuine text,
    # which _sum_decimal then skips -- so a truly non-numeric editable field
    # just drops out of detail_sum rather than crashing the reconcile.
    detail_sum = _sum_decimal(
        _as_decimal(r.fields.get(r.feedback_editable_field))
        for r in l1_rows
        if r.feedback_editable_field
    )
    reported = _l2_field(l2_rows, "Amount")
    derived = _l2_field(l2_rows, "Derived Amount")
    l2_count = _l2_field(l2_rows, "Count")

    effective = derived if derived is not None else reported

    delta = None
    flag_detail_mismatch = False
    if detail_sum is not None and reported is not None:
        delta = detail_sum - reported
        flag_detail_mismatch = abs(delta) > TOLERANCE

    flag_derivation = derived is not None and derived != reported

    flag_count_mismatch = False
    if l2_count is not None and l1_rows:
        l2_count_num = _as_decimal(l2_count)
        if l2_count_num is not None and l2_count_num != Decimal(len(l1_rows)):
            flag_count_mismatch = True

    status_breakdown = Counter(r.status for r in l1_rows if r.status)

    return ElementReco(
        section_key=section_key,
        category=category,
        info_src_id=info_src_id,
        l1_row_count=len(l1_rows),
        detail_sum=detail_sum,
        reported=reported,
        derived=derived,
        effective=effective,
        l2_count=l2_count,
        delta_detail_vs_reported=delta,
        flag_detail_mismatch=flag_detail_mismatch,
        flag_derivation=flag_derivation,
        flag_count_mismatch=flag_count_mismatch,
        status_breakdown=status_breakdown,
    )


def reconcile_internal(normalized: NormalizedAis) -> AisRecoReport:
    """Group NormalizedAis.rows by (section_key, category, info_src_id) and
    build one ElementReco per group, cross-checking l1 detail against the l2
    aggregate.

    Elements with no l2 aggregate at all (e.g. a section that only ever
    emits l1 rows) are handled gracefully: reported/derived/l2_count all stay
    None, and no reported-vs-detail flag can fire (nothing to compare
    against). A section with NO rows at all (e.g. an empty paymentOfTaxes)
    simply never produces a group -- there is nothing to iterate."""
    groups: dict[tuple, list[AisRow]] = {}
    order: list[tuple] = []
    for row in normalized.rows:
        key = _element_key(row)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    elements = [_reco_for_element(key, groups[key]) for key in order]

    total_reported_by_section: dict[str, Decimal] = {}
    for el in elements:
        if el.reported is not None:
            total_reported_by_section[el.section_key] = (
                total_reported_by_section.get(el.section_key, Decimal("0")) + el.reported
            )

    tds_credit_values = []
    for row in normalized.rows:
        if row.section_key != "tdsTcs" or row.level != "l1":
            continue
        for candidate in TDS_CREDIT_FIELD_CANDIDATES:
            v = _as_decimal(row.fields.get(candidate))
            if v is not None:
                tds_credit_values.append(v)
                break
    total_tds_credit = _sum_decimal(tds_credit_values) or Decimal("0")

    flagged_element_count = sum(1 for el in elements if el.flagged)

    return AisRecoReport(
        json_version=normalized.json_version,
        download_date=normalized.download_date,
        logged_in_pan=normalized.logged_in_pan,
        taxpayer_name=_taxpayer_name(normalized.part_a),
        elements=elements,
        total_reported_by_section=total_reported_by_section,
        total_tds_credit=total_tds_credit,
        flagged_element_count=flagged_element_count,
    )
