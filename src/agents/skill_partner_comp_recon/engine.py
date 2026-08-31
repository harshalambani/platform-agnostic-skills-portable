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

from dataclasses import dataclass, field
from datetime import date, datetime

# ---------------------------------------------------------------------------
# Small shared plumbing.
# ---------------------------------------------------------------------------

CANNOT_RECONCILE = "CANNOT RECONCILE"


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
                                 months_achieved, months_total) -> RateChangeSuspect | None:
    """See spec s.5.2. If a cohort's instalment capital-deducted figures are
    not all equal (beyond a 1-rupee tolerance), that asymmetry is the
    fingerprint of the firm changing the capital rate part-way through the
    cohort -- never a rounding artefact, and never smoothed over.

    Returns None (no exception) when every instalment's capital matches
    the first within tolerance, or when the inputs needed to compute the
    implied rates are missing (the asymmetry is still real, but the
    implied-rate figures cannot be computed -- callers should still surface
    the raw asymmetry as an open item in that case).
    """
    if not instalment_capitals:
        return None
    first = instalment_capitals[0]
    if all(abs(c - first) <= CAPITAL_TOLERANCE for c in instalment_capitals):
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


def reconcile_category(category: str, sources: dict, tolerance: float = 1.0) -> ReconciliationResult:
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


# ---------------------------------------------------------------------------
# Top-level report assembly.
# ---------------------------------------------------------------------------

@dataclass
class MonthlyLine:
    month: str
    remuneration: float
    share_of_profit_gross: float
    additional_share_of_profit: float
    firms_tax: float
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
            firms_tax=m.get("firms_tax", 0.0) or 0.0, tds=m.get("tds", 0.0) or 0.0,
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
        capitals = [abs(i["capital"]) for i in cohort.get("instalments", [])
                    if i.get("capital") is not None]
        suspect = detect_mid_year_rate_change(
            capitals, drivers.get("target_compensation"),
            drivers.get("capital_months_achieved"), drivers.get("capital_months_total"),
        )
        if suspect is not None:
            rate_change_suspects.append(suspect)

    # ---- Reconciliation categories --------------------------------------
    reconciliation: list[ReconciliationResult] = []

    reporting_instalments = [i for i in cohort_instalments if i.membership == "reporting"]
    total_monthly_paid = sum(m.total_paid for m in monthly) if monthly else None
    total_instalment_net = (sum(i.net for i in reporting_instalments if i.net is not None)
                             if reporting_instalments else 0.0)
    total_received = (None if total_monthly_paid is None
                       else total_monthly_paid + total_instalment_net)
    bank_total, _ = field_or_reason(external, "bank_credits_total", "bank credits total")
    reconciliation.append(reconcile_category(
        "Total cash received (monthly payouts + in-FY cohort instalments) vs Bank",
        {"Computed (monthly + cohort)": total_received, "Bank statement": bank_total},
    ))

    total_sop = sum(m.share_of_profit_gross for m in monthly) if monthly else None
    return_exempt_sop, _ = field_or_reason(external, "return_exempt_share_of_profit",
                                            "return's exempt share of profit")
    reconciliation.append(reconcile_category(
        "Exempt share of profit (s.10(2A)) vs the filed return",
        {"Computed (monthly)": total_sop, "Return": return_exempt_sop},
    ))

    advisory_closing, _ = field_or_reason(advisory, "stated_closing_capital",
                                           "Advisory's stated closing capital")
    return_closing, _ = field_or_reason(external, "return_closing_capital",
                                         "return's closing capital")
    reconciliation.append(reconcile_category(
        "Closing capital: rule vs Advisory vs the filed return",
        {"Rule (Drivers)": capital_rule.required_cumulative_capital,
         "Advisory": advisory_closing, "Return": return_closing},
    ))

    total_tds_credit = -sum(m.tds for m in monthly) if monthly else None
    form_26as, _ = field_or_reason(external, "form_26as_total_credit", "Form 26AS total credit")
    reconciliation.append(reconcile_category(
        "TDS credit: computed (monthly remuneration TDS) vs Form 26AS",
        {"Computed (monthly TDS)": total_tds_credit, "Form 26AS": form_26as},
    ))

    total_firms_tax = (sum(m.firms_tax for m in monthly) if monthly else 0.0) + sum(
        (i.firms_tax or 0.0) for i in reporting_instalments
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

    # Leg 2 (the firm's payment-schedule PDF) is never independently
    # available in this build -- parsers/payment_schedule.py is a Stage 2
    # placeholder (see AGENT.md). The cohort ledger from the structured
    # input is the only source of the incentive schedule this run has, so
    # it cannot be cross-checked against itself.
    reconciliation.append(ReconciliationResult(
        category="Incentive schedule (payment-schedule PDF) vs cohort ledger",
        sources={"Payment-schedule PDF": None,
                 "Cohort ledger (structured input)": sum(
                     i.gross for i in reporting_instalments if i.gross is not None
                 ) if reporting_instalments else 0.0},
        agree=None,
        note=f"{CANNOT_RECONCILE} -- payment-schedule PDF parsing is a Stage 2 "
             "placeholder (see parsers/payment_schedule.py); the cohort ledger "
             "supplied as structured input is this run's only source.",
    ))

    return Report(
        financial_year=fy, drivers=drivers, monthly=monthly, cohorts_raw=cohorts_raw,
        cohort_instalments=cohort_instalments, one_offs=one_offs, capital_rule=capital_rule,
        rate_change_suspects=rate_change_suspects, tds_applicability=tds_app,
        tds_month_exceptions=tds_month_exceptions, reconciliation=reconciliation,
        payroll=payroll,
        firm_name=data.get("firm_name", "") or "",
        opening_reclass=data.get("opening_reclass"),
    )
