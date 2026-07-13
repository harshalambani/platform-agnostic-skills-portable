"""
verify.py -- book <-> HTML cross-check (plan section 1.2, point 4) and the
Book<->Form16 cross-checks (plan section 6.2).

For every income/expense account GUID present in BOTH the parsed eguile HTML
(leaf nodes under the Retained Earnings section) and the .gnucash book, the
book-derived FY sum must equal the HTML's Retained-Earnings line for that
GUID (+/- 0.01). Only leaf accounts are compared -- subtotal/branch rows are
already covered by parse_eguile's own "Total X == sum of children" identity
check and would require a recursive book-side roll-up to compare meaningfully.

The Form16 cross-checks compare the resolved mapping model (mapping.py) --
not the raw book -- against the parsed Form 16 (parse_form16.py): 17(1) gross
salary should equal the sum of every leaf tagged SALARY_GROSS, and Part B's
net tax payable should equal the sum of every leaf tagged TAXPAID_TDS_SALARY
(sign-flipped back to positive, since TDS is booked as a negative expense).
"""
from __future__ import annotations

from dataclasses import dataclass

import parse_eguile as pe
import parse_gnucash as pg


@dataclass
class CrossCheckResult:
    guid: str
    name: str
    html_total: float
    book_total: float

    @property
    def ok(self) -> bool:
        return abs(self.html_total - self.book_total) <= 0.01


def cross_check(
    tree: pe.ParsedBalanceSheet, book: pg.Book, year_key: str,
) -> list[CrossCheckResult]:
    results = []
    for node in tree.all_nodes():
        if node.guid is None or node.children:
            continue
        if not node.section.startswith("RetainedEarnings"):
            continue
        if node.guid not in book.accounts:
            continue
        html_total = node.total if node.total is not None else 0.0
        book_total = pg.account_fy_sum(book, node.guid, year_key)
        results.append(CrossCheckResult(
            guid=node.guid, name=node.name, html_total=html_total, book_total=book_total,
        ))
    return results


def summarize(results: list[CrossCheckResult]) -> str:
    if not results:
        return "Book<->HTML cross-check: no matching GUIDs found (nothing to compare)."
    mismatches = [r for r in results if not r.ok]
    lines = [f"Book<->HTML cross-check: {len(results)} account(s) compared, "
             f"{len(mismatches)} mismatch(es)."]
    for r in mismatches:
        lines.append(
            f"  MISMATCH {r.name} ({r.guid}): HTML={r.html_total:.2f} "
            f"Book={r.book_total:.2f} diff={r.html_total - r.book_total:.2f}"
        )
    if not mismatches:
        lines.append("  OK -- all compared accounts reconcile.")
    return "\n".join(lines)


@dataclass
class Form16CrossCheckResult:
    label: str
    mapped_total: float
    form16_total: float

    @property
    def ok(self) -> bool:
        return abs(self.mapped_total - self.form16_total) <= 0.01


def cross_check_form16(tree: pe.ParsedBalanceSheet, resolved: dict, form16) -> list[Form16CrossCheckResult]:
    """Compare the resolved mapping model's SALARY_GROSS / TAXPAID_TDS_SALARY
    leaf totals against the parsed Form16Data's 17(1) and net tax payable.
    `resolved` is mapping.ResolutionResult.resolved (guid -> ResolvedLeaf).
    Returns [] if either side has nothing to compare (form16 is None, or no
    leaf carries the relevant tag)."""
    if form16 is None:
        return []

    node_by_guid = {n.guid: n for n in tree.all_nodes() if n.guid}

    def _sum_tag(tag: str) -> float:
        return sum(
            node_by_guid[leaf.guid].total or 0.0
            for leaf in resolved.values()
            if leaf.tag == tag and leaf.guid in node_by_guid
        )

    results = []
    if form16.s17_1 is not None:
        results.append(Form16CrossCheckResult(
            label="17(1) Salary vs SALARY_GROSS", mapped_total=_sum_tag("SALARY_GROSS"),
            form16_total=form16.s17_1,
        ))
    if form16.net_tax_payable_21 is not None:
        # TDS-on-salary is booked (and sign-flipped) as a negative expense in
        # the HTML/book -- flip back to compare against Form16's positive
        # net tax payable figure.
        results.append(Form16CrossCheckResult(
            label="Net tax payable vs TAXPAID_TDS_SALARY", mapped_total=abs(_sum_tag("TAXPAID_TDS_SALARY")),
            form16_total=form16.net_tax_payable_21,
        ))
    return results


def summarize_form16(results: list[Form16CrossCheckResult]) -> str:
    if not results:
        return "Book<->Form16 cross-check: nothing to compare (no Form16, or no SALARY_GROSS/TAXPAID_TDS_SALARY tags)."
    mismatches = [r for r in results if not r.ok]
    lines = [f"Book<->Form16 cross-check: {len(results)} check(s), {len(mismatches)} mismatch(es)."]
    for r in results:
        status = "OK" if r.ok else "MISMATCH"
        lines.append(f"  {status} {r.label}: mapped={r.mapped_total:.2f} form16={r.form16_total:.2f}")
    return "\n".join(lines)
