"""
reconcile_books.py — Phase C: the PRIMARY reconciliation, AIS <-> GnuCash
POSTED ENTRIES (the books). Pure functions over already-parsed inputs (the
tools/agent layer does parse_book + load_mapping's file I/O) — no I/O here,
no Data/ access.

GnuCash is treated as the SOURCE OF TRUTH ("books is primary"): the loud
call-out this module cares about most is "AIS shows income the books never
recorded" (a missing posting), followed by "books show income AIS never
reported" (a books mislabel or an AIS gap), followed by the TDS-credit
tie-out.

ARCHITECTURE: reads the GnuCash account tree directly and resolves each
account to an ITR tag by NEAREST-ANCESTOR lookup over parent_guid chains
using the entity's mapping.yaml — no eguile HTML involved. A tag placed on a
parent account naturally covers all of its untagged descendants, because
every descendant's own FY sum is walked up to the same nearest mapped
ancestor and added to that tag's bucket.

AIS INCOME SOURCE — IMPORTANT DESIGN NOTE (v1 scope limitation):
  AIS income is taken ONLY from tdsTcs elements' per-category income
  (ElementReco.detail_sum, i.e. the summed amtPaid on which TDS was
  deducted), not from sft/other-info. This is deliberate: AIS routinely
  ECHOES the same underlying income event under more than one info-source
  (e.g. interest reported once under tdsTcs and again under an sft
  statement), and summing across sections would double-count it. The
  consequence: income that had NO TDS deducted at all (exempt income,
  interest below the TDS threshold, etc.) is OUT OF SCOPE for this income
  tie-out in v1 — such income won't appear in ais_by_category no matter how
  it appears in the books. This is a known, intentional v1 gap, not a bug;
  a future phase could widen the AIS side to include sft/other-info with
  proper de-duplication against tdsTcs.

Category buckets mirror reconcile_26as.py's categories (interest / dividend
/ salary / other) via the shared _classify_ais_category classifier, imported
from reconcile_26as rather than redefined here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agents.skill_ais_reconcile.reconcile import AisRecoReport, ElementReco, TOLERANCE, _as_decimal
from agents.skill_ais_reconcile.reconcile_26as import _classify_ais_category

# Tag groups (mirrors src/agents/skill_itr_workbook/scripts/tags.py's TAGS
# keys, listed explicitly here rather than imported wholesale so this module
# only depends on the tag NAMES it actually cross-checks, not the full TAGS
# registry/schema).
INTEREST_TAGS = {
    "OS_INTEREST_SB", "OS_INTEREST_BANK", "OS_INTEREST_NBFC",
    "OS_INTEREST_EPF_TAXABLE", "OS_REFUND_INTEREST",
}
DIVIDEND_TAGS = {"OS_DIVIDEND"}
SALARY_TAGS = {"SALARY_GROSS"}
TDS_CREDIT_TAGS = {"TAXPAID_TDS_SALARY", "TAXPAID_TDS_INTEREST", "TAXPAID_TDS_DIVIDEND"}

# tag -> AIS-category, used to fold per-tag books totals into the same
# interest/dividend/salary buckets the AIS side uses.
_TAG_TO_CATEGORY: dict[str, str] = {}
for _tag in INTEREST_TAGS:
    _TAG_TO_CATEGORY[_tag] = "interest"
for _tag in DIVIDEND_TAGS:
    _TAG_TO_CATEGORY[_tag] = "dividend"
for _tag in SALARY_TAGS:
    _TAG_TO_CATEGORY[_tag] = "salary"


@dataclass
class CategoryBooksTieOut:
    """AIS vs books income comparison for one category ('interest' /
    'dividend' / 'salary'), for whichever categories appear on EITHER side
    (union)."""
    category: str
    ais_income: Decimal
    books_income: Decimal
    delta: Decimal
    flagged: bool


@dataclass
class AisBooksReport:
    categories: list[CategoryBooksTieOut] = field(default_factory=list)

    ais_tds_credit: Decimal = Decimal("0")
    books_tds_credit: Decimal = Decimal("0")
    delta_tds: Decimal = Decimal("0")
    flag_tds_mismatch: bool = False

    # Completeness call-outs (the loud "you haven't booked / reported this"
    # lists) -- category names, not amounts, since the amount already lives
    # in `categories` above.
    books_income_not_in_ais: list[str] = field(default_factory=list)
    ais_income_not_in_books: list[str] = field(default_factory=list)

    # Count of INCOME-type accounts with a nonzero FY sum that resolved to
    # NO tag at all (no mapped ancestor) -- surfaces books income entirely
    # outside the mapping, which the category tie-out above cannot see
    # because it has no tag to bucket into.
    untagged_income_account_count: int = 0

    @property
    def flagged_categories(self) -> list[str]:
        return [c.category for c in self.categories if c.flagged]


def _resolve_tag(guid: str, accounts: dict, mapping_entries: dict) -> str | None:
    """Nearest-ancestor tag resolution: walk guid -> parent_guid -> ... ,
    including the account itself, and return the first one present in
    mapping_entries. A tag on a parent covers every untagged descendant this
    way. Returns None if no ancestor (including the account itself) is
    mapped."""
    seen = set()
    current = guid
    while current is not None and current not in seen:
        seen.add(current)
        entry = mapping_entries.get(current)
        if entry is not None:
            return entry.tag
        acct = accounts.get(current)
        current = acct.parent_guid if acct is not None else None
    return None


def _books_totals_by_tag(book, mapping, year_key: str) -> tuple[dict[str, Decimal], int]:
    """Sum account_fy_sum into resolved-tag buckets, plus the count of
    untagged INCOME accounts with a nonzero FY sum.

    SIGN NOTE for TDS-credit tags: account_fy_sum applies parse_gnucash's ITR
    presentation-sign normalization, which FLIPS FLIP_TYPES accounts
    (INCOME/EXPENSE/LIABILITY/EQUITY/CREDIT/PAYABLE). Income accounts flip to
    a positive presentation, which is exactly what the income tie-out wants.
    But TDS credit is a tax-WITHHELD amount -- a positive credit against tax
    payable -- and in real books it is commonly posted to an EXPENSE account
    ("TDS on Interest", "TDS on Dividend"), whose debit the flip turns
    NEGATIVE. For TDS_CREDIT_TAGS we therefore un-flip FLIP_TYPES accounts so
    the credit lands debit-positive regardless of whether the book models TDS
    as an EXPENSE or as an ASSET receivable (ASSET is not a FLIP_TYPE, so it
    is already debit-positive and left as-is). This keeps the TDS bucket on
    the same positive footing as the AIS and 26AS sides it's compared to. The
    income tags in by_tag stay presentation-normalized -- the two never share
    a bucket (see _books_by_category vs _books_tds_credit)."""
    from agents.skill_itr_workbook.scripts.parse_gnucash import account_fy_sum, FLIP_TYPES

    by_tag: dict[str, Decimal] = {}
    untagged_income_count = 0

    for guid, acct in book.accounts.items():
        tag = _resolve_tag(guid, book.accounts, mapping.entries)
        fy_sum = _as_decimal(account_fy_sum(book, guid, year_key)) or Decimal("0")
        if tag is not None:
            if tag in TDS_CREDIT_TAGS and acct.type in FLIP_TYPES:
                fy_sum = -fy_sum
            by_tag[tag] = by_tag.get(tag, Decimal("0")) + fy_sum
        elif acct.type == "INCOME" and fy_sum != Decimal("0"):
            untagged_income_count += 1

    return by_tag, untagged_income_count


def _books_by_category(by_tag: dict[str, Decimal]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for tag, total in by_tag.items():
        category = _TAG_TO_CATEGORY.get(tag)
        if category is None:
            continue
        out[category] = out.get(category, Decimal("0")) + total
    return out


def _books_tds_credit(by_tag: dict[str, Decimal]) -> Decimal:
    total = Decimal("0")
    for tag in TDS_CREDIT_TAGS:
        total += by_tag.get(tag, Decimal("0"))
    return total


def _ais_by_category(report: AisRecoReport) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for el in report.elements:
        if el.section_key != "tdsTcs":
            continue
        if el.detail_sum is None:
            continue
        category = _classify_ais_category(el.category)
        out[category] = out.get(category, Decimal("0")) + el.detail_sum
    return out


def reconcile_ais_vs_books(report: AisRecoReport, book: Any, mapping: Any,
                            year_key: str) -> AisBooksReport:
    """Tie out AIS's per-category tdsTcs income (and total TDS credit)
    against the GnuCash books, with the books treated as primary. `book` is
    an already-parsed parse_gnucash.Book, `mapping` an already-loaded
    configs.MappingLoadResult -- this function does no file I/O itself.
    """
    by_tag, untagged_income_count = _books_totals_by_tag(book, mapping, year_key)
    books_cat = _books_by_category(by_tag)
    books_tds = _books_tds_credit(by_tag)

    ais_cat = _ais_by_category(report)
    ais_tds = report.total_tds_credit

    categories: list[CategoryBooksTieOut] = []
    books_income_not_in_ais: list[str] = []
    ais_income_not_in_books: list[str] = []

    for category in sorted(set(ais_cat) | set(books_cat)):
        a = ais_cat.get(category, Decimal("0"))
        b = books_cat.get(category, Decimal("0"))
        d = a - b
        categories.append(CategoryBooksTieOut(
            category=category, ais_income=a, books_income=b, delta=d,
            flagged=abs(d) > TOLERANCE,
        ))
        if b != Decimal("0") and a == Decimal("0"):
            books_income_not_in_ais.append(category)
        if a != Decimal("0") and b == Decimal("0"):
            ais_income_not_in_books.append(category)

    delta_tds = ais_tds - books_tds
    flag_tds_mismatch = abs(delta_tds) > TOLERANCE

    return AisBooksReport(
        categories=categories,
        ais_tds_credit=ais_tds,
        books_tds_credit=books_tds,
        delta_tds=delta_tds,
        flag_tds_mismatch=flag_tds_mismatch,
        books_income_not_in_ais=books_income_not_in_ais,
        ais_income_not_in_books=ais_income_not_in_books,
        untagged_income_account_count=untagged_income_count,
    )
