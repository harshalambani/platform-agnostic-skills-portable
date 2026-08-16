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

Form16<->26AS cross-check (GAP B): compares what the employer's Form 16
SAYS it deducted (net tax payable, per TAN -- primary certificate plus every
successfully parsed extra certificate from GAP A) against what the
government's own 26AS records show was actually deposited under section
192/192A for that TAN. This is deliberately independent of the Book<->Form16
check above -- a mismatch here means Form16 and 26AS disagree with each
other, regardless of what the books say.
"""
from __future__ import annotations

from dataclasses import dataclass

import as26 as as26_engine
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


# Rupee-rounding tolerance -- matches schedules.py's _TIE_OUT_TOLERANCE used
# for the other Form16/26AS-derived tie-outs (looser than the 0.01 used for
# pure within-Form16 arithmetic identities, since 26AS figures come from a
# separate government system with its own rounding).
_SALARY_TIE_OUT_TOLERANCE = 1.0


@dataclass
class Form16As26SalaryResult:
    tan: str
    form16_tds: float | None    # None => no successfully parsed Form16 certificate for this TAN
    as26_tds: float

    @property
    def ok(self) -> bool:
        if self.form16_tds is None:
            return False
        return abs(self.form16_tds - self.as26_tds) <= _SALARY_TIE_OUT_TOLERANCE


def cross_check_form16_26as_salary(form16, as26_data, tds_sections: dict) -> list[Form16As26SalaryResult]:
    """Compare each employer TAN's Form16 salary TDS (net tax payable, item
    21 -- primary certificate plus every successfully parsed GAP-A extra
    certificate) against the sum of that TAN's section 192/192A ("salary")
    tax actually deducted per 26AS. The single highest-value check in this
    area: Form16 is what the employer SAYS it deducted, 26AS is what the
    government's own records show was actually deposited -- a mismatch is a
    real filing risk, not a cosmetic difference, and is never silently
    dropped: a TAN with 26AS salary TDS but no successfully parsed Form16
    certificate for it is reported as a mismatch (`form16_tds=None`), not
    omitted. Returns [] if there is no Form16 or no 26AS data to compare."""
    if form16 is None or as26_data is None:
        return []

    form16_tds_by_tan: dict[str, float] = {}
    if form16.tan is not None and form16.net_tax_payable_21 is not None:
        form16_tds_by_tan[form16.tan] = form16.net_tax_payable_21
    for c in form16.extra_certificates:
        if c.parsed and c.tan is not None and c.tds_net_tax_payable_21 is not None:
            form16_tds_by_tan[c.tan] = c.tds_net_tax_payable_21

    as26_tds_by_tan: dict[str, float] = {}
    for txn in as26_data.transactions:
        if as26_engine.classify_section(txn.section, tds_sections) != "salary":
            continue
        as26_tds_by_tan[txn.tan] = as26_tds_by_tan.get(txn.tan, 0.0) + txn.tax_deducted

    all_tans = set(form16_tds_by_tan) | set(as26_tds_by_tan)
    results = [
        Form16As26SalaryResult(
            tan=tan, form16_tds=form16_tds_by_tan.get(tan), as26_tds=as26_tds_by_tan.get(tan, 0.0),
        )
        for tan in sorted(all_tans)
    ]
    return results


def summarize_form16_26as_salary(results: list[Form16As26SalaryResult]) -> str:
    if not results:
        return "Form16<->26AS cross-check (s.192 salary TDS): nothing to compare (no Form16, or no 26AS data)."
    mismatches = [r for r in results if not r.ok]
    lines = [
        f"Form16<->26AS cross-check (s.192 salary TDS): {len(results)} check(s), "
        f"{len(mismatches)} mismatch(es)."
    ]
    for r in results:
        status = "OK" if r.ok else "MISMATCH"
        form16_str = f"{r.form16_tds:.2f}" if r.form16_tds is not None else "no parsed Form16 certificate"
        lines.append(f"  {status} TAN {r.tan}: form16={form16_str} 26AS={r.as26_tds:.2f}")
    return "\n".join(lines)
