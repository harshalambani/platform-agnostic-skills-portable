"""
tax_core.py -- the ONE tax core: slab tax, rebate u/s 87A, surcharge (with
marginal relief), and cess, driven entirely from a loaded Rules config.

Extracted from schedules.py's `compute_tax` (2026-08 advance-tax-estimator
work) so the year-end ITR workbook and the mid-year advance-tax estimator
share a single implementation instead of two copies that could silently
drift apart. schedules.py's `compute_tax` is now a thin re-export of
`compute_tax_on` here, kept for backward compatibility with existing
imports/tests.

Nothing in this module may hardcode a rate, cap, slab, or section number --
every figure comes from the `RulesConfig` passed in (rules.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import rules as rules_engine


@dataclass
class TaxBlock:
    normal_income: float
    special_rate_income: float
    tax_on_normal_income: float
    tax_on_special_rate_income: float
    rebate_87a: float
    tax_after_rebate: float
    surcharge: float
    cess: float
    tax_before_relief: float
    relief_89: float
    tax_liability: float


def _slab_tax(income: float, slabs: list) -> float:
    tax = 0.0
    prev_upto = 0.0
    for band in slabs:
        upto = band["upto"]
        rate = band["rate"]
        top = upto if upto is not None else income
        if income <= prev_upto:
            break
        taxable_in_band = min(income, top) - prev_upto
        if taxable_in_band > 0:
            tax += taxable_in_band * rate
        prev_upto = top
        if upto is not None and income <= upto:
            break
    return tax


def _surcharge_rate_and_threshold(income: float, bands: list) -> tuple:
    """(rate, threshold) for the highest surcharge band `income` exceeds;
    (0.0, 0.0) if income is below every band's 'above' value. `threshold`
    is that band's 'above' figure -- the boundary CF1's marginal relief
    check operates against."""
    rate, threshold = 0.0, 0.0
    for band in bands:
        if income > band["above"]:
            rate, threshold = band["rate"], band["above"]
    return rate, threshold


def _surcharge(
    income_for_surcharge: float, normal_income: float, special_rate_income_amount: float,
    tax_normal_after_rebate: float, tax_special: float, slabs: list, surcharge_cfg: dict,
) -> float:
    """Surcharge on (tax_normal_after_rebate + tax_special), CF1-compliant:
      (b) tax_special (111A/112A/112 CG tax -- already flat-rate, so linear
          in income) is surcharged at min(band_rate, cap_on_cg_dividend);
          tax_normal_after_rebate is surcharged at the full band rate.
          NOTE (known deviation, flag on Reconciliation): dividend income is
          not yet split out of normal_income, so the 15% cap does not yet
          reach dividend-attributable tax -- CG (111A/112A/112) only.
      (a) marginal relief at every band boundary income_for_surcharge
          crosses: total tax+surcharge is capped at (tax-at-threshold +
          income excess over threshold). 'Tax at threshold' recomputes slab
          tax on normal_income scaled down to the threshold total and scales
          the (linear) special-rate tax by the same factor -- a documented
          simplification for mixed normal/special-rate income; the exact
          apportionment is not settled market practice either.
    Driven entirely from `surcharge_cfg` (Rules config) -- no hardcoded
    bands/caps/rates.
    """
    bands = surcharge_cfg["bands"]
    rate, threshold = _surcharge_rate_and_threshold(income_for_surcharge, bands)
    if rate == 0.0:
        return 0.0

    cap = surcharge_cfg.get("cap_on_cg_dividend")
    special_rate = min(rate, cap) if cap is not None else rate
    raw_surcharge = tax_normal_after_rebate * rate + tax_special * special_rate
    total_tax = tax_normal_after_rebate + tax_special

    if threshold <= 0 or income_for_surcharge <= 0:
        return raw_surcharge

    scale = threshold / income_for_surcharge
    normal_income_at_t = normal_income * scale
    tax_normal_at_t = _slab_tax(normal_income_at_t, slabs)
    tax_special_at_t = tax_special * scale   # linear in income (flat rate)

    rate_at_t, _ = _surcharge_rate_and_threshold(threshold, bands)
    special_rate_at_t = min(rate_at_t, cap) if cap is not None else rate_at_t
    surcharge_at_t = tax_normal_at_t * rate_at_t + tax_special_at_t * special_rate_at_t
    tax_at_t = tax_normal_at_t + tax_special_at_t

    max_allowed = tax_at_t + surcharge_at_t + (income_for_surcharge - threshold)
    if total_tax + raw_surcharge > max_allowed:
        return max(0.0, max_allowed - total_tax)
    return raw_surcharge


def compute_tax_on(
    normal_income: float, special_rate_tax: float, special_rate_income_amount: float,
    rules: rules_engine.RulesConfig, regime: str, status: str, dob: str | None, fy_end: date,
    relief_89: float = 0.0, residency: str | None = None,
) -> TaxBlock:
    """The single tax core: slabs -> rebate 87A -> surcharge (with marginal
    relief) -> cess -> relief u/s 89. Used by BOTH the year-end ITR workbook
    (schedules.build_computation) and the mid-year advance-tax estimator
    (advance_tax.py) -- there is exactly one implementation of this
    arithmetic in the codebase."""
    slabs = rules_engine.resolve_slabs(rules, regime, status, dob, fy_end, residency=residency)
    block = rules.regime(regime)
    tax_normal = _slab_tax(normal_income, slabs)

    # Special-rate income (111A/112A/112) is taxed at its own flat rate(s) --
    # the caller has already applied the correct before/after-split rate per
    # lot; special_rate_tax is that pre-computed tax, and
    # special_rate_income_amount is the underlying INCOME (needed separately
    # for surcharge-threshold classification -- CF1 fix: these were
    # previously conflated, understating income_for_surcharge for any
    # entity with material CG since CG tax << CG income).
    tax_special = special_rate_tax

    rebate_cfg = block["rebate_87a"]
    total_income_for_rebate = (
        normal_income if rebate_cfg.get("excludes_special_rate_income")
        else normal_income + special_rate_income_amount
    )
    rebate = 0.0
    if rebate_cfg["eligibility"] == "resident-individual" and status == "Individual":
        if total_income_for_rebate <= rebate_cfg["max_total_income"]:
            rebate = min(rebate_cfg["max_rebate"], tax_normal)
        elif rebate_cfg.get("marginal_relief"):
            excess = total_income_for_rebate - rebate_cfg["max_total_income"]
            if tax_normal - excess > 0:
                rebate = max(0.0, tax_normal - excess)

    tax_normal_after_rebate = tax_normal - rebate
    income_for_surcharge = normal_income + special_rate_income_amount
    surcharge = _surcharge(
        income_for_surcharge, normal_income, special_rate_income_amount,
        tax_normal_after_rebate, tax_special, slabs, block["surcharge"],
    )
    tax_after_rebate = tax_normal_after_rebate + tax_special
    cess_rate = rules.common["cess_rate"]
    cess = (tax_after_rebate + surcharge) * cess_rate
    tax_before_relief = tax_after_rebate + surcharge + cess
    tax_liability = max(0.0, tax_before_relief - relief_89)

    return TaxBlock(
        normal_income=normal_income, special_rate_income=special_rate_income_amount,
        tax_on_normal_income=tax_normal, tax_on_special_rate_income=tax_special,
        rebate_87a=rebate, tax_after_rebate=tax_after_rebate, surcharge=surcharge,
        cess=cess, tax_before_relief=tax_before_relief, relief_89=relief_89,
        tax_liability=tax_liability,
    )
