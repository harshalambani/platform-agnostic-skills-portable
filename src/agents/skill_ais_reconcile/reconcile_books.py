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

AIS INCOME SOURCE (LEG-02): the AIS side counts the TDS-section (tdsTcs) income of every row with an
  amount paid, including nil-TDS rows, PLUS interest and dividend reported under the SFT section
  (dividend; interest from deposit; savings interest; interest from others). AIS routinely echoes one
  income event under both sections, so each SOURCE is counted ONCE: when the same source (a TAN or a
  normalised payer name, whichever the rows carry) and the same head appear in both, the TDS-section
  amount is used and the SFT amount is not added; the report says which section each amount came from.
  The source is the payer name in the element's l2 "Information Source" value (ids differ between the
  sections: TAN in tdsTcs, PAN in sft), compared after dropping any bracketed id, punctuation and filler
  words. A payer whose name only shares its first word with a TDS payer is NOT added and is listed as a
  possible same payer for the user to check. An SFT row whose source cannot be identified is never added
  on top of a head that already has TDS-section income (it could be the same income); it is listed as
  not counted. Securities purchase and sale SFT rows never enter income. Callers pass the NormalizedAis as `normalized` for the SFT
  widening; without it only the TDS section is counted, as before.

Category buckets mirror reconcile_26as.py's categories (interest / dividend
/ salary / other) via the shared _classify_ais_category classifier, imported
from reconcile_26as rather than redefined here.
"""
from __future__ import annotations

import re
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
    from_tds_section: Decimal = Decimal("0")     # LEG-02: of ais_income, counted from the TDS section
    from_sft_only: Decimal = Decimal("0")        # LEG-02: of ais_income, counted from SFT (source not in TDS)


@dataclass
class AisBooksReport:
    categories: list[CategoryBooksTieOut] = field(default_factory=list)

    ais_tds_credit: Decimal = Decimal("0")
    uncounted_tds: list = field(default_factory=list)  # ITR-22, from the AIS report
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

    # LEG-02: one line per source/head saying which section its AIS amount was counted from, and which
    # SFT amounts were NOT counted (same source already in the TDS section, or source unidentifiable).
    source_notes: list[str] = field(default_factory=list)
    # LEG-02: SFT payers that share a first word with a TDS payer: not added, listed for the user to check.
    possible_same_payers: list[str] = field(default_factory=list)

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


_NAME_FIELDS = ("deductorName", "payerName", "nameOfPayer", "nameOfDeductor", "reportingEntityName",
                "nameOfReportingEntity", "sourceName", "name", "Name")
_TAN_FIELDS = ("tan", "TAN", "deductorTan", "payerTan", "reportingEntityTan")
_SFT_EXCLUDE = ("purchase", "sale", "redemption", "buyback", "buy back")

# Words dropped from a payer name before comparing, so "Alpha Finance Ltd" and "ALPHA FINANCE LIMITED"
# are the same payer. Keep this list small; it is the only place filler words are defined.
_NAME_FILLER = frozenset({"LIMITED", "LTD", "PRIVATE", "PVT", "THE", "CO", "COMPANY", "AND",
                          "INC", "CORP", "CORPORATION"})
_SOURCE_LABEL = "informationsource"        # "Information Source" matched ignoring case, spaces, underscores


def _norm_name(value: Any) -> str:
    """Upper-case, strip a trailing bracketed id, punctuation to spaces, drop filler words.
    Empty result = the name is unusable (counts as unidentified)."""
    text = re.sub(r"\([^()]*\)\s*$", "", str(value).strip())
    words = "".join(ch if ch.isalnum() else " " for ch in text.upper()).split()
    return " ".join(w for w in words if w not in _NAME_FILLER)


def _element_key(row: Any) -> tuple:
    return (row.section_key, row.category, row.info_src_id)


def _element_names(normalized: Any) -> dict:
    """element -> normalised payer name, read from the element's l2 'Information Source' value."""
    out: dict = {}
    for r in normalized.rows:
        if r.level != "l2":
            continue
        key = _element_key(r)
        if out.get(key):
            continue
        for label, value in r.fields.items():
            if re.sub(r"[\s_]+", "", str(label)).lower() == _SOURCE_LABEL and value is not None:
                name = _norm_name(value)
                if name:
                    out[key] = name
                break
    return out


def _identities(row: Any, element_names: dict | None = None) -> set:
    """The source identities of a row. The element's l2 payer name wins; otherwise a TAN and/or a payer
    name carried on the l1 row itself. Empty = unidentifiable."""
    out = set()
    name = (element_names or {}).get(_element_key(row))
    if name:
        out.add("NAME:" + name)
        return out
    for f in _TAN_FIELDS:
        v = row.fields.get(f)
        if v is not None and str(v).strip():
            out.add("TAN:" + str(v).strip().upper())
    for f in _NAME_FIELDS:
        v = row.fields.get(f)
        if v is not None and _norm_name(v):
            out.add("NAME:" + _norm_name(v))
    return out


def _first_word(ident: str) -> str:
    return ident.split(":", 1)[-1].split(" ", 1)[0] if ident.startswith("NAME:") else ""


def _row_amount(row: Any) -> Decimal | None:
    if not row.feedback_editable_field:
        return None
    return _as_decimal(row.fields.get(row.feedback_editable_field))


def _ais_by_category(report: AisRecoReport, normalized: Any = None):
    """(total by category, TDS-section part, SFT-only part, notes, possible-same-payer notes).
    Each source is counted once; a possibly-same payer is neither added nor silently dropped."""
    tds: dict[str, Decimal] = {}
    for el in report.elements:
        if el.section_key != "tdsTcs" or el.detail_sum is None:
            continue
        category = _classify_ais_category(el.category)
        tds[category] = tds.get(category, Decimal("0")) + el.detail_sum
    sft: dict[str, Decimal] = {}
    notes: list[str] = []
    possible: list[str] = []
    if normalized is None:
        return dict(tds), tds, sft, notes, possible

    names = _element_names(normalized)
    tds_ids_by_head: dict[str, set] = {}
    for r in normalized.rows:
        if r.section_key == "tdsTcs" and r.level == "l1":
            tds_ids_by_head.setdefault(_classify_ais_category(r.category), set()).update(
                _identities(r, names))

    sft_groups: dict[tuple, Decimal] = {}
    near_with: dict[tuple, str] = {}
    order: list[tuple] = []
    for r in normalized.rows:
        if r.section_key != "sft" or r.level != "l1":
            continue
        low = (r.category or "").lower()
        head = _classify_ais_category(r.category)
        if head not in ("interest", "dividend") or any(w in low for w in _SFT_EXCLUDE):
            continue                        # securities purchase/sale and everything else stays OUT
        amt = _row_amount(r)
        if amt is None:
            continue
        ids = _identities(r, names)
        tds_ids = tds_ids_by_head.get(head, set())
        shared = ids & tds_ids
        near = sorted(t for t in tds_ids for i in ids
                      if _first_word(i) and _first_word(i) == _first_word(t) and t not in ids)
        if shared:
            key = (head, "dup", min(shared))
        elif near:
            key = (head, "near", min(ids))
            near_with[key] = near[0]
        elif ids:
            key = (head, "new", min(ids))
        else:
            key = (head, "unknown", "")
        if key not in sft_groups:
            order.append(key)
        sft_groups[key] = sft_groups.get(key, Decimal("0")) + amt

    for head in sorted(tds):
        notes.append(f"{head}: {tds[head]:,.2f} counted from the TDS section")
    for key in order:
        head, kind, ident = key
        amt = sft_groups[key]
        label = ident.split(":", 1)[-1].title() if ident else "(source not identified)"
        if kind == "dup":
            notes.append(f"{head}: {label} is in both sections; counted from the TDS section, the SFT "
                         f"amount {amt:,.2f} is NOT added (counted once)")
        elif kind == "near":
            other = near_with[key].split(":", 1)[-1].title()
            msg = (f"{head}: SFT payer {label} ({amt:,.2f}) may be the same payer as TDS-section payer "
                   f"{other}; the SFT amount is NOT added until you check")
            notes.append(msg)
            possible.append(msg)
        elif kind == "unknown" and tds.get(head, Decimal("0")) != Decimal("0"):
            notes.append(f"{head}: SFT amount {amt:,.2f} from a source that cannot be identified is NOT "
                         f"counted, because the TDS section already reports {head} income and it may be "
                         f"the same income (counted once)")
        else:
            sft[head] = sft.get(head, Decimal("0")) + amt
            notes.append(f"{head}: {label} {amt:,.2f} counted from SFT (not in the TDS section)")
    total = dict(tds)
    for head, v in sft.items():
        total[head] = total.get(head, Decimal("0")) + v
    return total, tds, sft, notes, possible


def reconcile_ais_vs_books(report: AisRecoReport, book: Any, mapping: Any,
                            year_key: str, normalized: Any = None) -> AisBooksReport:
    """Tie out AIS's per-category tdsTcs income (and total TDS credit)
    against the GnuCash books, with the books treated as primary. `book` is
    an already-parsed parse_gnucash.Book, `mapping` an already-loaded
    configs.MappingLoadResult -- this function does no file I/O itself.
    """
    by_tag, untagged_income_count = _books_totals_by_tag(book, mapping, year_key)
    books_cat = _books_by_category(by_tag)
    books_tds = _books_tds_credit(by_tag)

    ais_cat, ais_tds_part, ais_sft_part, source_notes, possible_same = _ais_by_category(report, normalized)
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
            from_tds_section=ais_tds_part.get(category, Decimal("0")),
            from_sft_only=ais_sft_part.get(category, Decimal("0")),
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
        uncounted_tds=list(getattr(report, "uncounted_tds", []) or []),
        books_tds_credit=books_tds,
        delta_tds=delta_tds,
        flag_tds_mismatch=flag_tds_mismatch,
        books_income_not_in_ais=books_income_not_in_ais,
        ais_income_not_in_books=ais_income_not_in_books,
        untagged_income_account_count=untagged_income_count,
        source_notes=source_notes,
        possible_same_payers=possible_same,
    )
