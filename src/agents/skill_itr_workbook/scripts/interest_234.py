"""
interest_234.py -- interest u/s 234A / 234B / 234C of the Income-tax Act, 1961.

Until now the workbook stopped at the tax liability and said nothing about
interest, so a tax-payable return understated what was actually due at
filing time (see the Assumptions note added 2026-07-22, now superseded).

Three independent charges, all at 1% per month simple:

  * **234A -- default in FURNISHING the return.** Charged only when the
    return is filed after the due date, from the day after the due date to
    the date of furnishing, on tax on total income less advance tax, TDS,
    TCS and reliefs. Nil for a return filed on time -- which is the common
    case, so this is nil far more often than 234B/234C.

  * **234B -- default in PAYMENT of advance tax.** Charged when advance tax
    actually paid is less than 90% of "assessed tax" (tax on total income
    less TDS/TCS/reliefs -- note: NOT less advance tax). Runs from 1 April
    of the assessment year to the date of determination, on the whole
    shortfall. The 90% test is a cliff: at 89% the entire shortfall carries
    interest, at 90% none of it does.

  * **234C -- DEFERMENT of individual advance-tax instalments.** Charged
    per instalment on the shortfall against a cumulative percentage of tax
    due on returned income, for a fixed 3/3/3/1 months. Two statutory safe
    harbours (12% at the June instalment, 36% at September) forgive small
    under-payments entirely; there is no such relief for December/March.

Two rules that are easy to get wrong and are applied throughout:

  * **Rule 119A** -- the amount on which interest is computed is rounded
    DOWN to the nearest multiple of 100 (fractions ignored). Applied to the
    base, never to the resulting interest.
  * **Part of a month counts as a FULL month** -- so one day past a
    threshold costs a whole month's interest. `months_between` implements
    exactly this and is deliberately not a day-count.

The 234C first proviso (relief for income that could not have been
foreseen) is implemented in `compute_234c` via `unforeseeable_by_quarter`:
capital gains, lottery/gambling winnings, dividend income and first-time
business income arising AFTER an instalment's due date are excluded from
that instalment's base, provided the tax is paid in the remaining
instalments or by 31 March. For a book with large capital gains, omitting
this proviso materially OVERSTATES 234C -- it is not an optional refinement.

This module is deliberately pure: dates and amounts in, numbers out, no
workbook or book dependency, so the statutory logic can be tested on its
own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

#: All three sections charge simple interest at 1% per month.
RATE_PER_MONTH = 0.01

#: Rule 119A -- round the base DOWN to the nearest 100 before applying the rate.
ROUND_DOWN_TO = 100

#: s.234B is charged only if advance tax paid falls below this fraction of
#: assessed tax. At or above it, 234B is nil however large the shortfall.
S234B_THRESHOLD = 0.90

#: s.234C instalment schedule: (label, due month, due day, cumulative
#: percentage required, safe-harbour percentage, months charged).
#: The June/September safe harbours are the first proviso's clauses (a)/(b);
#: December and March have none, so required == safe there.
S234C_INSTALMENTS: tuple[tuple[str, int, int, float, float, int], ...] = (
    ("Q1 (by 15 Jun)", 6, 15, 0.15, 0.12, 3),
    ("Q2 (by 15 Sep)", 9, 15, 0.45, 0.36, 3),
    ("Q3 (by 15 Dec)", 12, 15, 0.75, 0.75, 3),
    ("Q4 (by 15 Mar)", 3, 15, 1.00, 1.00, 1),
)


def round_down_100(amount: float) -> float:
    """Rule 119A. Negative bases never attract interest, so they clamp to 0
    rather than rounding away from zero (which would invent a charge)."""
    if amount <= 0:
        return 0.0
    return float(int(amount // ROUND_DOWN_TO) * ROUND_DOWN_TO)


def months_between(start: date, end: date) -> int:
    """Whole months from `start` to `end`, counting ANY part of a month as a
    full month (the statutory convention shared by 234A and 234B). Returns 0
    when `end` is on or before `start`.

    Worked: 31-Jul -> 01-Aug is 1 month (one day is a part month), 31-Jul ->
    31-Aug is also 1, and 31-Jul -> 01-Sep is 2.
    """
    if end <= start:
        return 0
    months = (end.year - start.year) * 12 + (end.month - start.month)
    if end.day > start.day:
        months += 1
    return max(months, 0)


@dataclass
class Interest234A:
    months: int = 0
    base: float = 0.0            # post-Rule-119A
    amount: float = 0.0
    applicable: bool = False     # True only when filed late
    due_date: date | None = None
    filing_date: date | None = None


@dataclass
class Interest234B:
    months: int = 0
    base: float = 0.0            # post-Rule-119A
    amount: float = 0.0
    applicable: bool = False     # True only when advance tax < 90% of assessed tax
    assessed_tax: float = 0.0
    advance_tax_paid: float = 0.0
    shortfall_pct: float = 0.0   # advance tax as a fraction of assessed tax


@dataclass
class Instalment234C:
    label: str
    required_pct: float
    safe_pct: float
    months: int
    tax_due_considered: float = 0.0   # base after the first-proviso exclusion
    required_amount: float = 0.0
    cumulative_paid: float = 0.0
    shortfall: float = 0.0            # post-Rule-119A
    amount: float = 0.0
    relieved: bool = False            # cumulative paid met the safe harbour


@dataclass
class Interest234C:
    instalments: list[Instalment234C] = field(default_factory=list)
    amount: float = 0.0


@dataclass
class Interest234:
    s234a: Interest234A = field(default_factory=Interest234A)
    s234b: Interest234B = field(default_factory=Interest234B)
    s234c: Interest234C = field(default_factory=Interest234C)
    total: float = 0.0
    #: Set when the caller supplied no filing date, so 234A/234B could only be
    #: computed against an assumed date -- surfaced in the workbook rather
    #: than silently presented as a determined figure.
    filing_date_assumed: bool = False
    #: Which TDS figure the charges were computed on, and why (see
    #: `resolve_tds_credit`). Populated whenever the caller passes one.
    tds_credit: TdsCreditBasis | None = None
    #: Everything the reader must check before filing -- the TDS-basis
    #: warnings plus any assumption this module had to make.
    warnings: list[str] = field(default_factory=list)


def compute_234a(
    tax_on_total_income: float, prepaid_taxes: float,
    due_date: date, filing_date: date,
) -> Interest234A:
    """s.234A. `prepaid_taxes` is everything already paid or credited --
    advance tax, TDS, TCS and reliefs -- since 234A, unlike 234B, is charged
    net of advance tax."""
    result = Interest234A(due_date=due_date, filing_date=filing_date)
    if filing_date <= due_date:
        return result                      # filed on time -- nil, the usual case
    result.applicable = True
    result.months = months_between(due_date, filing_date)
    result.base = round_down_100(tax_on_total_income - prepaid_taxes)
    result.amount = result.base * RATE_PER_MONTH * result.months
    return result


def compute_234b(
    assessed_tax: float, advance_tax_paid: float,
    ay_start: date, determination_date: date,
) -> Interest234B:
    """s.234B. `assessed_tax` must already be net of TDS/TCS/reliefs but NOT
    net of advance tax -- the 90% test and the interest base are defined
    against that figure. `ay_start` is 1 April of the assessment year."""
    result = Interest234B(assessed_tax=assessed_tax, advance_tax_paid=advance_tax_paid)
    if assessed_tax <= 0:
        return result
    result.shortfall_pct = advance_tax_paid / assessed_tax if assessed_tax else 0.0
    if result.shortfall_pct >= S234B_THRESHOLD:
        return result                      # 90% cliff cleared -- nil
    result.applicable = True
    result.months = months_between(ay_start, determination_date)
    result.base = round_down_100(assessed_tax - advance_tax_paid)
    result.amount = result.base * RATE_PER_MONTH * result.months
    return result


def compute_234c(
    tax_due_on_returned_income: float,
    cumulative_advance_tax: list[float],
    unforeseeable_by_quarter: list[float] | None = None,
) -> Interest234C:
    """s.234C.

    `tax_due_on_returned_income` is tax on total income less TDS/TCS/reliefs
    (advance tax is NOT deducted -- that is what each instalment is measured
    against). `cumulative_advance_tax` is four figures: advance tax paid up
    to and including each instalment due date, cumulative.

    `unforeseeable_by_quarter` carries the first proviso: for each
    instalment, the amount of TAX attributable to capital gains, lottery or
    gambling winnings, dividend income, or first-time business income that
    arose only AFTER that instalment's due date. It is subtracted from the
    base for that instalment, which is what stops a large March capital gain
    from retrospectively creating a June/September shortfall. Defaults to
    zeros (no relief), which is the conservative -- and for a book with
    capital gains, materially overstated -- treatment.
    """
    if unforeseeable_by_quarter is None:
        unforeseeable_by_quarter = [0.0] * len(S234C_INSTALMENTS)

    result = Interest234C()
    for i, (label, _m, _d, required_pct, safe_pct, months) in enumerate(S234C_INSTALMENTS):
        paid = cumulative_advance_tax[i] if i < len(cumulative_advance_tax) else 0.0
        excluded = unforeseeable_by_quarter[i] if i < len(unforeseeable_by_quarter) else 0.0
        considered = max(0.0, tax_due_on_returned_income - excluded)

        inst = Instalment234C(
            label=label, required_pct=required_pct, safe_pct=safe_pct, months=months,
            tax_due_considered=considered,
            required_amount=considered * required_pct,
            cumulative_paid=paid,
        )
        # Safe harbour: clearing the (lower) safe percentage forgives the
        # instalment entirely -- it does not merely reduce the shortfall.
        if paid >= considered * safe_pct:
            inst.relieved = True
        else:
            inst.shortfall = round_down_100(inst.required_amount - paid)
            inst.amount = inst.shortfall * RATE_PER_MONTH * months
        result.instalments.append(inst)

    result.amount = sum(i.amount for i in result.instalments)
    return result


# ---------------------------------------------------------------------------
# Which TDS figure the interest is actually computed on
# ---------------------------------------------------------------------------
#
# This matters more than it looks. 234B and 234C are both charged on tax
# LESS TDS/TCS, so every rupee of TDS credit directly reduces the interest.
# The book and 26AS routinely disagree, and they are not interchangeable:
#
#   * The **book** records what the assessee believes was deducted.
#   * **26AS** records what the deductor actually reported to TRACES, and is
#     what the department will allow as credit.
#
# Crediting book TDS that 26AS does not support therefore UNDERSTATES the
# interest -- the assessee files showing a small charge and the department
# later computes a larger one. So when 26AS is available it is the basis,
# and any divergence is surfaced rather than quietly absorbed.
#
# A second, separate trap: 26AS reports tax DEDUCTED and tax DEPOSITED as
# distinct columns. Amounts deducted but never deposited by the deductor are
# the deductor's default, and s.205 bars recovery from the assessee -- but in
# practice credit is often withheld until the deductor corrects its return.
# That exposure is reported here, never silently netted off either way.


@dataclass
class TdsCreditBasis:
    amount: float = 0.0          # the figure the 234 computation actually uses
    basis: str = "book"          # "26AS" | "book"
    book_amount: float = 0.0
    as26_amount: float = 0.0
    divergence: float = 0.0      # book - 26AS (positive => book claims more)
    not_deposited: float = 0.0   # deducted per 26AS but not deposited
    warnings: list[str] = field(default_factory=list)


def resolve_tds_credit(
    book_tds: float, as26_tds: float, as26_available: bool,
    as26_deducted_vs_deposited: list[tuple[float, float]] | None = None,
    book_only: float = 0.0, book_only_label: str = "",
) -> TdsCreditBasis:
    """Decide which TDS figure the 234 charges are computed on.

    `as26_deducted_vs_deposited` is a list of (tax_deducted, tds_deposited)
    pairs straight off 26AS Part I, used only to report the
    deducted-but-not-deposited exposure -- it never changes `amount`.

    `book_only` is the part of `as26_tds` that did NOT come from 26AS and was
    carried over from the book unchanged, because the 26AS reader classifies
    only interest and dividend sections. Calling the resulting total a "26AS"
    figure without saying so would overstate how corroborated it is, so it is
    disclosed as a warning. It never changes `amount` either -- dropping the
    component would understate the credit and overstate the interest.
    """
    result = TdsCreditBasis(book_amount=book_tds, as26_amount=as26_tds)

    if not as26_available:
        result.amount = book_tds
        result.basis = "book"
        result.warnings.append(
            "No 26AS supplied -- interest u/s 234B/234C is computed on the BOOK's "
            "TDS figure. If 26AS credits less than the book claims, the real "
            "interest will be HIGHER than shown."
        )
        return result

    result.amount = as26_tds
    result.basis = "26AS + book" if book_only > 0 else "26AS"
    result.divergence = book_tds - as26_tds

    if book_only > 0:
        result.warnings.append(
            f"Of the {as26_tds:,.2f} credit, {book_only:,.2f} "
            f"({book_only_label or 'not classified by the 26AS reader'}) comes "
            "from the BOOK, not from 26AS. Confirm it appears in 26AS before "
            "filing -- if it does not, the department will allow less and the "
            "real interest will be HIGHER than shown."
        )

    if abs(result.divergence) > 1.0:
        if result.divergence > 0:
            result.warnings.append(
                f"Book claims {result.divergence:,.2f} MORE TDS than 26AS reflects. "
                f"Interest is computed on the 26AS figure ({as26_tds:,.2f}), which is "
                "what the department will allow -- computing on the book figure would "
                "understate the interest. Reconcile before filing."
            )
        else:
            result.warnings.append(
                f"26AS reflects {abs(result.divergence):,.2f} MORE TDS than the book "
                f"records. Interest is computed on the 26AS figure ({as26_tds:,.2f}). "
                "The book may be missing a TDS entry."
            )

    if as26_deducted_vs_deposited:
        gap = sum(
            max(0.0, deducted - deposited)
            for deducted, deposited in as26_deducted_vs_deposited
        )
        if gap > 1.0:
            result.not_deposited = gap
            result.warnings.append(
                f"26AS shows {gap:,.2f} deducted but NOT deposited by the deductor. "
                "Credit may be withheld until the deductor files a correction, which "
                "would increase the interest u/s 234B/234C above the figure shown."
            )

    return result


def compute_all(
    tax_on_total_income: float,
    tds_tcs_and_reliefs: float,
    advance_tax_paid: float,
    cumulative_advance_tax: list[float],
    year_key: str,
    due_date: date,
    filing_date: date | None = None,
    unforeseeable_by_quarter: list[float] | None = None,
    tds_credit: TdsCreditBasis | None = None,
) -> Interest234:
    """Compute all three charges for an income year.

    `year_key` is the canonical income-year key ("2025-26"), so the
    assessment year starts 1 April of `int(year_key[:4]) + 1`.

    When `filing_date` is None the return is treated as filed on the due
    date -- 234A is then nil and 234B runs to the due date. That is an
    ASSUMPTION, flagged on the result as `filing_date_assumed` so the
    workbook can say so rather than present it as determined.
    """
    assumed = filing_date is None
    if filing_date is None:
        filing_date = due_date

    ay_start = date(int(year_key[:4]) + 1, 4, 1)
    assessed_tax = tax_on_total_income - tds_tcs_and_reliefs

    result = Interest234(filing_date_assumed=assumed, tds_credit=tds_credit)
    result.s234a = compute_234a(
        tax_on_total_income, tds_tcs_and_reliefs + advance_tax_paid, due_date, filing_date,
    )
    result.s234b = compute_234b(assessed_tax, advance_tax_paid, ay_start, filing_date)
    result.s234c = compute_234c(assessed_tax, cumulative_advance_tax, unforeseeable_by_quarter)
    result.total = result.s234a.amount + result.s234b.amount + result.s234c.amount

    if tds_credit is not None:
        result.warnings.extend(tds_credit.warnings)
    if assumed:
        result.warnings.append(
            "Filing date not supplied -- the return is ASSUMED to be filed on the due "
            f"date ({due_date.isoformat()}). 234A is nil on that assumption and 234B "
            "stops there; both grow for every part-month of delay beyond it."
        )
    return result
