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

from .engine import (
    CANNOT_RECONCILE,
    PENDING_JOURNAL_VERDICT,
    RECONCILIATION_TOLERANCE,
    ReconciliationResult,
    fy_prefix,
    journal_txn_id,
    reconcile_category,
)
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

_RECLASS_ACCOUNTS = ("current_account", "capital_contribution")


def build_balance_tieout(
    report, accounts: dict, gnucash_path: str, year_key: str,
    posted_check: "list[PostedCheckResult] | None" = None,
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

    H35-04 round 2, item 1 (`posted_check`, optional -- the SAME per-journal
    results build_posted_check() already produces): "compare against the
    book PLUS this skill's own journals" applies to these 8 rows too, not
    only the L5 tie-out row. For each account, this run's own journal
    splits that touch it are split into "already posted" (per
    posted_check) and "not yet posted". When any are not yet posted, the
    book is compared against the POSTED-ONLY portion of this run's
    computed figure -- if that ties within tolerance, the row is
    PENDING_JOURNAL_VERDICT (agree=True, reconciled, naming the pending
    journal id(s) and the amount per account); if a gap remains even after
    accounting for the pending journal(s), THAT gap is the genuine variance
    reported (agree=False), never the raw pre-journal gap. When every
    journal touching an account is already posted (or posted_check was not
    supplied at all -- the pre-round-2 behaviour), this falls straight back
    to today's plain comparison, unchanged.

    Item 2: on `current_account` and `capital_contribution` specifically, a
    POSTED prior-period reclassification journal -- this skill's own
    opening-reclass journal for THIS financial year (jv_emitter.py's
    `_opening_reclass_journal`), Num/Transaction ID exactly
    `journal_txn_id(fy_prefix(year_key), report.firm_name, "RECT")` -- is
    recognised the SAME way build_posted_check()'s Section B recognises any
    of this skill's own journals: an EXACT match on the book transaction's
    `num` field, never a fuzzy/prefix match. It is looked up only when this
    run's OWN `journals` list does not already contain a journal with that
    exact txn_id (report.opening_reclass supplied this run would mean
    build_journals() already produced it, and double-counting it from the
    book on top would be wrong). When found, its split amount for the
    account is folded into the "Computed" side as an explicitly-named
    addition -- never into "variance" -- because it is a real, already-
    posted movement this run's own (monthly-only) journal never claims to
    represent.
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

    # H35-04 round 2, items 1-2 pre-loop setup.
    firm_name = getattr(report, "firm_name", "") or ""
    fy_pfx = fy_prefix(year_key)
    own_txn_ids = {j.txn_id for j in journals}
    rect_id = journal_txn_id(fy_pfx, firm_name, "RECT")
    fy_txns = parse_gnucash.fy_transactions(book, year_key)
    # Only look for a POSTED prior-period reclass in the book when this
    # run's own build_journals() did NOT already produce one under the same
    # deterministic id -- otherwise the book hit and this run's own journal
    # would be the same posting counted twice.
    rect_txn = (
        next((t for t in fy_txns if t.num == rect_id), None)
        if rect_id not in own_txn_ids else None
    )
    posted_status_by_id = {p.txn_id: p.status for p in (posted_check or [])}

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

        contributing = [
            j for j in journals if any(s.account == stripped_path for s in j.splits)
        ]
        pending = [
            j for j in contributing
            if posted_status_by_id.get(j.txn_id) == NOT_POSTED
        ]

        computed_raw = sum(
            s.debit - s.credit
            for j in journals
            for s in j.splits
            if s.account == stripped_path
        )
        acct_type = book.accounts[guid].type
        computed_figure = parse_gnucash.normalize_value(computed_raw, acct_type)
        book_figure = parse_gnucash.account_fy_sum(book, guid, year_key)

        # Item 2: fold a POSTED prior-period reclassification journal (this
        # skill's own opening-reclass journal for THIS year, recognised by
        # exact Num match -- see the docstring) into the computed side as
        # its own named addition. It is a real, already-posted movement
        # this run's own (monthly-only) `journals` never claims to
        # represent, so it belongs on the "what we expect the book to
        # show" side, never folded into a variance.
        reclass_raw = 0.0
        if key in _RECLASS_ACCOUNTS and rect_txn is not None:
            reclass_raw = sum(
                float(sp.value) for sp in rect_txn.splits
                if colon_paths.get(sp.account_guid) == stripped_path
            )
        reclass_figure = parse_gnucash.normalize_value(reclass_raw, acct_type) if reclass_raw else 0.0
        reclass_bit = (
            f"; plus posted reclassification journal {rect_id} ({reclass_figure:,.2f})"
            if reclass_raw else ""
        )

        if pending:
            # Item 1: compare the book against the PORTION of this run's
            # computed figure that is already posted (this run's total
            # minus the not-yet-posted journal(s), plus any posted
            # reclassification line) -- naming the pending journal(s)
            # rather than letting them show up as a false gap.
            pending_raw = sum(
                s.debit - s.credit for j in pending for s in j.splits
                if s.account == stripped_path
            )
            pending_figure = parse_gnucash.normalize_value(pending_raw, acct_type)
            posted_computed_raw = computed_raw - pending_raw + reclass_raw
            posted_computed_figure = parse_gnucash.normalize_value(posted_computed_raw, acct_type)
            ids = ", ".join(sorted({j.txn_id for j in pending}))
            sources = {
                "Computed (this run's journal)": computed_figure,
                "GnuCash book (FY movement)": book_figure,
            }
            if abs(book_figure - posted_computed_figure) <= RECONCILIATION_TOLERANCE:
                note = (
                    f"{PENDING_JOURNAL_VERDICT}: journal(s) {ids} for this account "
                    f"({pending_figure:,.2f}) are not yet posted in the book{reclass_bit}. "
                    f"Book FY movement ({book_figure:,.2f}) plus the pending journal(s) "
                    f"ties to the computed figure ({computed_figure:,.2f}) within "
                    "tolerance -- reconciled, nothing further to post beyond the "
                    "journal(s) already named."
                )
                result = ReconciliationResult(
                    category=category, sources=sources, agree=True, note=note,
                )
            else:
                residual = book_figure - posted_computed_figure
                note = (
                    f"Genuine residual after posting {ids}: {residual:,.2f} -- book FY "
                    f"movement {book_figure:,.2f}, computed {computed_figure:,.2f}"
                    f"{reclass_bit}; journal(s) {ids} ({pending_figure:,.2f}) not yet "
                    "posted account for part of the gap but not all of it -- this "
                    "residual is the genuine gap."
                )
                result = ReconciliationResult(
                    category=category, sources=sources, agree=False, note=note,
                )
            results.append(result)
            continue

        # No pending journal touches this account (or posted_check was not
        # supplied at all): today's plain comparison, unchanged -- except
        # that a posted reclassification line (item 2) is still folded into
        # the computed side even here, since it can apply whether or not a
        # pending journal is also in play.
        computed_figure_with_reclass = parse_gnucash.normalize_value(
            computed_raw + reclass_raw, acct_type,
        )
        result = reconcile_category(category, {
            "Computed (this run's journal)": computed_figure_with_reclass,
            "GnuCash book (FY movement)": book_figure,
        })
        if reclass_raw:
            # Always name the reclassification line explicitly -- even on
            # a silent AGREE, where reconcile_category()'s own note would
            # otherwise be empty -- so "Computed" is never a figure that
            # silently includes a posted prior-period movement with no
            # trace of it in the note.
            base_note = result.note or "Sources agree."
            result = ReconciliationResult(
                category=category, sources=result.sources, agree=result.agree,
                note=f"{base_note}{reclass_bit}",
            )
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
            and computed_figure_with_reclass != 0.0
        ):
            result = ReconciliationResult(
                category=category, sources=result.sources, agree=None,
                note=(
                    f"Book FY movement is 0.00 for this {acct_type} account while the "
                    f"computed figure is {computed_figure_with_reclass:,.2f}. This can happen "
                    "for two different reasons that look identical on this account alone: "
                    "(1) a closed-book income/expense sweep to Equity at year-end (GnuCash's "
                    "standard close-the-books behaviour), in which case the movement is "
                    "genuinely posted and sitting on the Equity account instead; or (2) "
                    "the transaction was never posted at all. This tool cannot tell the "
                    "two apart from this account's balance alone -- check the book's "
                    "Equity account movement for the missing figure before treating this "
                    "as either a false positive or a real variance."
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
