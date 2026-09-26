"""
engine.py -- Partner Compensation Reconciliation: pure computation engine.

No I/O, no filesystem access, no network. Every function here takes plain
Python data (dicts/dataclasses built from the already-loaded structured
input -- see AGENT.md and skill.yaml for the YAML/JSON shape) and returns
plain data. `writer.py` is the only module in this package that touches
openpyxl; `agent.py` is the only one that touches the filesystem.

Governing rule (see AGENT.md and the spec this package was built from):
NO rate, percentage, or period is ever a module-level constant or a
fallback default. Every one of them is read out of the caller-supplied
``drivers`` block for the financial year being processed, and a value that
is missing produces an explicit CANNOT-RECONCILE result -- never a guess,
never a silent zero.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Small shared plumbing.
# ---------------------------------------------------------------------------

CANNOT_RECONCILE = "CANNOT RECONCILE"

# H35-04 rework item C: this row (and the parallel "bank" leg in
# gnucash_tieout.py's balance tie-out) is not a genuine gap -- it is simply
# not wired up YET. H35-05 (queued next) does the partner-side fuzzy match
# of payouts to bank credits that this row needs. Until then it must never
# read as a failure and must never enter the LOUD block -- see
# statement_flags assembly below, which only ever fires on a
# "STATEMENT DISAGREES"-prefixed note, so a NOT_CHECKED_YET-labelled row is
# excluded from it automatically.
NOT_CHECKED_YET = "NOT CHECKED YET (bank credits are matched in H35-05)"

# H35-04 rework item A: the verdict used when this skill's OWN pending
# journal(s) (a monthly line not yet posted, or the year-end accrual) close
# a statement-vs-book gap to within RECONCILIATION_TOLERANCE. This is never
# a genuine disagreement -- see statement_reference_row() below -- so a row
# carrying this verdict has agree=True and is excluded from the LOUD block.
PENDING_JOURNAL_VERDICT = "PENDING JOURNAL POSTING (provided by this skill)"

# The Re 1 reconciliation tolerance (item 3.2 / H35-02): a difference of up
# to this many rupees between two sources ties; anything more is reported as
# a VARIANCE, never silently rounded away. This is the single named source
# of truth for that figure across this package -- reconcile_category()'s
# default below, the L5 tie-out rows, and the year-end accrual/residual
# logic all reference this constant rather than restating "1.0" or "Re 1"
# independently, so a future change to the tolerance cannot desync between
# call sites.
RECONCILIATION_TOLERANCE = 1.0


def _parse_date(value) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def fy_of_date(d) -> str:
    """India's financial year (1 April - 31 March) label for a date, e.g.
    a date of 2025-07-31 -> "2025-26", a date of 2026-01-15 -> "2025-26"."""
    d = _parse_date(d)
    start_year = d.year if d.month >= 4 else d.year - 1
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def fy_start_year(fy: str) -> int:
    """"2025-26" -> 2025."""
    return int(fy.split("-")[0])


def driver(drivers: dict, key: str, fy: str, label: str | None = None):
    """Read one driver value for one financial year. Returns
    (value, None) if present, or (None, reason) if missing -- the reason
    string is the exact CANNOT-RECONCILE wording this skill uses
    everywhere a rate/period/date is absent. Never applies a default.
    """
    if drivers is None or key not in drivers or drivers[key] is None:
        shown = label or key
        return None, f"{CANNOT_RECONCILE} -- {shown} not supplied for FY{fy}"
    return drivers[key], None


def fy_prefix(fy: str) -> str:
    """Compact financial-year prefix for Transaction IDs: '2025-26' ->
    '2526'. Moved here (H35-04 rework) from jv_emitter.py, which imports it
    back, so build_report() can display a pending journal's Transaction ID
    in a reconciliation row's note without a circular import (jv_emitter.py
    already imports FROM engine.py). jv_emitter.py re-exports this under
    the same name, so `from .jv_emitter import fy_prefix` (used by tests)
    keeps working unchanged."""
    m = re.match(r"\s*(\d{4})-(\d{2})\s*$", fy or "")
    if m:
        return m.group(1)[2:] + m.group(2)
    return re.sub(r"[^0-9A-Za-z]", "", fy or "FY")


def firm_token(firm_name: str) -> str:
    """Short firm-scoped Transaction ID prefix derived from firm_name, e.g.
    "KPMG India Services LLP" -> "KPMG". Moved here (H35-04 rework) from
    jv_emitter.py's private _firm_token() -- jv_emitter.py imports this back
    as `_firm_token`, so its own module-level name and every existing test
    that calls `jv_emitter._firm_token(...)` directly keep working
    unchanged."""
    words = (firm_name or "").split()
    if not words:
        return ""
    return re.sub(r"[^0-9A-Za-z]", "", words[0]).upper()


def journal_txn_id(fy_pfx: str, firm_name: str, suffix: str) -> str:
    """Build a Transaction ID as '<firm_token>-<fy_pfx>-<suffix>' when a
    firm token is available, else the bare '<fy_pfx>-<suffix>'. Moved here
    (H35-04 rework) from jv_emitter.py's private _txn_id() -- jv_emitter.py
    imports this back as `_txn_id`, so every existing test that calls
    `jv_emitter._txn_id(...)` directly keeps working unchanged."""
    token = firm_token(firm_name)
    if token:
        return f"{token}-{fy_pfx}-{suffix}"
    return f"{fy_pfx}-{suffix}"


def field_or_reason(container: dict, key: str, what: str):
    """Same contract as driver() but for any other required input field
    (an `external` figure, an `advisory` figure, etc.) rather than a
    per-FY driver."""
    if container is None or key not in container or container[key] is None:
        return None, f"{CANNOT_RECONCILE} -- {what} not supplied"
    return container[key], None


# ---------------------------------------------------------------------------
# 3.1 Miscellaneous adjustment -- always derive, never read.
# ---------------------------------------------------------------------------

def derive_misc(total_paid: float, remuneration: float, share_of_profit_gross: float,
                 additional_share_of_profit: float) -> float:
    """misc = total_paid - remuneration - share_of_profit - additional_share_of_profit.

    The printed "Misc Adjustments" line on a monthly payout advice is never
    read -- its sign is inconsistent between months -- this derived value
    always wins, whatever the advice prints.
    """
    return total_paid - remuneration - share_of_profit_gross - additional_share_of_profit


# ---------------------------------------------------------------------------
# 3.2 Grossing up a net one-off.
# ---------------------------------------------------------------------------

@dataclass
class OneOffResult:
    net: float
    firms_tax_rate: float | None
    gross: float | None
    roundness: float | None
    status: str  # "CONFIRMED" / "SUSPECT" / "CANNOT RECONCILE"
    reason: str | None = None


ROUNDNESS_TOLERANCE = 1000.0  # rupees; see AGENT.md for why this figure


def gross_up_one_off(net: float, firms_tax_rate: float | None, fy: str) -> OneOffResult:
    """gross = net / (1 - firms_tax_rate). Also reports how far `gross` sits
    from the nearest 100,000 -- a one-off is always awarded as a round
    number, so a near-zero distance confirms both the gross-up and the
    rate used; a large distance means the rate is wrong for that year and
    must be flagged, never smoothed over.
    """
    if firms_tax_rate is None:
        return OneOffResult(
            net=net, firms_tax_rate=None, gross=None, roundness=None,
            status=CANNOT_RECONCILE,
            reason=f"{CANNOT_RECONCILE} -- firm's tax rate not supplied for FY{fy}",
        )
    gross = net / (1 - firms_tax_rate)
    nearest_lakh = round(gross / 100000.0) * 100000.0
    roundness = abs(gross - nearest_lakh)
    status = "CONFIRMED" if roundness <= ROUNDNESS_TOLERANCE else "SUSPECT"
    return OneOffResult(net=net, firms_tax_rate=firms_tax_rate, gross=gross,
                         roundness=roundness, status=status)


# ---------------------------------------------------------------------------
# 5.1 / 5.2 Capital contribution.
# ---------------------------------------------------------------------------

@dataclass
class CapitalRuleResult:
    required_cumulative_capital: float | None
    status: str  # "OK" / "CANNOT RECONCILE"
    reason: str | None = None


def required_cumulative_capital(target_compensation, months_achieved, months_total,
                                 rate, fy: str) -> CapitalRuleResult:
    """required_cumulative_capital = TC * (months_achieved / months_total) * rate.

    Every one of the four inputs is a per-FY driver/advisory figure, never
    a constant -- a missing one produces an explicit cannot-reconcile
    result rather than a guess.
    """
    missing = [
        name for name, v in (
            ("target compensation", target_compensation),
            ("capital months achieved", months_achieved),
            ("capital months total", months_total),
            ("capital contribution rate", rate),
        ) if v is None
    ]
    if missing:
        return CapitalRuleResult(
            required_cumulative_capital=None, status=CANNOT_RECONCILE,
            reason=f"{CANNOT_RECONCILE} -- {', '.join(missing)} not supplied for FY{fy}",
        )
    value = target_compensation * (months_achieved / months_total) * rate
    return CapitalRuleResult(required_cumulative_capital=value, status="OK")


CAPITAL_TOLERANCE = 1.0  # rupee


@dataclass
class RateChangeSuspect:
    implied_old_rate: float
    implied_new_rate: float
    first_instalment_capital: float
    instalment_count: int
    actual_total: float
    note: str


def detect_mid_year_rate_change(instalment_capitals: list[float], target_compensation,
                                 months_achieved, months_total,
                                 instalment_grosses: list[float] | None = None) -> RateChangeSuspect | None:
    """See spec s.5.2. If a cohort's instalment capital-deducted figures are
    not all equal (beyond a 1-rupee tolerance), that asymmetry COULD be the
    fingerprint of the firm changing the capital rate part-way through the
    cohort -- but only when the instalments' GROSS figures are all equal
    too. When the grosses themselves differ, unequal capital-deducted
    figures are the expected, arithmetic consequence of unequal grosses
    (a bigger instalment carries a bigger capital deduction even at an
    unchanged rate) -- not evidence of a rate change, and not flagged.

    `instalment_grosses`, when supplied, must be the same length as
    `instalment_capitals` (one gross per capital figure, same instalment
    order). Returns None (no exception) when every instalment's capital
    matches the first within tolerance, when the grosses are supplied and
    are not all equal within CAPITAL_TOLERANCE, or when the inputs needed
    to compute the implied rates are missing (the asymmetry is still real
    in that last case, but the implied-rate figures cannot be computed --
    callers should still surface the raw asymmetry as an open item then).
    """
    if not instalment_capitals:
        return None
    first = instalment_capitals[0]
    if all(abs(c - first) <= CAPITAL_TOLERANCE for c in instalment_capitals):
        return None
    if instalment_grosses:
        first_gross = instalment_grosses[0]
        if not all(abs(g - first_gross) <= CAPITAL_TOLERANCE for g in instalment_grosses):
            return None
    if target_compensation is None or months_achieved is None or months_total is None:
        return None
    base = target_compensation * (months_achieved / months_total)
    if base == 0:
        return None
    count = len(instalment_capitals)
    implied_old_total = first * count
    implied_old_rate = implied_old_total / base
    actual_total = sum(instalment_capitals)
    implied_new_rate = actual_total / base
    return RateChangeSuspect(
        implied_old_rate=implied_old_rate,
        implied_new_rate=implied_new_rate,
        first_instalment_capital=first,
        instalment_count=count,
        actual_total=actual_total,
        note=(
            "RATE CHANGE SUSPECTED -- instalment capital-deducted figures within "
            "this cohort are not equal. Implied old rate "
            f"{implied_old_rate:.4f}, implied new rate {implied_new_rate:.4f}. "
            "Re-read this year's Compensation Advisory -- it will have been "
            "reissued mid-year at the new rate."
        ),
    )


# ---------------------------------------------------------------------------
# 4. Incentive cohorts and the FY straddle.
# ---------------------------------------------------------------------------

@dataclass
class InstalmentRow:
    award_fy: str
    payment_date: date
    gross: float
    firms_tax: float | None
    capital: float | None
    net: float | None
    instalment_fy: str
    membership: str  # "reporting" / "prior" / "future"
    label: str


def classify_cohort_instalments(cohort: dict, reporting_fy: str) -> list[InstalmentRow]:
    """Assigns every instalment in one cohort to the FY of its *payment*
    date (never the award FY), and tags it reporting / prior / future
    relative to `reporting_fy`. Only "reporting" instalments belong in that
    year's totals -- prior/future ones are carried in the ledger for
    traceability but excluded from the reporting FY's arithmetic.
    """
    award_fy = cohort["award_fy"]
    reporting_start = fy_start_year(reporting_fy)
    rows: list[InstalmentRow] = []
    for inst in cohort.get("instalments", []):
        pdate = _parse_date(inst["date"])
        inst_fy = fy_of_date(pdate)
        inst_start = fy_start_year(inst_fy)
        if inst_start == reporting_start:
            membership, label = "reporting", f"reporting (FY{award_fy} cohort)"
        elif inst_start > reporting_start:
            membership, label = "future", f"future (FY {inst_fy})"
        else:
            membership, label = "prior", f"prior (FY {inst_fy})"
        rows.append(InstalmentRow(
            award_fy=award_fy, payment_date=pdate, gross=inst.get("gross"),
            firms_tax=inst.get("firms_tax"), capital=inst.get("capital"),
            net=inst.get("net"), instalment_fy=inst_fy, membership=membership,
            label=label,
        ))
    return rows


# ---------------------------------------------------------------------------
# 3.4 Remuneration TDS section/applicability -- per-FY config, never hardcoded.
# ---------------------------------------------------------------------------

@dataclass
class TdsApplicability:
    section: str | None
    rate: float | None
    start_date: date | None
    status: str  # "OK" / "CANNOT RECONCILE"
    reason: str | None = None

    def applicable_on(self, d) -> bool | None:
        """True/False once start_date is known; None if it cannot be
        determined (missing driver)."""
        if self.start_date is None:
            return None
        return _parse_date(d) >= self.start_date


def remuneration_tds_applicability(drivers: dict, fy: str) -> TdsApplicability:
    section, section_reason = driver(drivers, "remuneration_tds_section", fy,
                                      "remuneration TDS section")
    rate, rate_reason = driver(drivers, "remuneration_tds_rate", fy,
                                "remuneration TDS rate")
    start_raw = drivers.get("remuneration_tds_start_date") if drivers else None
    if rate is None and section is None and start_raw is None:
        # Genuinely no partner-remuneration-TDS regime configured for this
        # FY -- before the s.194T-style deduction existed. Not an error:
        # only the payroll stream carries any TDS in a year like this.
        return TdsApplicability(section=None, rate=None, start_date=None, status="OK")
    if rate is None:
        return TdsApplicability(section=None, rate=None, start_date=None,
                                 status=CANNOT_RECONCILE, reason=rate_reason)
    if start_raw is None:
        return TdsApplicability(
            section=section, rate=rate, start_date=None, status=CANNOT_RECONCILE,
            reason=f"{CANNOT_RECONCILE} -- remuneration TDS start date not "
                   f"supplied for FY{fy} (needed to tell which months it applies to)",
        )
    return TdsApplicability(section=section, rate=rate, start_date=_parse_date(start_raw),
                             status="OK")


# ---------------------------------------------------------------------------
# 3.3 Firm's tax carries no TDS credit.
# ---------------------------------------------------------------------------

def firms_tax_conflated_with_26as(firms_tax_total: float, form_26as_total_credit: float | None,
                                   computed_creditable_tds: float | None) -> str | None:
    """Firm's tax on share of profit is a permanent cost and must appear
    nowhere in Form 26AS. If the supplied 26AS total looks large enough to
    include it (i.e. it is far closer to tds+|firms_tax| than to tds
    alone), that is a conflation upstream and must be flagged.
    Returns a note string if suspected, else None.
    """
    if form_26as_total_credit is None or computed_creditable_tds is None:
        return None
    combined = computed_creditable_tds + abs(firms_tax_total)
    dist_to_tds_only = abs(form_26as_total_credit - computed_creditable_tds)
    dist_to_combined = abs(form_26as_total_credit - combined)
    if dist_to_combined < dist_to_tds_only:
        return (
            "Form 26AS total credit is closer to (TDS + firm's tax) than to TDS "
            "alone -- firm's tax on share of profit carries no TDS credit and must "
            "never appear in Form 26AS. Re-check upstream for conflation."
        )
    return None


# ---------------------------------------------------------------------------
# Reconciliation matrix idiom (mirrors skill_mf_cas.cg_parser.reconcile).
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationResult:
    category: str
    sources: dict  # label -> value or None
    agree: bool | None  # True / False / None (cannot reconcile)
    note: str = ""
    # H35-04 item D: True marks a row as TRULY informational -- it must
    # never be counted toward agree/disagree/undecidable totals or the
    # summary verdict, and must never enter the LOUD block, regardless of
    # what `agree` happens to be on a given run. Figures stay visible on
    # the row; only the counting/aggregation in agent.py excludes it.
    informational: bool = False


def reconcile_category(category: str, sources: dict,
                        tolerance: float = RECONCILIATION_TOLERANCE) -> ReconciliationResult:
    """Generic N-source reconciliation, exactly the skill_mf_cas idiom
    generalised past three columns: every present value must agree with
    every other present value within tolerance to AGREE; any two disagree
    -> VARIANCE; fewer than two values present -> CANNOT RECONCILE,
    reported explicitly with which source(s) were missing.
    """
    present = {k: v for k, v in sources.items() if v is not None}
    missing = [k for k, v in sources.items() if v is None]
    if len(present) < 2:
        note = f"{CANNOT_RECONCILE} -- missing: {', '.join(missing) or 'insufficient sources'}"
        return ReconciliationResult(category=category, sources=sources, agree=None, note=note)
    values = list(present.values())
    baseline = values[0]
    agree = all(abs(v - baseline) <= tolerance for v in values[1:])
    note = ""
    if not agree:
        spread = max(values) - min(values)
        note = f"Variance of {spread:,.2f} across sources: {present}"
    elif missing:
        note = f"Agreed sources present; not supplied: {', '.join(missing)}"
    return ReconciliationResult(category=category, sources=sources, agree=agree, note=note)


def statement_reference_row(
    category: str, statement_value, statement_label: str, other_sources: dict,
    tolerance: float = RECONCILIATION_TOLERANCE,
    pending_journal: dict | None = None,
) -> ReconciliationResult:
    """H35-04 item A: for every row where the LLP Statement of Account (L5)
    carries a figure, the statement is the reference ("gospel truth") --
    every other supplied source is measured AGAINST it, never treated as an
    equal peer the way reconcile_category()'s "first value happens to be
    the baseline" idiom would. Each disagreement beyond `tolerance` is
    spelled out explicitly: "Statement says X; <source> says Y; difference
    Z" -- never just "variance across sources".

    When `statement_value` is None (no statement supplied for this row, or
    the field could not be parsed), this degrades to the OLD (pre-H35-04)
    all-sources-equal reconcile_category() behaviour over `other_sources`
    alone (the statement is never in the sources dict in that case, exactly
    as before), with a note stating plainly that no statement was supplied
    -- it never silently promotes another source to be the reference.

    `pending_journal` (H35-04 rework, item A -- "compare against book PLUS
    this skill's own pending journals"): optional dict describing ONE
    not-yet-posted journal effect this skill itself proposes (a monthly
    line, or the year-end accrual), keyed:
        "applies_to":  the `other_sources` label this journal would post
                       against (e.g. "Booked (monthly)"),
        "amount":      the signed rupee amount that journal would add to
                       that label's value once posted,
        "journal_ids": list of Transaction ID string(s) for the journal(s),
        "description": short human description of what the journal is.
    When the labelled source disagrees with the statement beyond
    `tolerance`, and applying `amount` to it would tie it to the statement
    within tolerance, the row is NOT a disagreement: it gets
    PENDING_JOURNAL_VERDICT, agree=True, and the note names the journal
    id(s) and the amount that closes it -- this is a finished, reconciled
    row, not a gap, and is excluded from the LOUD block by construction
    (agree=True). When applying the amount only PARTIALLY closes the gap,
    the row IS still a genuine disagreement (STATEMENT DISAGREES, agree=
    False, in the LOUD block) but the note shows BOTH what the pending
    journal explains AND the genuine residual left after it -- never just
    the raw, pre-journal gap. The literal phrase "PENDING JOURNAL POSTING"
    is used ONLY for the fully-closed case, never for a partial one, so the
    two are never ambiguous to a reader (or a test) grepping for it.
    """
    if statement_value is None:
        result = reconcile_category(category, dict(other_sources), tolerance=tolerance)
        prefix = (
            "No LLP Statement of Account (L5) figure supplied for this row -- "
            "the statement is not available as the reference here. "
        )
        result.note = prefix + (result.note or "")
        return result

    sources = {statement_label: statement_value, **other_sources}
    present_others = {k: v for k, v in other_sources.items() if v is not None}
    missing_others = [k for k, v in other_sources.items() if v is None]

    if not present_others:
        note = (
            f"{CANNOT_RECONCILE} -- the statement's figure ({statement_value:,.2f}) is "
            "the reference for this row, but no other source is available to "
            f"compare it against (missing: {', '.join(missing_others) or 'all other sources'})."
        )
        return ReconciliationResult(category=category, sources=sources, agree=None, note=note)

    pj_label = pending_journal.get("applies_to") if pending_journal else None
    pj_amount = pending_journal.get("amount") if pending_journal else None
    pj_ids = pending_journal.get("journal_ids") if pending_journal else None
    pj_description = pending_journal.get("description") if pending_journal else ""
    has_pending = bool(pending_journal) and pj_amount is not None and pj_amount != 0

    disagreements = []
    agreements = []
    closures = []  # rows whose gap is FULLY explained by a pending journal
    for label, value in present_others.items():
        diff = value - statement_value
        if abs(diff) <= tolerance:
            agreements.append(label)
            continue
        if has_pending and label == pj_label:
            ids = ", ".join(pj_ids) if pj_ids else "the journal this skill produced"
            adjusted_value = value + pj_amount
            adjusted_diff = adjusted_value - statement_value
            if abs(adjusted_diff) <= tolerance:
                closures.append(
                    f"{PENDING_JOURNAL_VERDICT} -- {label} ({value:,.2f}) plus "
                    f"{pj_description or 'a not-yet-posted journal'} "
                    f"({pj_amount:,.2f}, journal {ids}) = {adjusted_value:,.2f}, which "
                    f"ties to the statement ({statement_value:,.2f}) within tolerance. "
                    f"Post {ids} and this row is reconciled; it is not a disagreement."
                )
                continue
            journal_desc = pj_description or "this skill's journal"
            disagreements.append(
                f"Statement says {statement_value:,.2f}; {label} says {value:,.2f}; "
                f"difference {diff:,.2f}. The not-yet-posted {journal_desc} "
                f"(journal {ids}, {pj_amount:,.2f}) explains part of the gap: "
                f"{label} plus that journal = {adjusted_value:,.2f}. Genuine "
                f"residual after posting {ids}: {adjusted_diff:,.2f}."
            )
            continue
        disagreements.append(
            f"Statement says {statement_value:,.2f}; {label} says {value:,.2f}; "
            f"difference {diff:,.2f}."
        )

    if disagreements:
        note = "STATEMENT DISAGREES -- " + " ".join(disagreements)
        if closures:
            note += " " + " ".join(closures)
        if agreements:
            note += f" (agrees with: {', '.join(agreements)})."
        if missing_others:
            note += f" Not supplied: {', '.join(missing_others)}."
        return ReconciliationResult(category=category, sources=sources, agree=False, note=note)

    if closures:
        note = " ".join(closures)
        if agreements:
            note += f" Also agrees with: {', '.join(agreements)}."
        if missing_others:
            note += f" Not supplied: {', '.join(missing_others)}."
        return ReconciliationResult(category=category, sources=sources, agree=True, note=note)

    note = "All supplied sources agree with the LLP Statement of Account (the reference)."
    if missing_others:
        note += f" Not supplied: {', '.join(missing_others)}."
    return ReconciliationResult(category=category, sources=sources, agree=True, note=note)


def booked_current_account_closing(monthly: "list[MonthlyLine]", llp_record: dict | None):
    """H35-02: the current-account closing balance implied purely by the
    booked monthly figures, rolled forward from the L5 statement's own
    OPENING balance (this module has no I/O, so it has no other source for
    an opening balance) plus this FY's booked current-account movement --
    the sum of every month's prior_cohort_drawdown, which CREDITS (reduces)
    the balance the firm owes the partner (see MonthlyLine's docstring).
    Shared by the pre-accrual L5 tie-out row in build_report() and by
    residual_current_account_check() below, so the two can never desync.
    Returns None if llp_record is None or has no current_opening_balance.
    """
    l5_current_opening = llp_record.get("current_opening_balance") if llp_record else None
    if l5_current_opening is None:
        return None
    booked_movement = -sum(m.prior_cohort_drawdown for m in monthly) if monthly else 0.0
    return l5_current_opening + booked_movement


def year_end_accrual_diff(llp_record: dict | None, monthly: "list[MonthlyLine]") -> float | None:
    """The signed diff (L5 'Profit Share for the Year' minus the monthly
    total already booked for share_of_profit_income) that
    jv_emitter.build_accrual_journal() would post as the year-end accrual,
    or None if it cannot be computed (no L5, or the L5 profit-share field
    is missing) -- mirrors that function's own arithmetic exactly (it calls
    this same helper, H35-04 rework) so the two can never desync. This does
    NOT decide whether an actual journal is produced: build_accrual_journal()
    only ever books a Dr current_account / Cr share_of_profit_income entry
    when this comes back POSITIVE and beyond RECONCILIATION_TOLERANCE -- a
    negative diff is flagged for manual review, never booked, and callers
    (build_report(), for the "pending journal" mechanism above) must apply
    the same gate before treating this figure as something that will
    actually close a gap.
    """
    if llp_record is None:
        return None
    l5_profit_share = llp_record.get("current_profit_share")
    if l5_profit_share is None:
        return None
    booked_sop = sum(
        (m.share_of_profit_gross + m.firms_tax_sop + m.additional_share_of_profit)
        for m in monthly
    ) if monthly else 0.0
    return round(l5_profit_share - booked_sop, 2)


def residual_current_account_check(report: "Report", applied_accrual: float = 0.0) -> ReconciliationResult:
    """H35-02 item 4: after the year-end accrual (if any) is applied, compare
    the resulting current-account balance against the L5 statement's own
    current_closing_balance ONE more time.

    `applied_accrual` is the signed amount jv_emitter.build_accrual_journal()
    actually posted to current_account -- 0.0 whenever no accrual journal was
    produced (no L5 supplied, the L5 profit-share field was unparseable, the
    monthly total already ties, or the difference was negative and only
    flagged). Passing the wrong applied_accrual would silently mask or
    fabricate a residual, so callers must pass exactly what
    build_accrual_journal() returned, never a value computed independently.

    A residual beyond RECONCILIATION_TOLERANCE is reported as a VARIANCE and
    is NEVER booked automatically by this function or any other -- it only
    reports the difference, exactly like every other reconciliation row.
    """
    llp_record = getattr(report, "llp_record", None)
    category = "L5 tie-out: current-account closing balance after year-end accrual"
    if llp_record is None or llp_record.get("current_closing_balance") is None:
        return ReconciliationResult(
            category=category,
            sources={"Booked (monthly + accrual)": None, "LLP Statement (L5)": None},
            agree=None,
            note=(
                f"{CANNOT_RECONCILE} -- the LLP Statement of Account (L5) is "
                "required for this figure and was not supplied or could not "
                "be parsed."
            ),
        )
    booked_closing = booked_current_account_closing(report.monthly, llp_record)
    booked_after_accrual = (
        (booked_closing + applied_accrual) if booked_closing is not None else None
    )
    return reconcile_category(
        category,
        {"Booked (monthly + accrual)": booked_after_accrual,
         "LLP Statement (L5)": llp_record.get("current_closing_balance")},
    )


# ---------------------------------------------------------------------------
# Top-level report assembly.
# ---------------------------------------------------------------------------

@dataclass
class MonthlyLine:
    month: str
    remuneration: float
    share_of_profit_gross: float
    additional_share_of_profit: float
    firms_tax_sop: float    # firm's tax on the CURRENT year's share of profit
    firms_tax_other: float  # firm's tax on a PRIOR-year PLMI instalment drawn
    # down this month (never on the current year's SoP) -- see jv_emitter.py.
    tds: float
    capital_transferred: float
    total_paid: float
    misc: float
    # Stage 1b (jv_emitter.py) fields. Optional / default 0.0 so every
    # existing fixture and test keeps working unchanged -- see AGENT.md's
    # "Stage 1b" section for the accounting behind each one.
    interest_on_capital: float = 0.0   # POSITIVE, income (PGBP s.28(v))
    medical_topup: float = 0.0         # NEGATIVE, a recovery from the payout
    prior_cohort_drawdown: float = 0.0  # POSITIVE, a prior-year incentive
    # instalment RECEIVED this year: cash in, adds to total_paid, and
    # CREDITS (reduces) the current-account balance owed by the firm. NOT
    # current-year income -- the income and its firm's tax were already
    # recognised in the award year (see jv_emitter.py).


@dataclass
class Report:
    financial_year: str
    drivers: dict
    monthly: list[MonthlyLine]
    cohorts_raw: list[dict]
    cohort_instalments: list[InstalmentRow]
    one_offs: list[OneOffResult]
    capital_rule: CapitalRuleResult
    rate_change_suspects: list[RateChangeSuspect]
    tds_applicability: TdsApplicability
    tds_month_exceptions: list[str]
    reconciliation: list[ReconciliationResult]
    payroll: list[dict] = field(default_factory=list)
    # Stage 1b (jv_emitter.py) fields, both optional / default so every
    # existing fixture and test keeps working unchanged.
    firm_name: str = ""
    opening_reclass: dict | None = None
    # H35-02: the whole parsed L5 (LLP Statement of Account) dict, or None
    # if that optional leg was not supplied/could not be parsed -- see
    # parsers.llp_statement.parse_l5_words() for its shape. Consumed by
    # jv_emitter.build_accrual_journal() as well as the L5 tie-out rows
    # below, so it is carried on the Report rather than only used locally.
    llp_record: dict | None = None
    # H35-04 item B: every LOUD flag this run raised -- one line per
    # statement-referenced reconciliation row whose note starts with
    # "STATEMENT DISAGREES" (see statement_reference_row()), plus one line
    # per ERROR-level entry in llp_record["diagnostics"] (item C -- the
    # statement's own arithmetic, already computed by
    # parsers/llp_statement.py's _balance_check()/_section_sum_check(),
    # never re-derived here). Empty when the statement agrees with every
    # other source and its own arithmetic checks out (or no statement was
    # supplied at all). Consumed by agent.py (top of the text summary) and
    # writer.py (top of the Reconciliation sheet) to build the loud block.
    statement_flags: list[str] = field(default_factory=list)


def build_report(data: dict) -> Report:
    """The single entry point every caller (agent.run(), tests) should use.
    Takes the already-loaded structured-input dict (see skill.yaml / AGENT.md
    for the YAML/JSON shape) and returns a fully computed, writer-ready
    Report. Pure -- no I/O.
    """
    fy = data["financial_year"]
    drivers = data.get("drivers") or {}
    advisory = data.get("advisory") or {}
    external = data.get("external") or {}
    payroll = data.get("payroll") or []
    # H35-02: the L5 (LLP Statement of Account) leg, whole. None if not
    # supplied/unparseable -- every L5-dependent row below must fail loud
    # in that case, never substitute a computed figure.
    llp_record = data.get("llp_record")

    firms_tax_rate, _ = driver(drivers, "firms_tax_rate", fy, "firm's tax rate")

    monthly: list[MonthlyLine] = []
    one_offs: list[OneOffResult] = []
    tds_month_exceptions: list[str] = []

    tds_app = remuneration_tds_applicability(drivers, fy)

    for m in data.get("monthly", []):
        misc = derive_misc(
            total_paid=m["total_paid"], remuneration=m["remuneration"],
            share_of_profit_gross=m["share_of_profit_gross"],
            additional_share_of_profit=m.get("additional_share_of_profit", 0.0) or 0.0,
        )
        monthly.append(MonthlyLine(
            month=m["month"], remuneration=m["remuneration"],
            share_of_profit_gross=m["share_of_profit_gross"],
            additional_share_of_profit=m.get("additional_share_of_profit", 0.0) or 0.0,
            firms_tax_sop=m.get("firms_tax_sop", 0.0) or 0.0,
            firms_tax_other=m.get("firms_tax_other", 0.0) or 0.0,
            tds=m.get("tds", 0.0) or 0.0,
            capital_transferred=m.get("capital_transferred", 0.0) or 0.0,
            total_paid=m["total_paid"], misc=misc,
            interest_on_capital=m.get("interest_on_capital", 0.0) or 0.0,
            medical_topup=m.get("medical_topup", 0.0) or 0.0,
            prior_cohort_drawdown=m.get("prior_cohort_drawdown", 0.0) or 0.0,
        ))
        addl = m.get("additional_share_of_profit", 0.0) or 0.0
        if addl:
            one_offs.append(gross_up_one_off(addl, firms_tax_rate, fy))

        if tds_app.status == "OK" and tds_app.start_date is not None:
            applicable = tds_app.applicable_on(m["month"] + "-01")
            month_tds = m.get("tds", 0.0) or 0.0
            if applicable is False and month_tds != 0:
                tds_month_exceptions.append(
                    f"{m['month']}: TDS of {month_tds:,.2f} deducted before the "
                    f"configured remuneration-TDS start date {tds_app.start_date}."
                )

    capital_rule = required_cumulative_capital(
        target_compensation=drivers.get("target_compensation"),
        months_achieved=drivers.get("capital_months_achieved"),
        months_total=drivers.get("capital_months_total"),
        rate=drivers.get("capital_rate"),
        fy=fy,
    )

    cohorts_raw = data.get("cohorts", [])
    cohort_instalments: list[InstalmentRow] = []
    rate_change_suspects: list[RateChangeSuspect] = []
    for cohort in cohorts_raw:
        cohort_instalments.extend(classify_cohort_instalments(cohort, fy))
        capital_instalments = [i for i in cohort.get("instalments", [])
                                if i.get("capital") is not None]
        capitals = [abs(i["capital"]) for i in capital_instalments]
        grosses = [abs(i["gross"]) for i in capital_instalments if i.get("gross") is not None]
        if len(grosses) != len(capitals):
            grosses = None
        suspect = detect_mid_year_rate_change(
            capitals, drivers.get("target_compensation"),
            drivers.get("capital_months_achieved"), drivers.get("capital_months_total"),
            instalment_grosses=grosses,
        )
        if suspect is not None:
            rate_change_suspects.append(suspect)

    # ---- Reconciliation categories --------------------------------------
    reconciliation: list[ReconciliationResult] = []

    reporting_instalments = [i for i in cohort_instalments if i.membership == "reporting"]
    # The cohort instalments' net figures are NOT added here: the payment
    # schedule's total_payout row (monthly.total_paid) is already inclusive
    # of any PLMI instalment paid that month -- the cohort ledger is built
    # from that same schedule row (mapper.py), so adding the cohort net on
    # top double-counts cash the monthly total already contains. The
    # monthly payouts alone ARE the cash received.
    total_monthly_paid = sum(m.total_paid for m in monthly) if monthly else None
    # H35-04 rework item C (D2 -- corrected): external["bank_credits_total"]
    # is meant to be the PARTNER's own bank statement total credit for the
    # cash received -- no parser or document flow in this skill produces
    # that figure yet, and none is asked for by skill.yaml either.
    # gnucash_tieout.py's build_balance_tieout() "bank" leg (wired
    # separately, below, into a ReconciliationResult appended onto
    # report.reconciliation by agent.py) looks similar but is NOT wired in
    # as a substitute here: it reads the PARTNER's OWN GnuCash book (the
    # accounts.bank path is configured on the same book as
    # accounts.current_account, accounts.medical_expense etc -- every
    # ACCOUNT_KEYS entry is a partner-personal ledger account, never a
    # firm-consolidated one; an earlier draft of this note wrongly called
    # this "the firm's own book" -- it is not) against this run's implied
    # journal -- a plausible source for this row, but a FUZZY MATCH of
    # individual payouts to individual bank credits (to rule out double
    # booking, e.g. a prior-cohort drawdown landing in the same bank credit
    # as a monthly payout) is needed before it can be wired in safely, and
    # that match is H35-05's job, not this one's. This row is therefore
    # NOT a genuine gap and must never read as one: it is simply not
    # checked yet, regardless of whether bank_credits_total happens to be
    # supplied. It is never a failure (agree is never False here) and never
    # enters the LOUD block (see statement_flags assembly below, which only
    # fires on a "STATEMENT DISAGREES"-prefixed note).
    bank_total, _ = field_or_reason(external, "bank_credits_total", "bank credits total")
    reconciliation.append(ReconciliationResult(
        category="Total cash received (monthly payouts) vs Bank",
        sources={"Computed (monthly payouts)": total_monthly_paid, "Bank statement": bank_total},
        agree=None,
        note=(
            f"{NOT_CHECKED_YET}. Computed (monthly payouts): "
            f"{total_monthly_paid if total_monthly_paid is not None else 'not available'}; "
            f"Bank statement: {bank_total if bank_total is not None else 'not supplied'}. "
            "See H35-05 (partner-side fuzzy match of payouts to bank credits)."
        ),
    ))

    total_sop = sum(m.share_of_profit_gross for m in monthly) if monthly else None
    return_exempt_sop, _ = field_or_reason(external, "return_exempt_share_of_profit",
                                            "return's exempt share of profit")
    # H35-02 / user ruling ("Y-3 should come from the Statement of account
    # as the final say"): this row compares the L5 LLP Statement of
    # Account's own "Profit Share for the Year" figure against the filed
    # return -- NEVER the monthly total (total_sop, above, still feeds
    # jv_emitter's monthly journal, but is not an acceptable substitute
    # here). Absent/unparseable L5 is a fail-loud placeholder naming the
    # L5 statement as required, not a silent fallback to total_sop.
    if llp_record is not None and llp_record.get("current_profit_share") is not None:
        reconciliation.append(statement_reference_row(
            "Exempt share of profit (s.10(2A)) vs the filed return",
            llp_record["current_profit_share"], "L5 Statement (Profit Share for the Year)",
            {"Return": return_exempt_sop},
        ))
    else:
        reconciliation.append(ReconciliationResult(
            category="Exempt share of profit (s.10(2A)) vs the filed return",
            sources={"L5 Statement (Profit Share for the Year)": None,
                     "Return": return_exempt_sop},
            agree=None,
            note=(
                f"{CANNOT_RECONCILE} -- the LLP Statement of Account (L5) is "
                "required for this row and was not supplied or could not be "
                "parsed. The monthly total is never substituted here, even "
                "though it is available -- see AGENT.md/H35-02. No year-end "
                "accrual journal is produced either, for the same reason."
            ),
        ))

    advisory_closing, _ = field_or_reason(advisory, "stated_closing_capital",
                                           "Advisory's stated closing capital")
    return_closing, _ = field_or_reason(external, "return_closing_capital",
                                         "return's closing capital")
    # H35-04 item A: the LLP Statement of Account (L5), when supplied, is
    # the REFERENCE for this row -- the rule/Advisory/Return are each
    # measured against it ("Statement says X; <source> says Y; difference
    # Z"), never averaged in as equal peers the way this row used to work
    # (H35-02 added the L5 figure as a fourth equal source; H35-04 changes
    # that). Absent a statement, this falls back to the old three-way
    # equal-peers comparison unchanged, with a plain note that no statement
    # was supplied.
    reconciliation.append(statement_reference_row(
        "Closing capital: rule vs Advisory vs the filed return",
        llp_record.get("capital_closing_balance") if llp_record is not None else None,
        "LLP Statement (L5)",
        {"Rule (Drivers)": capital_rule.required_cumulative_capital,
         "Advisory": advisory_closing, "Return": return_closing},
    ))

    # H35-02 item 1: three further L5 tie-out rows -- current-account
    # closing balance, remuneration for the year, and interest on capital.
    # "Booked (monthly)" is computed purely from `monthly` -- the same
    # totals jv_emitter.py's monthly journal would post -- so it means
    # exactly "the booked figure (existing monthly journal totals)" per
    # the H35-02 instruction; it is NOT a live GnuCash book read (this
    # module is pure, no I/O). A missing/unparseable L5 fails loud on
    # every one of these rows rather than being silently omitted.
    l5_required_note = (
        f"{CANNOT_RECONCILE} -- the LLP Statement of Account (L5) is required "
        "for this figure and was not supplied or could not be parsed; no "
        "other document substitutes for the L5 closing figures."
    )

    def _l5_tieout_row(category: str, booked, l5_key: str,
                        pending_journal: dict | None = None) -> ReconciliationResult:
        if llp_record is None:
            return ReconciliationResult(
                category=category,
                sources={"Booked (monthly)": booked, "LLP Statement (L5)": None},
                agree=None, note=l5_required_note,
            )
        # H35-04 item A: the L5 figure is the reference here too -- a
        # disagreement is reported as "Statement says X; Booked (monthly)
        # says Y; difference Z", not a generic two-way variance.
        return statement_reference_row(
            category, llp_record.get(l5_key), "LLP Statement (L5)",
            {"Booked (monthly)": booked},
            pending_journal=pending_journal,
        )

    # H35-04 item A (sweep, current-account-closing row): of the three L5
    # tie-out rows below, only this one can be affected by a not-yet-posted
    # journal this skill itself produces -- the year-end share-of-profit
    # accrual posts to current_account/share_of_profit_income, which is
    # exactly what "Booked (monthly)" measures here. Remuneration and
    # interest-on-capital (further below) have no accrual-driven book-side
    # effect and are deliberately NOT given a pending_journal argument.
    # year_end_accrual_diff() mirrors jv_emitter.build_accrual_journal()'s
    # own diff arithmetic exactly, so the two can never desync; a negative
    # diff is a manual-review case there (never booked), so it is never
    # offered as a closing pending journal here either.
    accrual_diff = year_end_accrual_diff(llp_record, monthly)
    pending_accrual = None
    if accrual_diff is not None and accrual_diff > RECONCILIATION_TOLERANCE:
        accrual_txn_id = journal_txn_id(
            fy_prefix(fy), data.get("firm_name", "") or "", "ACCR"
        )
        pending_accrual = {
            "applies_to": "Booked (monthly)",
            "amount": accrual_diff,
            "journal_ids": [accrual_txn_id],
            "description": "this skill's year-end share-of-profit accrual journal",
        }

    booked_current_closing = booked_current_account_closing(monthly, llp_record)
    reconciliation.append(_l5_tieout_row(
        "L5 tie-out: current-account closing balance",
        booked_current_closing, "current_closing_balance",
        pending_journal=pending_accrual,
    ))

    booked_remuneration = sum(m.remuneration for m in monthly) if monthly else None
    reconciliation.append(_l5_tieout_row(
        "L5 tie-out: remuneration for the year",
        booked_remuneration, "current_remuneration",
    ))

    booked_interest_on_capital = sum(m.interest_on_capital for m in monthly) if monthly else None
    reconciliation.append(_l5_tieout_row(
        "L5 tie-out: interest on capital",
        booked_interest_on_capital, "capital_interest_on_capital",
    ))

    total_tds_credit = -sum(m.tds for m in monthly) if monthly else None
    form_26as, _ = field_or_reason(external, "form_26as_total_credit", "Form 26AS total credit")
    reconciliation.append(reconcile_category(
        "TDS credit: computed (monthly remuneration TDS) vs Form 26AS",
        {"Computed (monthly TDS)": total_tds_credit, "Form 26AS": form_26as},
    ))

    # H35-04 item B (D1, corrected identity): the current account's
    # Drawings, per the L5 statement, is:
    #     net payouts + TDS
    #         + other payslip deductions paid on the partner's behalf
    #           (medical top-up -- the only such distinctly-named field the
    #           payout model carries; other recoveries fold into the
    #           generic derived "misc" balancing figure, not a separately
    #           named field, so there is nothing else to add here)
    #         - interest on capital paid out, GROSS
    # Interest on capital is credited to/drawn from the CAPITAL column of
    # the statement, not the current account, so it must be backed OUT of
    # a current-account drawings identity; it arrives in the payouts net of
    # its own s.194T TDS (that TDS is already inside `total_tds_credit`
    # above), so the GROSS figure must be the one subtracted -- subtracting
    # the net figure would double-count that TDS and understate the
    # residual. `booked_interest_on_capital` (computed above for the L5
    # interest-on-capital tie-out row) IS already the gross figure --
    # mapper._place_interest_on_capital() places the L5 statement's own
    # printed capital_interest_on_capital straight onto MonthlyLine.
    # interest_on_capital, never netted for TDS -- so no gross-up
    # computation is needed here, only the subtraction itself.
    # medical_topup is NEGATIVE on MonthlyLine (a recovery from the
    # payout), so its magnitude (the amount actually withheld from cash and
    # paid on the partner's behalf) is added back with a sign flip.
    #
    # Sign note: the L5 statement prints Drawings parenthesised (negative
    # -- see parsers/llp_statement.py's module docstring and
    # _balance_check()'s "withdrawals are already negative" convention),
    # while the identity above is a positive cash-out figure. The statement
    # value is negated here purely to compare magnitudes on the same sign
    # -- statement_reference_row() still receives the statement's own
    # printed sign as the displayed reference value would be misleading,
    # so the negated (positive, "cash drawn") figure is what is shown and
    # compared, named accordingly.
    medical_and_other_deductions = (
        -sum(m.medical_topup for m in monthly) if monthly else None
    )
    identity_components_known = (
        total_monthly_paid is not None
        and total_tds_credit is not None
        and medical_and_other_deductions is not None
        and booked_interest_on_capital is not None
    )
    payouts_plus_adjustments = (
        total_monthly_paid + total_tds_credit + medical_and_other_deductions
        - booked_interest_on_capital
        if identity_components_known
        else None
    )
    statement_drawings_raw = (
        llp_record.get("current_drawings") if llp_record is not None else None
    )
    statement_drawings_magnitude = (
        -statement_drawings_raw if statement_drawings_raw is not None else None
    )
    _d1_row = statement_reference_row(
        "Current-account drawings: statement vs (net monthly payouts + TDS + "
        "other payslip deductions - gross interest on capital)",
        statement_drawings_magnitude, "LLP Statement (L5) Drawings (magnitude, as cash drawn)",
        {"Net monthly payouts + TDS + other deductions - gross interest on capital":
             payouts_plus_adjustments},
    )
    if identity_components_known:
        _d1_row.note += (
            " Build-up: net monthly payouts "
            f"{total_monthly_paid:,.2f} + TDS withheld {total_tds_credit:,.2f} "
            f"+ other payslip deductions (medical top-up) "
            f"{medical_and_other_deductions:,.2f} - gross interest on capital "
            f"{booked_interest_on_capital:,.2f} = {payouts_plus_adjustments:,.2f}."
        )
    reconciliation.append(_d1_row)

    # The cohort instalments' firms_tax is NOT added here: mapper.py sources
    # each cohort instalment's firms_tax from the same payment-schedule row
    # (firm_tax_others) that already feeds the monthly line's
    # firms_tax_other -- adding the cohort term would read that row twice.
    total_firms_tax = (
        sum(m.firms_tax_sop + m.firms_tax_other for m in monthly) if monthly else 0.0
    )
    conflation_note = firms_tax_conflated_with_26as(total_firms_tax, form_26as, total_tds_credit)
    reconciliation.append(ReconciliationResult(
        category="Firm's tax on share of profit is absent from Form 26AS",
        sources={"Firm's tax total": total_firms_tax, "Form 26AS total credit": form_26as},
        agree=(conflation_note is None) if form_26as is not None else None,
        note=conflation_note or (
            "No conflation detected." if form_26as is not None
            else f"{CANNOT_RECONCILE} -- Form 26AS total credit not supplied"
        ),
    ))

    # Leg 2: the award-year Compensation Advisory's own schedule_instalments
    # gross total (see mapper.py -- advisory["schedule_instalments_gross_total"])
    # cross-checked against this reporting FY's cohort ledger (the payment
    # schedule's "Previous Year PLMIs" row, assembled into `cohorts` by
    # mapper._build_cohorts()). This can only be compared when the Advisory
    # supplied is the one issued for the cohort's AWARD year -- an Advisory
    # for any other year describes a different cohort's instalments
    # entirely, so a mismatch there is a missing-document CANNOT RECONCILE
    # naming the specific award-year Advisory that is missing, never a
    # silent comparison against the wrong year's figures.
    # H35-04 item E: this category is SUPERSEDED by D1's drawings-vs-
    # payouts-plus-TDS identity above, which reconciles the same "did the
    # cash the partner actually drew match what should have gone out"
    # question directly against the statement (the gospel-truth reference)
    # instead of only cross-checking the Advisory's award-year schedule
    # against the payment schedule's own cohort ledger -- two sources that
    # both originate upstream of the statement. It is NOT retired outright:
    # several existing tests assert specific agree/note values for this
    # exact category (test_reconciliation_incentive_instalments_*), so
    # removing it would drop real coverage for a still-useful cross-check
    # (advisory vs schedule) that D1 does not replace one-for-one. It is
    # demoted to informational instead -- every branch's note below is
    # prefixed to say so plainly, without changing `agree`/`sources`/the
    # rest of the note (existing substring assertions keep passing).
    _informational_prefix = (
        "INFORMATIONAL (superseded by the D1 drawings-vs-payouts identity "
        "check, H35-04) -- "
    )
    category_name = "Incentive instalments: award-year Advisory vs payment schedule"
    if not cohorts_raw:
        reconciliation.append(ReconciliationResult(
            category=category_name,
            sources={"Award-year Advisory (schedule_instalments)": None,
                     "Payment-schedule cohort ledger": None},
            agree=None,
            note=_informational_prefix +
                 f"{CANNOT_RECONCILE} -- no cohort (Previous Year PLMIs) data "
                 "supplied for this FY.",
            informational=True,
        ))
    else:
        award_fy = cohorts_raw[0]["award_fy"]
        advisory_fy = advisory.get("financial_year")
        cohort_gross_total = sum(
            i.gross for i in reporting_instalments if i.gross is not None
        ) if reporting_instalments else 0.0
        if advisory_fy != award_fy:
            reconciliation.append(ReconciliationResult(
                category=category_name,
                sources={"Award-year Advisory (schedule_instalments)": None,
                         "Payment-schedule cohort ledger": cohort_gross_total},
                agree=None,
                note=_informational_prefix +
                     f"{CANNOT_RECONCILE} -- the cohort's award year is FY{award_fy}, "
                     f"but the Compensation Advisory supplied is for FY"
                     f"{advisory_fy or '<none supplied>'}; the FY{award_fy} Advisory "
                     "is missing and its schedule_instalments cannot be substituted "
                     "from any other year.",
                informational=True,
            ))
        else:
            advisory_gross_total = advisory.get("schedule_instalments_gross_total")
            informational_row = reconcile_category(
                category_name,
                {"Award-year Advisory (schedule_instalments)": advisory_gross_total,
                 "Payment-schedule cohort ledger": cohort_gross_total},
            )
            informational_row.note = _informational_prefix + (informational_row.note or "")
            informational_row.informational = True
            reconciliation.append(informational_row)

    # ---- H35-04 item B: assemble the LOUD block ---------------------------
    # One line per statement-referenced row that disagrees with the L5
    # statement (its note starts with "STATEMENT DISAGREES" -- see
    # statement_reference_row()), plus one line per ERROR-level entry in
    # the L5 parser's own arithmetic diagnostics (item C -- surfaced here,
    # never re-derived). Empty when the statement agrees everywhere and its
    # own arithmetic checks out (or no statement was supplied at all).
    statement_flags: list[str] = []
    for r in reconciliation:
        if r.informational:
            continue
        if r.agree is False and r.note.startswith("STATEMENT DISAGREES"):
            statement_flags.append(f"{r.category}: {r.note}")
    if llp_record is not None:
        for line in llp_record.get("diagnostics", []) or []:
            if line.startswith("ERROR:"):
                statement_flags.append(f"Statement arithmetic -- {line}")

    return Report(
        financial_year=fy, drivers=drivers, monthly=monthly, cohorts_raw=cohorts_raw,
        cohort_instalments=cohort_instalments, one_offs=one_offs, capital_rule=capital_rule,
        rate_change_suspects=rate_change_suspects, tds_applicability=tds_app,
        tds_month_exceptions=tds_month_exceptions, reconciliation=reconciliation,
        payroll=payroll,
        firm_name=data.get("firm_name", "") or "",
        opening_reclass=data.get("opening_reclass"),
        llp_record=llp_record,
        statement_flags=statement_flags,
    )
