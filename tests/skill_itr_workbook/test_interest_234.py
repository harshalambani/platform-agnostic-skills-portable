"""
Tests for scripts/interest_234.py -- interest u/s 234A / 234B / 234C.

The statutory logic here is all cliffs and conventions rather than smooth
arithmetic, so the tests concentrate on the places a plausible-looking
implementation goes wrong:

  * **Part-month counting.** One day past a threshold must cost a full
    month. A day-count or a naive `relativedelta` both silently undercharge.
  * **Rule 119A.** The base rounds DOWN to the nearest 100, and only the
    base -- rounding the interest instead gives a different answer.
  * **The 234B 90% cliff.** At 89.99% the WHOLE shortfall carries interest;
    at 90% none does. An implementation that charges interest on the
    shortfall below 90% (rather than testing at 90% and charging on the
    full shortfall) passes casual inspection and is wrong.
  * **The 234C safe harbours.** Clearing 12%/36% forgives the June/September
    instalment ENTIRELY -- it does not merely reduce the shortfall to the
    safe level. December and March have no such relief.
  * **The 234C first proviso.** Capital gains arising in Q4 must not create
    a retrospective Q1 shortfall. For a book with large capital gains this
    is the difference between a correct figure and a large overstatement,
    so it is tested in both directions.

All figures are fabricated. No real PII, PAN, account numbers or amounts
appear in this file.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import interest_234 as i234  # noqa: E402


# ---------------------------------------------------------------------------
# Rule 119A -- round the base down to the nearest 100
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("amount,expected", [
    (0.0, 0.0),
    (99.0, 0.0),
    (100.0, 100.0),
    (199.99, 100.0),
    (12_345.0, 12_300.0),
    (1_00_000.0, 1_00_000.0),
])
def test_round_down_100_ignores_fractions_of_a_hundred(amount, expected):
    assert i234.round_down_100(amount) == expected


@pytest.mark.parametrize("amount", [-1.0, -100.0, -12_345.0])
def test_round_down_100_clamps_negatives_to_zero(amount):
    """A negative base means nothing is owed. Rounding it "down" would move
    it further from zero and invent a charge."""
    assert i234.round_down_100(amount) == 0.0


# ---------------------------------------------------------------------------
# Part of a month counts as a full month
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("start,end,expected", [
    (date(2026, 7, 31), date(2026, 7, 31), 0),   # same day
    (date(2026, 7, 31), date(2026, 7, 30), 0),   # end before start
    (date(2026, 7, 31), date(2026, 8, 1), 1),    # ONE DAY = a full month
    (date(2026, 7, 31), date(2026, 8, 31), 1),
    (date(2026, 7, 31), date(2026, 9, 1), 2),
    (date(2026, 4, 1), date(2026, 7, 15), 4),    # Apr, May, Jun, Jul
    (date(2026, 4, 1), date(2027, 4, 1), 12),
    (date(2026, 4, 1), date(2027, 4, 2), 13),
])
def test_months_between_counts_part_months_as_whole(start, end, expected):
    assert i234.months_between(start, end) == expected


# ---------------------------------------------------------------------------
# 234A -- default in furnishing the return
# ---------------------------------------------------------------------------

def test_234a_nil_when_filed_on_or_before_due_date():
    """The common case. Filing on the due date itself must not attract a
    month's interest."""
    r = i234.compute_234a(1_00_000, 20_000, date(2026, 7, 31), date(2026, 7, 31))
    assert r.applicable is False
    assert r.amount == 0.0
    assert r.months == 0


def test_234a_one_day_late_costs_a_full_month():
    r = i234.compute_234a(1_00_000, 20_000, date(2026, 7, 31), date(2026, 8, 1))
    assert r.applicable is True
    assert r.months == 1
    assert r.base == 80_000          # 1,00,000 - 20,000, already a multiple of 100
    assert r.amount == pytest.approx(800.0)


def test_234a_is_net_of_advance_tax_unlike_234b():
    """234A's base is net of ALL prepaid taxes including advance tax. Using
    the 234B base (which excludes advance tax) would overcharge here."""
    r = i234.compute_234a(
        tax_on_total_income=1_00_000, prepaid_taxes=95_000,
        due_date=date(2026, 7, 31), filing_date=date(2026, 9, 30),
    )
    assert r.months == 2
    assert r.base == 5_000
    assert r.amount == pytest.approx(100.0)


def test_234a_base_is_rounded_down_before_the_rate_applies():
    r = i234.compute_234a(1_00_099, 20_000, date(2026, 7, 31), date(2026, 8, 15))
    assert r.base == 80_000          # 80,099 -> 80,000, not 80,099
    assert r.amount == pytest.approx(800.0)


def test_234a_nil_when_prepaid_exceeds_liability_even_if_filed_late():
    r = i234.compute_234a(50_000, 60_000, date(2026, 7, 31), date(2026, 12, 31))
    assert r.applicable is True      # the default happened...
    assert r.base == 0.0             # ...but there is nothing to charge on
    assert r.amount == 0.0


# ---------------------------------------------------------------------------
# 234B -- default in payment of advance tax (the 90% cliff)
# ---------------------------------------------------------------------------

def test_234b_nil_at_exactly_90_percent():
    r = i234.compute_234b(1_00_000, 90_000, date(2026, 4, 1), date(2026, 7, 31))
    assert r.applicable is False
    assert r.amount == 0.0


def test_234b_charges_the_whole_shortfall_just_below_90_percent():
    """The cliff: at 89.9% the base is the ENTIRE shortfall (10,100), not
    just the part below 90%."""
    r = i234.compute_234b(1_00_000, 89_900, date(2026, 4, 1), date(2026, 7, 31))
    assert r.applicable is True
    assert r.base == 10_100
    assert r.months == 4                      # Apr, May, Jun, Jul
    assert r.amount == pytest.approx(404.0)


def test_234b_nil_when_no_advance_tax_but_no_assessed_tax_either():
    r = i234.compute_234b(0.0, 0.0, date(2026, 4, 1), date(2026, 7, 31))
    assert r.applicable is False
    assert r.amount == 0.0


def test_234b_full_charge_when_no_advance_tax_paid_at_all():
    r = i234.compute_234b(1_00_000, 0.0, date(2026, 4, 1), date(2026, 7, 31))
    assert r.applicable is True
    assert r.shortfall_pct == 0.0
    assert r.base == 1_00_000
    assert r.amount == pytest.approx(4_000.0)


def test_234b_assessed_tax_is_not_net_of_advance_tax():
    """Guard against the classic error of passing an assessed tax that has
    already had advance tax deducted -- the 90% test would then always fail
    and the base would be double-counted."""
    r = i234.compute_234b(1_00_000, 50_000, date(2026, 4, 1), date(2026, 5, 1))
    assert r.assessed_tax == 1_00_000
    assert r.shortfall_pct == pytest.approx(0.5)
    assert r.base == 50_000


# ---------------------------------------------------------------------------
# 234C -- deferment of instalments
# ---------------------------------------------------------------------------

def test_234c_nil_when_every_instalment_paid_in_full():
    tax_due = 1_00_000.0
    cum = [15_000.0, 45_000.0, 75_000.0, 1_00_000.0]
    r = i234.compute_234c(tax_due, cum)
    assert r.amount == 0.0
    assert all(i.amount == 0.0 for i in r.instalments)


def test_234c_no_advance_tax_at_all_charges_every_instalment():
    """3 + 3 + 3 + 1 months on 15% / 45% / 75% / 100% of 1,00,000."""
    r = i234.compute_234c(1_00_000.0, [0.0, 0.0, 0.0, 0.0])
    amounts = [i.amount for i in r.instalments]
    assert amounts[0] == pytest.approx(15_000 * 0.01 * 3)    # 450
    assert amounts[1] == pytest.approx(45_000 * 0.01 * 3)    # 1350
    assert amounts[2] == pytest.approx(75_000 * 0.01 * 3)    # 2250
    assert amounts[3] == pytest.approx(1_00_000 * 0.01 * 1)  # 1000
    assert r.amount == pytest.approx(5_050.0)


def test_234c_q1_safe_harbour_at_12_percent_forgives_the_instalment_entirely():
    """Paying 12% (not the required 15%) forgives Q1 completely -- it does
    NOT leave a 3% shortfall."""
    r = i234.compute_234c(1_00_000.0, [12_000.0, 45_000.0, 75_000.0, 1_00_000.0])
    assert r.instalments[0].relieved is True
    assert r.instalments[0].amount == 0.0
    assert r.amount == 0.0


def test_234c_q1_just_below_the_safe_harbour_charges_the_full_shortfall():
    """At 11,900 the safe harbour is missed, and the shortfall is measured
    against the REQUIRED 15% -- 3,100, not the 100 below the safe harbour."""
    r = i234.compute_234c(1_00_000.0, [11_900.0, 45_000.0, 75_000.0, 1_00_000.0])
    inst = r.instalments[0]
    assert inst.relieved is False
    assert inst.shortfall == 3_100
    assert inst.amount == pytest.approx(93.0)


def test_234c_q2_safe_harbour_is_36_percent():
    r = i234.compute_234c(1_00_000.0, [15_000.0, 36_000.0, 75_000.0, 1_00_000.0])
    assert r.instalments[1].relieved is True
    assert r.amount == 0.0


def test_234c_q3_and_q4_have_no_safe_harbour():
    """December and March are charged on any shortfall at all."""
    r = i234.compute_234c(1_00_000.0, [15_000.0, 45_000.0, 74_900.0, 99_900.0])
    assert r.instalments[2].relieved is False
    assert r.instalments[2].shortfall == 100
    assert r.instalments[3].relieved is False
    assert r.instalments[3].shortfall == 100


def test_234c_q4_is_charged_for_one_month_not_three():
    r = i234.compute_234c(1_00_000.0, [15_000.0, 45_000.0, 75_000.0, 0.0])
    assert r.instalments[3].months == 1
    assert r.instalments[3].amount == pytest.approx(1_00_000 * 0.01 * 1)


# ---------------------------------------------------------------------------
# 234C first proviso -- income that could not have been foreseen
# ---------------------------------------------------------------------------

def test_234c_q4_capital_gain_does_not_create_a_retrospective_q1_shortfall():
    """The whole point of the proviso. A capital gain realised in March
    cannot make the June instalment retrospectively short, PROVIDED the tax
    is paid by 31 March -- which it is here (Q4 cumulative covers it)."""
    tax_due = 1_00_000.0
    cg_tax = 70_000.0                     # tax attributable to a Q4 capital gain
    # Advance tax covers only the non-CG 30,000 until March, then the lot.
    cum = [4_500.0, 13_500.0, 22_500.0, 1_00_000.0]
    unforeseeable = [cg_tax, cg_tax, cg_tax, 0.0]

    r = i234.compute_234c(tax_due, cum, unforeseeable)
    assert r.instalments[0].tax_due_considered == 30_000.0
    assert r.instalments[0].amount == 0.0     # 4,500 == 15% of 30,000
    assert r.instalments[3].amount == 0.0
    assert r.amount == 0.0


def test_234c_without_the_proviso_the_same_facts_are_materially_overstated():
    """Same facts as above with the relief switched off -- proving the
    proviso is not a no-op and that omitting it overstates the charge."""
    tax_due = 1_00_000.0
    cum = [4_500.0, 13_500.0, 22_500.0, 1_00_000.0]

    without = i234.compute_234c(tax_due, cum)
    assert without.amount > 0
    assert without.instalments[0].shortfall == 10_500     # 15,000 - 4,500

    with_relief = i234.compute_234c(tax_due, cum, [70_000.0] * 3 + [0.0])
    assert with_relief.amount == 0.0
    assert without.amount > with_relief.amount


def test_234c_proviso_does_not_help_if_the_tax_is_not_paid_by_march():
    """The relief is conditional on paying in the remaining instalments. If
    Q4 is still short, Q4 itself is charged on the full (unexcluded) base."""
    r = i234.compute_234c(
        1_00_000.0, [4_500.0, 13_500.0, 22_500.0, 22_500.0], [70_000.0] * 3 + [0.0],
    )
    assert r.instalments[3].tax_due_considered == 1_00_000.0
    assert r.instalments[3].shortfall == 77_500
    assert r.instalments[3].amount == pytest.approx(775.0)


def test_234c_exclusion_larger_than_tax_due_clamps_to_zero_base():
    r = i234.compute_234c(50_000.0, [0.0, 0.0, 0.0, 50_000.0], [80_000.0] * 3 + [0.0])
    assert r.instalments[0].tax_due_considered == 0.0
    assert r.instalments[0].amount == 0.0


# ---------------------------------------------------------------------------
# Which TDS figure the interest is computed on (26AS vs book)
# ---------------------------------------------------------------------------

def test_tds_credit_prefers_26as_when_available():
    c = i234.resolve_tds_credit(book_tds=30_000, as26_tds=4_600, as26_available=True)
    assert c.basis == "26AS"
    assert c.amount == 4_600


def test_tds_credit_falls_back_to_book_and_warns_when_no_26as():
    c = i234.resolve_tds_credit(book_tds=30_000, as26_tds=0.0, as26_available=False)
    assert c.basis == "book"
    assert c.amount == 30_000
    assert any("No 26AS supplied" in w for w in c.warnings)


def test_tds_credit_warns_when_book_claims_more_than_26as():
    """The dangerous direction: crediting TDS the department will not allow
    understates the interest."""
    c = i234.resolve_tds_credit(book_tds=30_000, as26_tds=4_600, as26_available=True)
    assert c.divergence == pytest.approx(25_400)
    assert any("MORE TDS than 26AS" in w for w in c.warnings)
    assert any("understate" in w for w in c.warnings)


def test_tds_credit_warns_when_26as_exceeds_the_book():
    c = i234.resolve_tds_credit(book_tds=4_600, as26_tds=30_000, as26_available=True)
    assert c.divergence == pytest.approx(-25_400)
    assert any("missing a TDS entry" in w for w in c.warnings)


def test_tds_credit_silent_when_book_and_26as_agree():
    c = i234.resolve_tds_credit(book_tds=4_600, as26_tds=4_600, as26_available=True)
    assert c.divergence == 0
    assert c.warnings == []


def test_tds_credit_reports_deducted_but_not_deposited_without_changing_the_amount():
    """s.205 bars recovery from the assessee, so the figure must NOT be
    reduced -- but the exposure has to be visible."""
    c = i234.resolve_tds_credit(
        book_tds=10_000, as26_tds=10_000, as26_available=True,
        as26_deducted_vs_deposited=[(6_000, 6_000), (4_000, 1_000)],
    )
    assert c.amount == 10_000            # unchanged
    assert c.not_deposited == pytest.approx(3_000)
    assert any("not deposited" in w.lower() for w in c.warnings)


def test_tds_credit_ignores_deposits_exceeding_the_deduction():
    """An over-deposit on one row must not offset a shortfall on another."""
    c = i234.resolve_tds_credit(
        book_tds=10_000, as26_tds=10_000, as26_available=True,
        as26_deducted_vs_deposited=[(5_000, 9_000), (5_000, 2_000)],
    )
    assert c.not_deposited == pytest.approx(3_000)


def test_using_book_tds_instead_of_26as_materially_understates_the_interest():
    """End-to-end proof of why the basis matters: same facts, two TDS
    figures, materially different interest."""
    common = dict(
        tax_on_total_income=2_00_000, advance_tax_paid=0,
        cumulative_advance_tax=[0, 0, 0, 0], year_key="2025-26",
        due_date=date(2026, 7, 31), filing_date=date(2026, 7, 31),
    )
    on_book = i234.compute_all(tds_tcs_and_reliefs=30_000, **common)
    on_26as = i234.compute_all(tds_tcs_and_reliefs=4_600, **common)
    assert on_26as.total > on_book.total


# ---------------------------------------------------------------------------
# compute_all -- wiring of the three charges
# ---------------------------------------------------------------------------

def test_compute_all_filed_on_time_and_fully_paid_is_nil_across_all_three():
    r = i234.compute_all(
        tax_on_total_income=1_00_000, tds_tcs_and_reliefs=20_000,
        advance_tax_paid=80_000,
        cumulative_advance_tax=[12_000, 36_000, 60_000, 80_000],
        year_key="2025-26", due_date=date(2026, 7, 31),
        filing_date=date(2026, 7, 31),
    )
    assert r.s234a.amount == 0.0
    assert r.s234b.amount == 0.0
    assert r.s234c.amount == 0.0
    assert r.total == 0.0
    assert r.filing_date_assumed is False


def test_compute_all_flags_an_assumed_filing_date_when_none_supplied():
    """A missing filing date must never be presented as a determined figure
    -- 234A/234B both depend on it."""
    r = i234.compute_all(
        tax_on_total_income=1_00_000, tds_tcs_and_reliefs=20_000,
        advance_tax_paid=10_000, cumulative_advance_tax=[0, 0, 0, 10_000],
        year_key="2025-26", due_date=date(2026, 7, 31), filing_date=None,
    )
    assert r.filing_date_assumed is True
    assert r.s234a.amount == 0.0            # treated as filed on the due date
    assert r.s234b.applicable is True       # but 234B still runs to that date


def test_compute_all_assessment_year_starts_the_april_after_the_income_year():
    """234B runs from 1 April of the ASSESSMENT year (2026 for FY 2025-26),
    not of the income year -- a year's difference is 12 months of interest."""
    r = i234.compute_all(
        tax_on_total_income=1_00_000, tds_tcs_and_reliefs=0,
        advance_tax_paid=0, cumulative_advance_tax=[0, 0, 0, 0],
        year_key="2025-26", due_date=date(2026, 7, 31),
        filing_date=date(2026, 7, 31),
    )
    assert r.s234b.months == 4              # Apr, May, Jun, Jul 2026


def test_compute_all_total_is_the_sum_of_the_three():
    r = i234.compute_all(
        tax_on_total_income=2_00_000, tds_tcs_and_reliefs=10_000,
        advance_tax_paid=0, cumulative_advance_tax=[0, 0, 0, 0],
        year_key="2025-26", due_date=date(2026, 7, 31),
        filing_date=date(2026, 12, 15),
    )
    assert r.total == pytest.approx(
        r.s234a.amount + r.s234b.amount + r.s234c.amount
    )
    assert r.s234a.amount > 0
    assert r.s234b.amount > 0
    assert r.s234c.amount > 0


def test_compute_all_carries_tds_basis_warnings_onto_the_result():
    credit = i234.resolve_tds_credit(book_tds=30_000, as26_tds=4_600, as26_available=True)
    r = i234.compute_all(
        tax_on_total_income=2_00_000, tds_tcs_and_reliefs=credit.amount,
        advance_tax_paid=0, cumulative_advance_tax=[0, 0, 0, 0],
        year_key="2025-26", due_date=date(2026, 7, 31), filing_date=date(2026, 7, 31),
        tds_credit=credit,
    )
    assert r.tds_credit is credit
    assert any("MORE TDS than 26AS" in w for w in r.warnings)


def test_compute_all_warns_that_an_assumed_filing_date_understates_234a():
    r = i234.compute_all(
        tax_on_total_income=1_00_000, tds_tcs_and_reliefs=0,
        advance_tax_paid=0, cumulative_advance_tax=[0, 0, 0, 0],
        year_key="2025-26", due_date=date(2026, 7, 31), filing_date=None,
    )
    assert any("ASSUMED to be filed" in w for w in r.warnings)
