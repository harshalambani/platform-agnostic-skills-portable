"""
advance_tax.py -- mid-year Advance Tax Estimator (2026-08 advance-tax-estimator
work, PR1/engine).

Central design rule: there is exactly ONE tax core in this codebase
(tax_core.compute_tax_on), shared by the year-end ITR workbook
(schedules.compute_tax, a thin re-export) and this estimator. Nothing here
reimplements slab/surcharge/cess arithmetic.

Two populators build the same `EstimateInput` contract:
  * MANUAL  -- the caller constructs EstimateInput directly. No GnuCash book,
    mapping, registry, or Data/ access of any kind; works standalone.
  * FROM BOOK -- `from_book()` below, built on top of the existing
    schedules.py builders (build_salary/build_business/build_house_property/
    build_other_sources/build_capital_gains/build_taxes_paid), reading a
    caller-supplied `resolved`/`node_by_guid`/`book` for FY-to-date actuals.
    Every field it fills is still a plain, editable EstimateInput field --
    the populator does no additional resolution of its own.

Projection is explicit, never silent: each recurring head (salary,
remuneration, rent, interest) is a `ProjectedHead` with `actual_to_date` and
an optional `projected_remainder` that starts BLANK (None) and stays blank
--  and loudly flagged via `EstimateInput.unprojected_heads()` -- until the
caller fills it in, either by hand or via `annualise()` (a visible,
straight-line helper; never a silent extrapolation baked into a total).

Capital gains are NEVER projected -- `CapitalGainItem` carries only actuals
to date. An item with no `sale_date` is flagged (see
`EstimateInput.undated_capital_gains()`) and, mirroring
schedules.cg_proviso_exclusions's own "no rows -> no relief" convention,
receives NO s.234C first-proviso relief -- it stays in every instalment's
base, which is the conservative direction (granting relief that cannot be
evidenced would understate the charge), not "silently first-quarter"
(which would instead ASSUME the earliest possible date and actively
overstate the charge).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import interest_234
import rules as rules_engine
import schedules as sch
import tax_core
from parse_gnucash import Book, fy_window

#: Capital-gains taxonomy, matching schedules.py's own vocabulary (LT ==
#: s.112A 112A-equity-STT LTCG, ST == s.111A equity-STT STCG). OTHER covers
#: anything special-rate-adjacent that the estimator does not itself
#: special-case (e.g. non-STT LTCG/STCG, s.112 unlisted, slump sale) --
#: rather than inventing a rate table for every possible head, an OTHER item
#: is folded into slab (normal) income, which is the conservative choice
#: whenever the true special rate would be lower.
GAIN_TYPES = ("STCG_111A", "LTCG_112A", "OTHER")

#: The four s.234C statutory instalment due dates, by (month, day) --
#: mirrors interest_234.S234C_INSTALMENTS and schedules.cg_proviso_exclusions.
_INSTALMENT_MONTH_DAY = ((6, 15), (9, 15), (12, 15), (3, 15))


# ---------------------------------------------------------------------------
# Input contract
# ---------------------------------------------------------------------------

@dataclass
class CapitalGainItem:
    """One capital-gains item, actuals-to-date only -- never projected."""
    amount: float                 # taxable gain (already net of cost/exemption)
    gain_type: str                # one of GAIN_TYPES
    sale_date: date | None = None
    note: str | None = None

    @property
    def undated(self) -> bool:
        return self.sale_date is None


@dataclass
class ProjectedHead:
    """One recurring income head. `actual_to_date` is the figure booked so
    far this FY; `projected_remainder` is the estimate for the rest of the
    year and starts blank (None) -- it is NEVER silently defaulted to 0 or
    guessed. Use `annualise()` to fill it visibly via straight-line
    extrapolation, or set it by hand."""
    actual_to_date: float = 0.0
    projected_remainder: float | None = None

    @property
    def unprojected(self) -> bool:
        return self.projected_remainder is None

    @property
    def full_year(self) -> float:
        return self.actual_to_date + (self.projected_remainder or 0.0)


@dataclass
class EstimateInput:
    entity_name: str
    fy: str                       # canonical income-year key, e.g. "2025-26"
    regime: str                   # "old" | "new" -- both are always computed
                                   # side by side regardless of this default
    status: str = "Individual"
    dob: str | None = None
    residency: str | None = None
    as_of: date = field(default_factory=date.today)

    salary: ProjectedHead = field(default_factory=ProjectedHead)
    remuneration: ProjectedHead = field(default_factory=ProjectedHead)   # PGBP: firm
                                   # remuneration + interest on capital, s.28(v)
    rent: ProjectedHead = field(default_factory=ProjectedHead)           # house property,
                                   # net income (post s.24 deductions) -- see
                                   # design note in from_book()
    interest: ProjectedHead = field(default_factory=ProjectedHead)       # other-sources interest

    other_sources_other: float = 0.0   # dividend etc. -- actual-to-date only,
                                   # not one of the projected recurring heads
    capital_gains: list[CapitalGainItem] = field(default_factory=list)

    deductions_via: float = 0.0
    tds_to_date: float = 0.0
    advance_tax_paid: list[tuple] = field(default_factory=list)  # [(date, amount), ...]

    def unprojected_heads(self) -> list[str]:
        """Names of recurring heads still missing a projected remainder --
        surfaced as a loud flag, never silently treated as zero."""
        heads = {
            "salary": self.salary, "remuneration": self.remuneration,
            "rent": self.rent, "interest": self.interest,
        }
        return [name for name, head in heads.items() if head.unprojected]

    def undated_capital_gains(self) -> list[CapitalGainItem]:
        return [item for item in self.capital_gains if item.undated]


def annualise(input_data: EstimateInput) -> EstimateInput:
    """Visible straight-line "annualise" helper: for every recurring head
    still unprojected, fill `projected_remainder` as
    actual_to_date * (days_remaining / days_elapsed) -- a simple, explicit
    extrapolation the caller can see and override, never a hidden default.
    Heads that already carry a projected_remainder (including an explicit
    0.0) are left untouched. Returns a NEW EstimateInput; does not mutate
    the one passed in."""
    fy_start, fy_end = fy_window(input_data.fy)
    as_of = input_data.as_of
    days_elapsed = max(1, (as_of - fy_start).days + 1)
    days_remaining = max(0, (fy_end - as_of).days)
    factor = days_remaining / days_elapsed

    def _annualised(head: ProjectedHead) -> ProjectedHead:
        if not head.unprojected:
            return head
        return ProjectedHead(
            actual_to_date=head.actual_to_date,
            projected_remainder=head.actual_to_date * factor,
        )

    return EstimateInput(
        entity_name=input_data.entity_name, fy=input_data.fy, regime=input_data.regime,
        status=input_data.status, dob=input_data.dob, residency=input_data.residency,
        as_of=input_data.as_of,
        salary=_annualised(input_data.salary),
        remuneration=_annualised(input_data.remuneration),
        rent=_annualised(input_data.rent),
        interest=_annualised(input_data.interest),
        other_sources_other=input_data.other_sources_other,
        capital_gains=list(input_data.capital_gains),
        deductions_via=input_data.deductions_via,
        tds_to_date=input_data.tds_to_date,
        advance_tax_paid=list(input_data.advance_tax_paid),
    )


# ---------------------------------------------------------------------------
# FROM BOOK populator
# ---------------------------------------------------------------------------

def from_book(
    resolved: dict, node_by_guid: dict, book: Book | None, fy: str,
    rules: rules_engine.RulesConfig, entity_name: str, regime: str, status: str,
    dob: str | None, scrips: dict | None = None, fmv_tables=None,
    residency: str | None = None, as_of: date | None = None,
    foreign_report=None, foreign_dividends_in_book: bool = False,
) -> EstimateInput:
    """Pre-fill an EstimateInput from a registered book's FY-to-date actuals,
    reusing the existing schedule builders -- fully editable afterwards, and
    every projected_remainder is left blank (None), same as the manual path.

    DESIGN NOTE (brief silent on mechanism): `rent` maps to the house-property
    schedule's already-net `income` figure (GAV - municipal tax - std
    deduction - s.24b interest), not gross rent -- schedules.py's own
    HousePropertySchedule models the head that way, and re-deriving gross
    rent here would duplicate that computation rather than reuse it.

    `foreign_report`/`foreign_dividends_in_book` (2026-08-19): optional,
    minimal extension so the advance-tax "interest" projection line also
    picks up an included foreign dividend, same resolution the ITR workbook
    itself uses (entity.foreign_dividends_in_book_by_ay.get(ay, entity.
    foreign_dividends_in_book)) -- no caller in this codebase passes them yet
    since no advance-tax UI currently loads a foreign broker report, but the
    seam is real (build_other_sources is reused as-is) and worth exposing now
    rather than only testing build_other_sources() in isolation.
    """
    salary = sch.build_salary(resolved, node_by_guid, None, rules, regime)
    business = sch.build_business(resolved, node_by_guid)
    house_property = sch.build_house_property(resolved, node_by_guid, rules)
    other_sources = sch.build_other_sources(
        resolved, node_by_guid, book, fy, rules, foreign_report=foreign_report,
        foreign_dividends_in_book=foreign_dividends_in_book,
    )
    taxes_paid = sch.build_taxes_paid(resolved, node_by_guid, rules, None, book, fy)

    capital_gains: list[CapitalGainItem] = []
    if scrips is not None and fmv_tables is not None:
        cg_schedule = sch.build_capital_gains(resolved, node_by_guid, book, fy, rules, scrips, fmv_tables)
        for row in cg_schedule.lot_rows:
            gain_type = "LTCG_112A" if row.term == "LT" else "STCG_111A"
            capital_gains.append(
                CapitalGainItem(amount=row.taxable_gain, gain_type=gain_type, sale_date=row.sale_date)
            )

    return EstimateInput(
        entity_name=entity_name, fy=fy, regime=regime, status=status, dob=dob,
        residency=residency, as_of=as_of or date.today(),
        salary=ProjectedHead(actual_to_date=salary.income_chargeable),
        remuneration=ProjectedHead(actual_to_date=business.net),
        rent=ProjectedHead(actual_to_date=house_property.income),
        interest=ProjectedHead(
            actual_to_date=other_sources.interest_sb + other_sources.interest_bank
            + other_sources.interest_nbfc + other_sources.interest_epf_taxable
        ),
        other_sources_other=(
            other_sources.refund_interest + other_sources.dividend_gross + other_sources.slbs
        ),
        capital_gains=capital_gains,
        tds_to_date=taxes_paid.tds_salary + taxes_paid.tds_interest + taxes_paid.tds_dividend + taxes_paid.tcs,
    )


# ---------------------------------------------------------------------------
# Capital-gains tax (per item, exact -- no ratio apportionment needed since
# each item already carries its own amount/gain_type/sale_date)
# ---------------------------------------------------------------------------

def _cg_rates(rules: rules_engine.RulesConfig) -> tuple:
    """(ltcg_rate, stcg_rate, exemption_limit) -- current (post-split, if
    any) rate only, since the estimator is a forward-looking mid-year tool,
    not a historical regression run (schedules.py's before/after-split
    machinery exists for regression years that straddle a mid-year rate
    change; that doesn't apply here)."""
    cg_cfg = rules.common["capital_gains"]

    def _rate(block: dict) -> float:
        return block["rate"] if "rate" in block else block["on_after_split"]

    ltcg_block = cg_cfg["s112a_ltcg_equity_stt"]
    stcg_block = cg_cfg["s111a_stcg_equity_stt"]
    exemption = ltcg_block.get("exemption_limit", 0) or 0
    return _rate(ltcg_block), _rate(stcg_block), exemption


@dataclass
class CapitalGainsTax:
    lt_taxable_gross: float = 0.0
    lt_taxable_after_exemption: float = 0.0
    st_taxable: float = 0.0
    other_taxable: float = 0.0      # OTHER-type items, folded into normal income
    special_rate_income: float = 0.0
    special_rate_tax: float = 0.0


def _capital_gains_tax(items: list, rules: rules_engine.RulesConfig) -> CapitalGainsTax:
    ltcg_rate, stcg_rate, exemption = _cg_rates(rules)
    lt_gross = sum(i.amount for i in items if i.gain_type == "LTCG_112A")
    st = sum(i.amount for i in items if i.gain_type == "STCG_111A")
    other = sum(i.amount for i in items if i.gain_type == "OTHER")
    # The s.112A exemption reduces a net LT GAIN; it must never turn a net
    # LT LOSS into zero (that would silently erase a real loss set-off
    # against STCG within the CG head -- mirrors schedules.py's
    # lt_taxable_gross - lt_exemption_used, where lt_exemption_used is 0.0
    # whenever lt_taxable_gross <= 0).
    lt_after_exemption = max(0.0, lt_gross - exemption) if lt_gross > 0 else lt_gross

    special_rate_tax = lt_after_exemption * ltcg_rate + st * stcg_rate
    special_rate_income = lt_after_exemption + st
    return CapitalGainsTax(
        lt_taxable_gross=lt_gross, lt_taxable_after_exemption=lt_after_exemption,
        st_taxable=st, other_taxable=other,
        special_rate_income=special_rate_income, special_rate_tax=special_rate_tax,
    )


# ---------------------------------------------------------------------------
# s.234C first-proviso exclusion for undated-aware, per-item CG
# ---------------------------------------------------------------------------

def _cg_proviso_exclusions_by_item(
    items: list, rules: rules_engine.RulesConfig, fy: str,
) -> tuple:
    """Per-instalment s.234C first-proviso exclusion, computed directly from
    each item's own sale_date/amount (exact -- no blended-figure ratio
    apportionment is needed here, unlike schedules.cg_proviso_exclusions,
    which only has one blended cg_tax per LT/ST bucket).

    An undated item receives NO relief in ANY instalment (stays in every
    instalment's base) -- the conservative direction, mirroring
    schedules.cg_proviso_exclusions's own convention -- and is never treated
    as though it arose in Q1 (which would instead be an active assumption
    maximising the base, not a neutral default).

    Returns (excluded_by_instalment: list[float], any_undated: bool).
    """
    ltcg_rate, stcg_rate, _exemption = _cg_rates(rules)
    fy_start = int(fy[:4])
    due_dates = [date(fy_start if m != 3 else fy_start + 1, m, d) for (m, d) in _INSTALMENT_MONTH_DAY]

    excluded = [0.0] * 4
    any_undated = any(i.undated for i in items if i.gain_type in ("LTCG_112A", "STCG_111A"))

    for item in items:
        if item.gain_type not in ("LTCG_112A", "STCG_111A") or item.undated:
            continue
        rate = ltcg_rate if item.gain_type == "LTCG_112A" else stcg_rate
        item_tax = item.amount * rate
        for i, due_date in enumerate(due_dates):
            if item.sale_date > due_date:
                excluded[i] += item_tax

    return excluded, any_undated


# ---------------------------------------------------------------------------
# Per-regime estimate + instalment schedule
# ---------------------------------------------------------------------------

def _cumulative_advance_tax_paid(advance_tax_paid: list, fy: str) -> list:
    """Cumulative advance tax actually paid, as at each of the four
    instalment due dates -- built directly from the caller-supplied
    (date, amount) list (advance tax paid is ALWAYS manual input, never
    scraped/inferred -- see module docstring / brief)."""
    fy_start = int(fy[:4])
    due_dates = [date(fy_start if m != 3 else fy_start + 1, m, d) for (m, d) in _INSTALMENT_MONTH_DAY]
    cumulative = []
    for due_date in due_dates:
        cumulative.append(sum(amt for (d, amt) in advance_tax_paid if d <= due_date))
    return cumulative


@dataclass
class RegimeEstimate:
    regime: str
    normal_income: float
    cg_tax: CapitalGainsTax
    tax_block: "tax_core.TaxBlock"
    tax_liability: float
    interest_234c: "interest_234.Interest234C"
    next_due_instalment: dict | None   # {"label", "due_date", "pay_by"} or None if none upcoming


@dataclass
class AdvanceTaxEstimate:
    input_data: EstimateInput
    old_regime: RegimeEstimate
    new_regime: RegimeEstimate
    unprojected_heads: list          # head names still blank
    undated_capital_gains: list      # CapitalGainItem entries with no sale_date
    undated_cg_amount: float
    advance_tax_cumulative_paid: list


def _estimate_regime(input_data: EstimateInput, regime: str, rules: rules_engine.RulesConfig) -> RegimeEstimate:
    cg_tax = _capital_gains_tax(input_data.capital_gains, rules)

    normal_income = (
        input_data.salary.full_year + input_data.remuneration.full_year
        + input_data.rent.full_year + input_data.interest.full_year
        + input_data.other_sources_other + cg_tax.other_taxable
    )
    # New regime disallows most Chapter VI-A deductions (schedules.py's own
    # DeductionsSchedule.regime_na convention) -- the estimator carries a
    # single blended VIA total rather than per-section eligibility, so it
    # follows the same regime gate rather than inventing a section-by-section
    # eligibility table (brief gives only a single "deductions" figure).
    via = 0.0 if regime == "new" else input_data.deductions_via
    normal_income_after_via = max(0.0, normal_income - via)

    fy_end = fy_window(input_data.fy)[1]
    tax_block = tax_core.compute_tax_on(
        normal_income=normal_income_after_via,
        special_rate_tax=cg_tax.special_rate_tax,
        special_rate_income_amount=cg_tax.special_rate_income,
        rules=rules, regime=regime, status=input_data.status, dob=input_data.dob,
        fy_end=fy_end, residency=input_data.residency,
    )

    tax_due_on_returned_income = max(0.0, tax_block.tax_liability - input_data.tds_to_date)
    cumulative_paid = _cumulative_advance_tax_paid(input_data.advance_tax_paid, input_data.fy)
    excluded_by_instalment, _any_undated = _cg_proviso_exclusions_by_item(
        input_data.capital_gains, rules, input_data.fy,
    )
    interest_result = interest_234.compute_234c(
        tax_due_on_returned_income, cumulative_paid, excluded_by_instalment,
    )

    next_due = None
    fy_start = int(input_data.fy[:4])
    for i, (m, d) in enumerate(_INSTALMENT_MONTH_DAY):
        due_date = date(fy_start if m != 3 else fy_start + 1, m, d)
        if due_date >= input_data.as_of:
            inst = interest_result.instalments[i]
            shortfall = max(0.0, inst.required_amount - inst.cumulative_paid)
            next_due = {
                "label": inst.label, "due_date": due_date,
                "pay_by": shortfall,
            }
            break

    return RegimeEstimate(
        regime=regime, normal_income=normal_income_after_via, cg_tax=cg_tax,
        tax_block=tax_block, tax_liability=tax_block.tax_liability,
        interest_234c=interest_result, next_due_instalment=next_due,
    )


def build_estimate(input_data: EstimateInput, rules: rules_engine.RulesConfig) -> AdvanceTaxEstimate:
    """Build both regimes side by side, per the brief -- the estimate output
    always shows old and new regardless of `input_data.regime` (which is
    only a display default)."""
    old = _estimate_regime(input_data, "old", rules)
    new = _estimate_regime(input_data, "new", rules)
    undated = input_data.undated_capital_gains()
    return AdvanceTaxEstimate(
        input_data=input_data, old_regime=old, new_regime=new,
        unprojected_heads=input_data.unprojected_heads(),
        undated_capital_gains=undated,
        undated_cg_amount=sum(i.amount for i in undated),
        advance_tax_cumulative_paid=_cumulative_advance_tax_paid(input_data.advance_tax_paid, input_data.fy),
    )


# ---------------------------------------------------------------------------
# Persistence -- Data/advance_tax/<name>.json (runtime, gitignored via
# Data/* -- see .gitignore; created at runtime if missing, never committed)
# ---------------------------------------------------------------------------

def _estimate_dir(data_root: str | Path | None = None) -> Path:
    """Resolve Data/advance_tax/. Mirrors the CWD-relative Data/ fallback
    used elsewhere in this codebase (see
    skill_gnucash_account_mapper/persistent_rules.py's `Path.cwd() / "Data"`)
    while still taking an explicit override so a caller that has already
    resolved the canonical Data root (e.g. ui/_config.py's data_root_dir())
    can pass it in directly instead of relying on CWD."""
    root = Path(data_root) if data_root is not None else Path.cwd() / "Data"
    return root / "advance_tax"


def _estimate_input_to_dict(input_data: EstimateInput) -> dict:
    def _head(h: ProjectedHead) -> dict:
        return {"actual_to_date": h.actual_to_date, "projected_remainder": h.projected_remainder}

    return {
        "entity_name": input_data.entity_name, "fy": input_data.fy, "regime": input_data.regime,
        "status": input_data.status, "dob": input_data.dob, "residency": input_data.residency,
        "as_of": input_data.as_of.isoformat(),
        "salary": _head(input_data.salary), "remuneration": _head(input_data.remuneration),
        "rent": _head(input_data.rent), "interest": _head(input_data.interest),
        "other_sources_other": input_data.other_sources_other,
        "capital_gains": [
            {
                "amount": i.amount, "gain_type": i.gain_type,
                "sale_date": i.sale_date.isoformat() if i.sale_date else None,
                "note": i.note,
            }
            for i in input_data.capital_gains
        ],
        "deductions_via": input_data.deductions_via, "tds_to_date": input_data.tds_to_date,
        "advance_tax_paid": [[d.isoformat(), amt] for (d, amt) in input_data.advance_tax_paid],
    }


def _estimate_input_from_dict(d: dict) -> EstimateInput:
    def _head(h: dict) -> ProjectedHead:
        return ProjectedHead(actual_to_date=h["actual_to_date"], projected_remainder=h["projected_remainder"])

    return EstimateInput(
        entity_name=d["entity_name"], fy=d["fy"], regime=d["regime"],
        status=d.get("status", "Individual"), dob=d.get("dob"), residency=d.get("residency"),
        as_of=date.fromisoformat(d["as_of"]),
        salary=_head(d["salary"]), remuneration=_head(d["remuneration"]),
        rent=_head(d["rent"]), interest=_head(d["interest"]),
        other_sources_other=d.get("other_sources_other", 0.0),
        capital_gains=[
            CapitalGainItem(
                amount=item["amount"], gain_type=item["gain_type"],
                sale_date=date.fromisoformat(item["sale_date"]) if item.get("sale_date") else None,
                note=item.get("note"),
            )
            for item in d.get("capital_gains", [])
        ],
        deductions_via=d.get("deductions_via", 0.0), tds_to_date=d.get("tds_to_date", 0.0),
        advance_tax_paid=[(date.fromisoformat(x[0]), x[1]) for x in d.get("advance_tax_paid", [])],
    )


def save_estimate(name: str, input_data: EstimateInput, data_root: str | Path | None = None) -> Path:
    """Save a named estimate to Data/advance_tax/<name>.json, creating the
    directory at runtime if missing. Only the EstimateInput is persisted
    (the recomputed output is cheap to rebuild from it, and never stale)."""
    est_dir = _estimate_dir(data_root)
    est_dir.mkdir(parents=True, exist_ok=True)
    out_path = est_dir / f"{name}.json"
    out_path.write_text(json.dumps(_estimate_input_to_dict(input_data), indent=2), encoding="utf-8")
    return out_path


def load_estimate(name: str, data_root: str | Path | None = None) -> EstimateInput:
    est_dir = _estimate_dir(data_root)
    in_path = est_dir / f"{name}.json"
    raw = json.loads(in_path.read_text(encoding="utf-8"))
    return _estimate_input_from_dict(raw)


def list_estimates(data_root: str | Path | None = None) -> list:
    """Names of saved estimates (sorted), or [] if the directory doesn't
    exist yet -- never an error just because nothing has been saved."""
    est_dir = _estimate_dir(data_root)
    if not est_dir.is_dir():
        return []
    return sorted(p.stem for p in est_dir.glob("*.json"))
