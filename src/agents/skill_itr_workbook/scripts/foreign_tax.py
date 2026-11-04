"""
foreign_tax.py -- ITR-06: foreign tax credit (relief u/s 90 / 90A / 91).

What this module does, and the lines it never crosses:

  * Foreign tax is NOT TDS. A TAXPAID_FOREIGN account never enters the TDS
    credit (TaxesPaidSchedule.total) and never enters the 26AS tie-out. It is
    relief, claimed on Form 67, and is subtracted from the tax liability.
  * Relief per country = the LOWER of (a) the foreign tax paid on that
    country's income and (b) the Indian tax on that income, where the Indian
    tax is the income times the average rate of Indian tax (tax liability /
    total income, surcharge and cess included, as in Rule 128(8)). The total
    relief is also capped at the tax liability itself.
  * Where the country is not known, NO relief is claimed (Form 67 is per
    country) and the figure is shown with a loud note instead.
  * The same dividend is never counted twice. A book account that carries
    foreign dividends (OS_DIVIDEND flagged FOREIGN_INCOME or AL_FOREIGN) and a
    broker report that would add the same dividends on top of it (the entity
    says the book does NOT already hold them) is a LOUD STOP, not a guess.
    For the FSI/Form 67 figures the broker report wins when it has rows; the
    book figures then serve only as a cross-check.
  * Relief is never claimed without the "Form 67 must be filed" line.

Pure data: no Excel here; write_workbook.py renders the ForeignTax sheet.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Mapping-entry flags that mark an income account as foreign source.
#: AL_FOREIGN is accepted on an OS_DIVIDEND leaf too ("a foreign-flagged
#: account"); FOREIGN_INCOME is the income-side flag that does not also pull
#: the leaf into the Schedule FA asset roll-up.
FOREIGN_INCOME_FLAGS = ("FOREIGN_INCOME", "AL_FOREIGN")

FOREIGN_TAX_TAG = "TAXPAID_FOREIGN"
FOREIGN_DIVIDEND_TAG = "OS_DIVIDEND"

#: Rupee tolerance when comparing the book's figure with the report's.
TOLERANCE = 1.0

FORM_67_LINE = (
    "Form 67 must be filed on the income-tax portal on or before the date the "
    "return is furnished; relief is not allowed without it."
)

#: Substring main() greps in the run summary to flip the exit code.
DOUBLE_COUNT_ERROR_MARKER = "ERROR: foreign dividends would be counted twice"

NO_COUNTRY = "(country not stated)"


class ForeignDoubleCountError(ValueError):
    """The book and the broker report would both count the same dividends."""


@dataclass
class ForeignTaxRow:
    country: str
    income: float
    foreign_tax: float
    source: str                       # where income / tax came from
    indian_tax: float = 0.0           # Indian tax on `income` at the average rate
    relief: float = 0.0               # min(foreign_tax, indian_tax); 0 when no country
    note: str = ""


@dataclass
class ForeignTaxSchedule:
    rows: list = field(default_factory=list)            # list[ForeignTaxRow]
    book_foreign_tax: float = 0.0                       # TAXPAID_FOREIGN total (abs)
    book_foreign_dividends: float = 0.0                 # OS_DIVIDEND on foreign-flagged accounts
    report_foreign_tax: float = 0.0                     # sum of FSI "tax paid outside"
    report_foreign_income: float = 0.0
    unallocated_tax: float = 0.0                        # foreign tax with no country: no relief
    warnings: list = field(default_factory=list)        # list[str]
    average_rate: float = 0.0
    relief_total: float = 0.0
    relief_computed: bool = False
    #: A1 reference of the relief total on the ForeignTax sheet, set by the
    #: workbook writer (None until written / when no relief is claimed).
    relief_cell: str | None = None

    @property
    def active(self) -> bool:
        """True when there is any foreign activity at all. A run with none
        gets no ForeignTax sheet and no new line anywhere."""
        return bool(
            self.book_foreign_tax or self.book_foreign_dividends
            or self.report_foreign_tax or self.warnings
        )

    @property
    def relief_claimed(self) -> bool:
        return self.relief_total > 0

    @property
    def form_67_required(self) -> bool:
        return self.relief_claimed


# ---------------------------------------------------------------------------
# Book side
# ---------------------------------------------------------------------------

def _leaf_total(node_by_guid: dict, leaf) -> float:
    node = node_by_guid.get(leaf.guid)
    return (node.total or 0.0) if node is not None else 0.0


def foreign_dividend_leaves(resolved: dict, node_by_guid: dict) -> list:
    """OS_DIVIDEND leaves flagged as foreign source (and present in the book)."""
    return [
        leaf for leaf in resolved.values()
        if leaf.tag == FOREIGN_DIVIDEND_TAG and leaf.guid in node_by_guid
        and any(f in FOREIGN_INCOME_FLAGS for f in (leaf.flags or []))
    ]


def book_foreign_dividends(resolved: dict, node_by_guid: dict) -> float:
    return sum(_leaf_total(node_by_guid, leaf) for leaf in foreign_dividend_leaves(resolved, node_by_guid))


def book_foreign_tax(resolved: dict, node_by_guid: dict) -> float:
    return abs(sum(
        _leaf_total(node_by_guid, leaf) for leaf in resolved.values()
        if leaf.tag == FOREIGN_TAX_TAG and leaf.guid in node_by_guid
    ))


# ---------------------------------------------------------------------------
# The double-count stop
# ---------------------------------------------------------------------------

def double_count_error(
    resolved: dict, node_by_guid: dict, foreign_dividends_in_book: bool,
    report_dividend_gross: float | None,
) -> str | None:
    """A message when the same foreign dividends would be counted from the
    book AND from the broker report; None when it is safe.

    It fires only when (1) a book account is flagged as foreign dividends,
    (2) the broker report carries ordinary dividends, and (3) the entity says
    the book does NOT already hold the broker's dividends -- the one setting
    under which Other Sources adds the report's dividends on top of the book's.
    With `foreign_dividends_in_book` true the report is kept out of Other
    Sources, so there is no second count."""
    if foreign_dividends_in_book:
        return None
    if not report_dividend_gross:
        return None
    leaves = foreign_dividend_leaves(resolved, node_by_guid)
    if not leaves:
        return None
    paths = ", ".join(sorted(leaf.path for leaf in leaves))
    return (
        f"{DOUBLE_COUNT_ERROR_MARKER}: the book carries foreign dividends on "
        f"{paths}, and the broker report carries Rs {report_dividend_gross:,.2f} of foreign "
        "dividends that this run would add on top. Nothing was built. If the book already "
        "records the broker's dividends, set foreign_dividends_in_book to true for this "
        "entity and year; if it does not, remove the foreign flag from those book accounts."
    )


# ---------------------------------------------------------------------------
# Build the pre-relief schedule
# ---------------------------------------------------------------------------

def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def build_foreign_tax(resolved: dict, node_by_guid: dict, foreign_report=None) -> ForeignTaxSchedule:
    """Country rows (before relief). The broker report's Schedule FSI is the
    primary source when it has numeric rows; the book's tagged figures are
    used when there is no report, and otherwise only as a cross-check."""
    sched = ForeignTaxSchedule(
        book_foreign_tax=book_foreign_tax(resolved, node_by_guid),
        book_foreign_dividends=book_foreign_dividends(resolved, node_by_guid),
    )

    by_country: dict[str, ForeignTaxRow] = {}
    fsi = getattr(foreign_report, "schedule_fsi", None) if foreign_report is not None else None
    if fsi is not None and getattr(fsi, "parsed", True):
        for r in fsi.rows:
            income, paid = _num(r.income), _num(r.tax_paid_outside)
            if income is None and paid is None:
                continue
            country = (r.country or r.country_code or "").strip() or NO_COUNTRY
            row = by_country.setdefault(country, ForeignTaxRow(
                country=country, income=0.0, foreign_tax=0.0, source="broker report (Schedule FSI)"))
            row.income += income or 0.0
            row.foreign_tax += abs(paid or 0.0)
            sched.report_foreign_income += income or 0.0
            sched.report_foreign_tax += abs(paid or 0.0)
    sched.rows = list(by_country.values())

    # Foreign tax in the book.
    if sched.book_foreign_tax:
        if sched.report_foreign_tax:
            # Both exist: the report is the figure used; the book is a check.
            # Never add the two -- they describe the same withholding.
            if abs(sched.book_foreign_tax - sched.report_foreign_tax) > TOLERANCE:
                sched.warnings.append(
                    f"Foreign tax: the book's TAXPAID_FOREIGN accounts total Rs "
                    f"{sched.book_foreign_tax:,.2f} but the broker report shows Rs "
                    f"{sched.report_foreign_tax:,.2f}. The report's figure is used (the two are "
                    "never added); reconcile them before filing Form 67."
                )
        else:
            countries = [r for r in sched.rows if r.country != NO_COUNTRY]
            if len(countries) == 1:
                countries[0].foreign_tax += sched.book_foreign_tax
                countries[0].source += " + book TAXPAID_FOREIGN"
            else:
                sched.unallocated_tax += sched.book_foreign_tax
                sched.warnings.append(
                    f"Foreign tax: Rs {sched.book_foreign_tax:,.2f} is tagged TAXPAID_FOREIGN in the "
                    "book but the country cannot be told (no broker report with a single country). "
                    "NO relief is claimed on it: Form 67 is per country. Supply the broker report."
                )

    # Foreign income in the book (flagged dividends).
    if sched.book_foreign_dividends:
        if sched.report_foreign_income:
            if abs(sched.book_foreign_dividends - sched.report_foreign_income) > TOLERANCE:
                sched.warnings.append(
                    f"Foreign income: the book's foreign-flagged dividends total Rs "
                    f"{sched.book_foreign_dividends:,.2f} but the broker report's Schedule FSI shows "
                    f"Rs {sched.report_foreign_income:,.2f}. The report's figure is used for Schedule "
                    "FSI (the two are never added)."
                )
        else:
            countries = [r for r in sched.rows if r.country != NO_COUNTRY]
            if len(countries) == 1:
                countries[0].income += sched.book_foreign_dividends
                countries[0].source += " + book foreign dividends"
            else:
                sched.rows.append(ForeignTaxRow(
                    country=NO_COUNTRY, income=sched.book_foreign_dividends, foreign_tax=0.0,
                    source="book foreign dividends",
                    note="country not known: no relief can be claimed on this income",
                ))

    if sched.unallocated_tax and not any(r.country == NO_COUNTRY for r in sched.rows):
        sched.rows.append(ForeignTaxRow(
            country=NO_COUNTRY, income=0.0, foreign_tax=sched.unallocated_tax,
            source="book TAXPAID_FOREIGN",
            note="country not known: no relief can be claimed on this tax",
        ))
    elif sched.unallocated_tax:
        for r in sched.rows:
            if r.country == NO_COUNTRY:
                r.foreign_tax += sched.unallocated_tax
                r.source += " + book TAXPAID_FOREIGN"
    return sched


# ---------------------------------------------------------------------------
# Relief
# ---------------------------------------------------------------------------

def apply_relief(sched: ForeignTaxSchedule, tax_liability: float, total_income: float) -> float:
    """Fill in Indian tax and relief per country; return the total relief.

    Indian tax on a country's income = income * average rate, where the
    average rate = tax liability / total income (capped at 1). Relief per
    country = min(foreign tax, that Indian tax); never negative; never for a
    row with no country; total never above the tax liability."""
    sched.relief_computed = True
    tax_liability = max(0.0, tax_liability or 0.0)
    total_income = total_income or 0.0
    rate = min(1.0, tax_liability / total_income) if total_income > 0 and tax_liability > 0 else 0.0
    sched.average_rate = rate

    total = 0.0
    for r in sched.rows:
        base = max(0.0, min(r.income, total_income)) if total_income > 0 else 0.0
        r.indian_tax = base * rate
        if r.country == NO_COUNTRY:
            r.relief = 0.0
            continue
        r.relief = max(0.0, min(r.foreign_tax, r.indian_tax))
        total += r.relief

    if total > tax_liability and total > 0:
        scale = tax_liability / total
        for r in sched.rows:
            r.relief *= scale
        total = tax_liability
    sched.relief_total = total
    return total
