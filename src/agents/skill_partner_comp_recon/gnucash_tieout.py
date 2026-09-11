"""
gnucash_tieout.py -- Sections B and C of the tie-out work: comparing the
journal this run WOULD produce (jv_emitter.build_journals, pure, no I/O)
against an already-posted GnuCash book, read-only.

  - Section B (build_posted_check): per-journal "posted already?" detector,
    to stop a CSV re-import from double-posting. A DEFINITE hit is a book
    transaction whose trn:num equals the journal's Transaction ID
    (jv_emitter's Number column lands there on import) -- but its ABSENCE
    proves nothing (the book may simply not have that transaction's num
    populated, or may not have been imported via this CSV convention at
    all). The fallback is an EXACT (2dp-rounded, no float tolerance) match
    of every split's (date, signed amount, colon account path) against a
    single book transaction on that date.
  - Section C (build_balance_tieout): for each of jv_emitter.ACCOUNT_KEYS,
    compares this run's implied FY movement for that account (summed
    straight off build_journals()'s splits, Dr+/Cr- -- the SAME raw
    convention as a GnuCash split's raw value, per parse_gnucash.py's own
    docstring) against the book's actual FY movement
    (parse_gnucash.account_fy_sum(), which is raw-summed then
    presentation-sign-normalized via normalize_value()/FLIP_TYPES). Both
    sides are pushed through normalize_value() so they are compared on the
    SAME (presentation) sign convention -- reusing parse_gnucash.py's own
    fy_window/fy_transactions/account_fy_sum/normalize_value, never a
    hand-rolled date filter or sign flip.

Both functions are read-only end to end: the only GnuCash access is
parse_gnucash.parse_book(), which never opens a write handle. Both degrade
every unavailable input (no book, no accounts configured, an account path
that doesn't resolve in the book, a book that fails to parse, a journal
that fails to build) to an explicit note/CANNOT-RECONCILE result -- never
a crash, never a silent 0.0/False.

Account-path convention note: parse_gnucash.py's own Account.path is
"/"-separated (_build_paths()) -- a DIFFERENT convention from
entities.yaml's/jv_emitter.py's ":"-separated paths. _colon_paths() below
is an ADDITIVE local resolver (mirrors _build_paths()'s parent-chain
recursion, joined with ":" instead of "/") built purely for this module;
it does not touch or replace parse_gnucash.py's own "/"-path convention.
"""
from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .engine import CANNOT_RECONCILE, ReconciliationResult, reconcile_category
from .jv_emitter import ACCOUNT_KEYS, JournalValidationError, build_journals
from .jv_emitter import _strip_root as _jv_strip_root

_ITR_SCRIPTS = Path(__file__).resolve().parent.parent / "skill_itr_workbook" / "scripts"
if str(_ITR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ITR_SCRIPTS))
import parse_gnucash  # noqa: E402

_TIEOUT_LABEL = "GnuCash tie-out"
_POSTED_LABEL = "Posted-already check"


# ---------------------------------------------------------------------------
# Shared: colon-path resolver (additive -- see module docstring).
# ---------------------------------------------------------------------------

def _colon_paths(book: "parse_gnucash.Book") -> dict[str, str]:
    """guid -> ':'-separated account path, mirroring
    parse_gnucash._build_paths()'s parent-chain recursion but joined with
    ':' (jv_emitter.py's/entities.yaml's convention) instead of '/'. The
    ROOT account itself is never included in a child's path, matching
    _build_paths()'s own "parent is ROOT -> just the leaf name" rule."""
    paths: dict[str, str] = {}

    def _resolve(guid: str) -> str:
        if guid in paths:
            return paths[guid]
        acct = book.accounts[guid]
        parent = book.accounts.get(acct.parent_guid) if acct.parent_guid else None
        if parent is None or parent.type == "ROOT":
            paths[guid] = acct.name
        else:
            paths[guid] = f"{_resolve(acct.parent_guid)}:{acct.name}"
        return paths[guid]

    for guid in book.accounts:
        _resolve(guid)
    return paths


def _load_book_safely(gnucash_path: str):
    """Returns (book, error_or_None). Never raises."""
    try:
        return parse_gnucash.parse_book(gnucash_path), None
    except Exception as e:
        return None, f"could not open/parse GnuCash book {gnucash_path!r}: {e}"


def _journals_safely(report, accounts: dict):
    """Returns (journals, error_or_None). Never raises."""
    try:
        return build_journals(report, accounts or {}), None
    except JournalValidationError as e:
        return None, f"could not build this run's implied journal: {e}"


# ---------------------------------------------------------------------------
# Section C -- balance tie-out.
# ---------------------------------------------------------------------------

def build_balance_tieout(
    report, accounts: dict, gnucash_path: str, year_key: str,
) -> list[ReconciliationResult]:
    """One ReconciliationResult per jv_emitter.ACCOUNT_KEYS entry, comparing
    this run's implied FY movement against the GnuCash book's actual FY
    movement for the configured account path. Appendable directly onto
    report.reconciliation -- the existing Reconciliation/Exceptions/Open
    items sheets then pick these up with no writer.py sheet-shape changes.

    Every source that cannot be resolved (no book, no accounts configured,
    an account path absent from the book, an unbuildable journal) degrades
    that account's sources to None -- reconcile_category() already renders
    that as the package's "CANNOT RECONCILE -- missing: ..." shape.
    """
    def _blank(note: str | None = None) -> list[ReconciliationResult]:
        results = []
        for key in ACCOUNT_KEYS:
            sources = {
                "Computed (this run's journal)": None,
                "GnuCash book (FY movement)": None,
            }
            if note is None:
                results.append(reconcile_category(f"{_TIEOUT_LABEL}: {key}", sources))
            else:
                results.append(ReconciliationResult(
                    category=f"{_TIEOUT_LABEL}: {key}", sources=sources, agree=None,
                    note=f"{CANNOT_RECONCILE} -- {note}",
                ))
        return results

    if not gnucash_path:
        return _blank("no GnuCash book supplied")
    if not accounts:
        return _blank("no partner_comp_accounts configured for this entity")

    book, err = _load_book_safely(gnucash_path)
    if err:
        return _blank(err)

    journals, err = _journals_safely(report, accounts)
    if err:
        return _blank(err)

    colon_paths = _colon_paths(book)
    path_to_guid: dict[str, str] = {}
    for guid, path in colon_paths.items():
        path_to_guid.setdefault(path, guid)

    results: list[ReconciliationResult] = []
    for key in ACCOUNT_KEYS:
        category = f"{_TIEOUT_LABEL}: {key}"
        configured_path = accounts.get(key)
        if not isinstance(configured_path, str) or not configured_path.strip():
            results.append(reconcile_category(category, {
                "Computed (this run's journal)": None,
                "GnuCash book (FY movement)": None,
            }))
            continue
        stripped_path = _jv_strip_root(configured_path)
        guid = path_to_guid.get(stripped_path)
        if guid is None:
            sources = {"Computed (this run's journal)": None, "GnuCash book (FY movement)": None}
            results.append(ReconciliationResult(
                category=category, sources=sources, agree=None,
                note=(f"{CANNOT_RECONCILE} -- account path {stripped_path!r} "
                      "not found in the supplied GnuCash book"),
            ))
            continue

        computed_raw = sum(
            s.debit - s.credit
            for j in journals
            for s in j.splits
            if s.account == stripped_path
        )
        acct_type = book.accounts[guid].type
        computed_figure = parse_gnucash.normalize_value(computed_raw, acct_type)
        book_figure = parse_gnucash.account_fy_sum(book, guid, year_key)

        result = reconcile_category(category, {
            "Computed (this run's journal)": computed_figure,
            "GnuCash book (FY movement)": book_figure,
        })
        # Closed-book income-sweep-to-Equity limitation: GnuCash's standard
        # "close the books" behaviour sweeps an INCOME/EXPENSE-type
        # account's balance to Equity at year-end, so a genuinely-posted
        # year can still show a 0.00 FY movement on the account itself.
        # Downgrade what would otherwise read as a hard VARIANCE to an
        # explicit, non-crashing limitation note rather than a false
        # positive.
        if (
            result.agree is False
            and acct_type in parse_gnucash.FLIP_TYPES
            and book_figure == 0.0
            and computed_figure != 0.0
        ):
            result = ReconciliationResult(
                category=category, sources=result.sources, agree=None,
                note=(
                    f"Book FY movement is 0.00 for this {acct_type} account while the "
                    f"computed figure is {computed_figure:,.2f} -- likely a closed-book "
                    "income/expense sweep to Equity at year-end (GnuCash's standard "
                    "close-the-books behaviour), not a real discrepancy. Verify against "
                    "the book's Equity account movement before treating this as a "
                    "variance."
                ),
            )
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Section B -- posted-already check.
# ---------------------------------------------------------------------------

NOT_POSTED = "NOT POSTED"
ALREADY_POSTED = "ALREADY POSTED"
PARTIALLY_POSTED = "PARTIALLY POSTED -- AMBIGUOUS"
CANNOT_CHECK = "CANNOT CHECK"


@dataclass
class PostedCheckResult:
    txn_id: str
    date: str
    description: str
    status: str
    detail: str


def _fallback_classify(journal, fy_txns, colon_paths: dict) -> tuple[str, str]:
    """Exact (2dp-rounded) date+amount+account fallback match, used only
    when no trn:num hit exists. Returns (status, detail)."""
    j_splits = Counter(
        (round(s.debit - s.credit, 2), s.account) for s in journal.splits
    )
    same_day = [t for t in fy_txns if t.date_posted.isoformat() == journal.date]

    for t in same_day:
        book_splits = Counter(
            (round(float(sp.value), 2), colon_paths.get(sp.account_guid, ""))
            for sp in t.splits
        )
        if book_splits == j_splits:
            return ALREADY_POSTED, (
                f"Matched book transaction {t.guid!r} on {journal.date} by exact "
                "date + signed amount + account for every split (no trn:num match)."
            )

    all_same_day_splits: Counter = Counter()
    for t in same_day:
        for sp in t.splits:
            all_same_day_splits[
                (round(float(sp.value), 2), colon_paths.get(sp.account_guid, ""))
            ] += 1

    matched = sum(
        min(count, all_same_day_splits.get(key, 0)) for key, count in j_splits.items()
    )
    total = sum(j_splits.values())
    if matched == 0:
        return NOT_POSTED, (
            f"No book transaction on {journal.date} matches any split of this "
            "journal by date + amount + account, and no trn:num match either."
        )
    return PARTIALLY_POSTED, (
        f"{matched} of {total} split(s) have a same-date/amount/account match "
        f"in the book, but not all as a single transaction -- treat as "
        "ambiguous, do not assume posted or not-posted."
    )


def build_posted_check(
    report, accounts: dict, gnucash_path: str, year_key: str,
) -> tuple[list[PostedCheckResult], str]:
    """Classify every journal build_journals() would produce for this run
    as NOT POSTED / ALREADY POSTED / PARTIALLY POSTED -- AMBIGUOUS /
    CANNOT CHECK against the supplied GnuCash book. Returns
    (per-journal results, a one-line summary note) -- the note is meant to
    replace the old gnucash_note placeholder in agent.py, the results are
    meant to feed a new workbook sheet.

    Never raises, never reports a false ALREADY POSTED: a trn:num match is
    definite; its absence only ever falls through to the exact fallback
    match, which itself only ever reports ALREADY POSTED for a full,
    exact, single-transaction match.
    """
    if not gnucash_path:
        return [], f"{_POSTED_LABEL}: not available (no GnuCash book supplied)."
    if not accounts:
        return [], (
            f"{_POSTED_LABEL}: not available (no partner_comp_accounts configured "
            "for this entity)."
        )

    book, err = _load_book_safely(gnucash_path)
    if err:
        return [], f"{_POSTED_LABEL}: not available ({err})."

    journals, err = _journals_safely(report, accounts)
    if err:
        return [], f"{_POSTED_LABEL}: not available ({err})."

    if not journals:
        return [], f"{_POSTED_LABEL}: no journal entries implied by this run -- nothing to check."

    colon_paths = _colon_paths(book)
    fy_txns = parse_gnucash.fy_transactions(book, year_key)

    results: list[PostedCheckResult] = []
    for j in journals:
        num_hit = next((t for t in book.transactions if t.num and t.num == j.txn_id), None)
        if num_hit is not None:
            results.append(PostedCheckResult(
                txn_id=j.txn_id, date=j.date, description=j.description,
                status=ALREADY_POSTED,
                detail=(
                    f"Matched by Transaction ID/Num {j.txn_id!r} on book "
                    f"transaction {num_hit.guid!r} -- definite match."
                ),
            ))
            continue
        status, detail = _fallback_classify(j, fy_txns, colon_paths)
        results.append(PostedCheckResult(
            txn_id=j.txn_id, date=j.date, description=j.description,
            status=status, detail=detail,
        ))

    counts = Counter(r.status for r in results)
    summary = ", ".join(f"{counts[s]} {s.lower()}" for s in
                         (ALREADY_POSTED, PARTIALLY_POSTED, NOT_POSTED) if counts[s])
    note = (
        f"{_POSTED_LABEL}: {len(results)} journal(s) checked against {gnucash_path} "
        f"({summary or 'nothing to report'}). See the 'Posted check' sheet for detail."
    )
    return results, note
