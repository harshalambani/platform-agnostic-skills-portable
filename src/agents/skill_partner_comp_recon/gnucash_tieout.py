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
from dataclasses import dataclass, field
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


def _journals_safely(report, accounts: dict, bank_matches: dict | None = None):
    """Returns (journals, error_or_None). Never raises.

    H35-05 round 2, item 2: `bank_matches` (optional), when supplied, is
    threaded straight into build_journals() so every caller of this helper
    (build_posted_check, build_balance_tieout) sees the SAME journals the
    run will actually write -- a payout that is NO_MATCH/TIE/SPLIT drops
    out of these journals entirely (jv_emitter._monthly_journal() already
    returns None for any bank_match.outcome != MATCHED), and a MATCHED
    payout's cash leg lands on the bank import's own counter-account
    instead of a second, unconditional leg on accounts['bank']."""
    try:
        return build_journals(report, accounts or {}, bank_matches=bank_matches), None
    except JournalValidationError as e:
        return None, f"could not build this run's implied journal: {e}"


# ---------------------------------------------------------------------------
# Section C -- balance tie-out.
# ---------------------------------------------------------------------------

_RECLASS_ACCOUNTS = ("current_account", "capital_contribution")


def build_balance_tieout(
    report, accounts: dict, gnucash_path: str, year_key: str,
    posted_check: "list[PostedCheckResult] | None" = None,
    bank_matches: dict | None = None,
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

    H35-05 round 2/3, item 2b: `bank_matches` (optional), threaded straight
    into `_journals_safely()`, makes every ACCOUNT_KEYS row above compare
    against the SAME journals this run will actually write -- a MATCHED
    payout's cash leg is on the bank import's own counter-account, never a
    second leg on accounts['bank']. That counter-account is very often NOT
    one of ACCOUNT_KEYS, so its rerouted amount would otherwise appear on
    no row at all. To avoid silently dropping it, one EXTRA
    ReconciliationResult is appended per distinct rerouted counter-account
    that is not already one of ACCOUNT_KEYS' configured paths. Round 3
    rewrote the comparison itself: the computed side is this run's
    rerouted leg(s) PLUS the bank import's OWN counter-leg(s) on the exact
    matched credit(s) (looked up by transaction guid, kept on
    PayoutMatch.credit_txn_guid) -- expected to net to nil once posted --
    compared against the account's whole FY book movement, reusing the
    SAME PENDING_JOURNAL_VERDICT mechanism as the main loop above when a
    contributing journal is not yet posted. When the account cannot be
    resolved in the book at all, the row still appears, naming the
    combined amount and the reason it could not be reconciled -- never
    silently left out.

    H35-05 round 3, item 2: the "bank" ACCOUNT_KEYS row itself is now
    INFORMATIONAL (H35-04 item D's mechanism), not a verdict row -- since
    H35-05 this skill never posts a leg to accounts['bank'] itself, so
    "Computed" was always 0 while "book" was the account's WHOLE FY
    movement (every deposit/withdrawal all year), which gave a false
    VARIANCE on any real bank account. It now shows what this run actually
    posted to the bank (0) plus how many/how much of the book's own
    already-posted credits it matched against, for visibility only -- never
    counted toward variance/undecidable totals or the LOUD block.
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

    journals, err = _journals_safely(report, accounts, bank_matches=bank_matches)
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

        if key == "bank" and bank_matches:
            # H35-05 round 3, item 2: since H35-05 this skill never posts
            # a leg to accounts["bank"] itself -- a MATCHED payout's cash
            # leg lands on the bank import's OWN counter-account instead
            # (see jv_emitter._monthly_journal()'s bank_match branch) --
            # so "Computed" is always 0 here, while the book side is the
            # bank account's WHOLE FY movement (every deposit and
            # withdrawal all year, from every source, not just these
            # matched payouts). Comparing those is not a real check and
            # gave a false VARIANCE on any real bank account. Make this
            # row informational (H35-04 item D's mechanism -- never
            # counted toward variance/undecidable totals or the LOUD
            # block), showing what this run actually posted plus how many
            # of the book's own already-posted credits it matched
            # against, for visibility only.
            guid = path_to_guid.get(stripped_path)
            book_figure = (
                parse_gnucash.account_fy_sum(book, guid, year_key)
                if guid is not None else None
            )
            posted_to_bank = sum(
                s.debit - s.credit for j in journals for s in j.splits
                if s.account == stripped_path
            )
            matched_here = [
                m for m in (bank_matches or {}).values() if m.outcome == MATCHED
            ]
            matched_count = len(matched_here)
            matched_total = round(sum(m.credit_amount or 0.0 for m in matched_here), 2)
            results.append(ReconciliationResult(
                category=category,
                sources={
                    "Computed (posted to bank by this run)": posted_to_bank,
                    "GnuCash book (FY movement, informational only)": book_figure,
                },
                agree=None,
                note=(
                    "INFORMATIONAL (H35-05 round 3) -- since H35-05 this skill never "
                    "posts a leg to the bank account itself (a matched payout's cash "
                    "leg lands on the bank import's own counter-account instead), so "
                    "the book's whole-FY bank movement (every deposit and withdrawal, "
                    "not just these matched payouts) is not a comparable figure here. "
                    f"posted to bank by this run: {posted_to_bank:,.2f}; matched "
                    f"bank-import credits: {matched_count} totalling {matched_total:,.2f}."
                ),
                informational=True,
            ))
            continue

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

    # H35-05 round 3, item 1 (rewrite of round 2's item 2b): report the
    # rerouted counter-account(s) of any MATCHED bank_matches entry that is
    # not already one of ACCOUNT_KEYS -- never silently drop the amount
    # from every row.
    #
    # THE DEFECT this replaces: the old computed side was only this run's
    # rerouted leg (a lone debit); the old book side was the account's
    # WHOLE FY movement. Before this run's journal is posted, the book
    # holds only the bank import's own credit on that account; after it is
    # posted, the book nets to 0 (the debit reverses the credit). The old
    # computed debit never agreed with either state, so this row gave a
    # false VARIANCE every single time.
    #
    # THE FIX: "Computed" is now this run's rerouted leg(s) PLUS the bank
    # import's OWN counter-leg(s) on the exact matched credit(s) -- looked
    # up by the matched transaction's guid (kept on PayoutMatch as
    # `credit_txn_guid`, never re-derived by a second date/amount search).
    # Once both are posted these sum to nil (a matched payout's rerouted
    # leg is a DEBIT of the payout amount on this account; the bank
    # import's original leg there was a CREDIT of the same raw amount --
    # see _add_leg_raw()/parse_gnucash.py's shared raw sign convention).
    # "Book" stays the account's whole FY movement, same as before. Then:
    #   (a) every contributing journal for this account already posted,
    #       and the book ties to the expected-nil figure within tolerance
    #       -> AGREE;
    #   (b) some contributing journal(s) not yet posted (per posted_check)
    #       but accounting for them ties the book -> PENDING_JOURNAL_VERDICT
    #       (the SAME mechanism/verdict the main ACCOUNT_KEYS loop above
    #       uses, never a new one);
    #   (c) any residual beyond (a)/(b) is a genuine movement on this
    #       account within the FY that is NOT from these matched
    #       transactions -- reported as a real variance, naming the
    #       amount (reconcile_category()'s own "Variance of ..." wording).
    known_paths = {
        _jv_strip_root(p) for p in accounts.values()
        if isinstance(p, str) and p.strip()
    }
    txns_by_guid = {t.guid: t for t in book.transactions}
    matched_by_account: dict[str, list] = {}
    for m in (bank_matches or {}).values():
        if m.outcome == MATCHED and m.credit_account:
            matched_by_account.setdefault(m.credit_account, []).append(m)
    rerouted_accounts = sorted({
        acct for acct in matched_by_account if acct not in known_paths
    })

    _COMPUTED_LABEL = (
        "Computed (this run's rerouted leg(s) plus the bank import's own "
        "matched counter-leg(s) -- expected nil)"
    )

    for stripped_path in rerouted_accounts:
        category = f"{_TIEOUT_LABEL}: bank-match counter-account {stripped_path}"
        account_matches = matched_by_account[stripped_path]

        computed_raw = sum(
            s.debit - s.credit
            for j in journals
            for s in j.splits
            if s.account == stripped_path
        )
        book_matched_raw = 0.0
        for m in account_matches:
            txn = txns_by_guid.get(m.credit_txn_guid) if m.credit_txn_guid else None
            if txn is None:
                continue
            book_matched_raw += float(sum(
                sp.value for sp in txn.splits
                if colon_paths.get(sp.account_guid) == stripped_path
            ))

        guid = path_to_guid.get(stripped_path)
        if guid is None:
            combined_raw = computed_raw + book_matched_raw
            results.append(ReconciliationResult(
                category=category,
                sources={_COMPUTED_LABEL: combined_raw, "GnuCash book (FY movement)": None},
                agree=None,
                note=(
                    f"{CANNOT_RECONCILE} -- account path {stripped_path!r} (the bank "
                    "import's own counter-account for a matched payout, H35-05) was "
                    "not found in the supplied GnuCash book. The combined, "
                    f"expected-nil figure ({combined_raw:,.2f}) is still shown here "
                    "so it is never silently missing from every row."
                ),
            ))
            continue

        acct_type = book.accounts[guid].type
        book_figure = parse_gnucash.account_fy_sum(book, guid, year_key)

        contributing = [
            j for j in journals if any(s.account == stripped_path for s in j.splits)
        ]
        pending = [
            j for j in contributing
            if posted_status_by_id.get(j.txn_id) == NOT_POSTED
        ]

        if pending:
            pending_raw = sum(
                s.debit - s.credit for j in pending for s in j.splits
                if s.account == stripped_path
            )
            posted_computed_raw = computed_raw - pending_raw + book_matched_raw
            posted_computed_figure = parse_gnucash.normalize_value(posted_computed_raw, acct_type)
            expected_figure = parse_gnucash.normalize_value(computed_raw + book_matched_raw, acct_type)
            pending_figure = parse_gnucash.normalize_value(pending_raw, acct_type)
            ids = ", ".join(sorted({j.txn_id for j in pending}))
            sources = {_COMPUTED_LABEL: expected_figure, "GnuCash book (FY movement)": book_figure}
            if abs(book_figure - posted_computed_figure) <= RECONCILIATION_TOLERANCE:
                note = (
                    f"{PENDING_JOURNAL_VERDICT}: journal(s) {ids} for this account "
                    f"({pending_figure:,.2f}) are not yet posted in the book. Book "
                    f"FY movement ({book_figure:,.2f}) plus the pending journal(s) "
                    f"ties to the expected-nil figure ({expected_figure:,.2f}) "
                    "within tolerance -- reconciled, nothing further to post beyond "
                    "the journal(s) already named."
                )
                results.append(ReconciliationResult(
                    category=category, sources=sources, agree=True, note=note,
                ))
            else:
                residual = book_figure - posted_computed_figure
                note = (
                    f"Genuine residual after posting {ids}: {residual:,.2f} -- book "
                    f"FY movement {book_figure:,.2f}, expected-nil computed figure "
                    f"{expected_figure:,.2f}; journal(s) {ids} ({pending_figure:,.2f}) "
                    "not yet posted account for part of the gap but not all of it "
                    "-- this residual is the genuine gap, most likely a movement on "
                    "this account within the FY that is not from these matched "
                    "transactions."
                )
                results.append(ReconciliationResult(
                    category=category, sources=sources, agree=False, note=note,
                ))
            continue

        expected_figure = parse_gnucash.normalize_value(computed_raw + book_matched_raw, acct_type)
        result = reconcile_category(category, {
            _COMPUTED_LABEL: expected_figure,
            "GnuCash book (FY movement)": book_figure,
        })
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
    bank_matches: dict | None = None,
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

    H35-05 round 2, item 2a: `bank_matches` (optional), threaded straight
    into `_journals_safely()`, makes this check see exactly the journals
    this run will actually write -- a NO_MATCH/TIE/SPLIT payout's journal
    is never built at all (so it can never wrongly appear here as
    NOT_POSTED / "pending journal posting"), and a MATCHED payout's cash
    leg is the bank import's own counter-account rather than a second leg
    on accounts['bank'].
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

    journals, err = _journals_safely(report, accounts, bank_matches=bank_matches)
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


# ---------------------------------------------------------------------------
# Section D (H35-05) -- payout-to-bank-credit matching.
#
# THE DEFECT this section fixes: _monthly_journal() used to book
# "Dr bank = total_paid" on EVERY monthly payout, unconditionally -- but the
# bank import (a separate, upstream process) already books that same cash
# credit into the same bank account, under its own counter-account. Posting
# both means the money lands twice in the book. The user's ruling: double
# booking is ALWAYS a defect, never a design question.
#
# THE FIX: the bank import always runs first; this skill runs last and
# MATCHES against what is already posted, instead of booking its own second
# bank-account leg. For each payout, find the bank credit (a deposit split
# on accounts["bank"]) already in the book:
#   - amount within RECONCILIATION_TOLERANCE (Re 1);
#   - date within +/- window_days of the payout's month-end date (default
#     DEFAULT_BANK_MATCH_WINDOW_DAYS, a skill setting -- see agent.py's
#     `bank_match_window` run() parameter / skill.yaml input, mirroring
#     skill_gnucash_intercompany's `date_tolerance` pattern);
#   - one-to-one (a credit satisfies at most one payout, a payout at most
#     one credit) -- enforced by consuming a credit the moment it is
#     assigned, payouts processed in report.monthly's own (chronological)
#     order;
#   - the nearer date wins;
#   - a tie (two or more candidates equally close) is NEVER auto-picked --
#     that payout is flagged TIE, naming every tied candidate, and gets NO
#     journal, exactly like a genuine no-match.
#
# Matched: the cash leg posts to the bank import's OWN counter-account for
# that credit (never a second leg on the bank account itself -- see
# jv_emitter._monthly_journal()'s bank_match branch). No match: no journal
# for that payout, a LOUD "genuine gap" flag. Tie: no journal, a LOUD "TIE"
# flag naming the candidates. No clearing account is used anywhere (rejected
# by the user) -- the matched leg always lands on the REAL counter-account
# the bank import already chose.
#
# Read-only, exactly like Sections B/C above: the only GnuCash access is
# parse_gnucash.parse_book() (via _load_book_safely()); this module never
# opens a write handle on gnucash_path.
# ---------------------------------------------------------------------------

from datetime import datetime as _datetime  # noqa: E402
from datetime import timedelta as _timedelta  # noqa: E402

from .jv_emitter import _month_end as _jv_month_end  # noqa: E402

DEFAULT_BANK_MATCH_WINDOW_DAYS = 7

MATCHED = "matched"
NO_MATCH = "no_match"
TIE = "tie"
SPLIT = "split"


@dataclass
class BankMatchCandidate:
    """One bank-side deposit transaction that is amount/date-eligible for a
    given payout: its date, its amount, and the COUNTER-account (colon
    path) the bank import already posted the other side of that deposit
    to -- never accounts["bank"] itself.

    H35-05 round 2, item 3: a deposit with TWO OR MORE non-bank splits has
    no single counter-account to route a journal leg to. Such a candidate
    is still built (never silently skipped -- a matching credit that
    happens to be split is a real deposit, not a "genuine gap"), but with
    `account=None` and `split_accounts` naming every counter-account the
    bank import spread it across. `account is None` is the signal used
    downstream (match_payouts_to_bank()'s winner-selection) to route to
    the SPLIT outcome instead of MATCHED -- no journal is ever built from
    a split candidate.

    H35-05 round 3, item 1: `txn_guid` (the book transaction's own guid)
    is kept so a MATCHED candidate's ORIGINAL counter-leg(s) can be looked
    up exactly, by guid, from the book -- see PayoutMatch.credit_txn_guid
    and build_balance_tieout()'s rewritten "bank-match counter-account"
    rows. Never used for matching itself (date/amount/account only)."""
    date: str       # ISO YYYY-MM-DD
    amount: float
    account: str | None       # colon path, the bank import's counter-account; None if split
    split_accounts: "list[str] | None" = None  # >=2 counter-accounts, when account is None
    txn_guid: str = ""  # the book transaction's own guid (H35-05 round 3)


@dataclass
class PayoutMatch:
    """The H35-05 matching outcome for one monthly payout (report.monthly
    index idx, 1-based -- the SAME index jv_emitter.build_journals()'s own
    enumerate(report.monthly, start=1) uses, so a dict keyed by idx threads
    straight into build_journals(bank_matches=...) unchanged)."""
    idx: int
    month: str
    payout_date: str       # ISO YYYY-MM-DD (the journal's own _month_end date)
    payout_amount: float
    outcome: str            # MATCHED / NO_MATCH / TIE / SPLIT
    credit_date: str | None = None
    credit_amount: float | None = None
    credit_account: str | None = None
    credit_txn_guid: str | None = None  # H35-05 round 3: the matched book
    # transaction's own guid -- MATCHED only, None otherwise. Lets
    # build_balance_tieout() look up the bank import's ORIGINAL counter-leg
    # on this exact transaction, by guid, instead of the account's whole
    # FY movement (see item 2b's rewrite).
    candidates: list = field(default_factory=list)  # list[BankMatchCandidate], TIE/SPLIT only


def _bank_deposits(book: "parse_gnucash.Book", bank_guid: str, colon_paths: dict) -> list[BankMatchCandidate]:
    """Every deposit (money IN) posted to the bank account, paired with the
    OTHER account(s) the bank import posted the counter-leg(s) to.

    H35-05 round 2, item 3: a deposit split across TWO OR MORE counter-
    accounts is kept as a candidate (never skipped -- see
    BankMatchCandidate's docstring), with `account=None` and
    `split_accounts` naming every counter-account it touched. Only a
    deposit with NO non-bank split at all (a book anomaly, not a real
    transfer) is skipped."""
    deposits: list[BankMatchCandidate] = []
    for txn in book.transactions:
        bank_splits = [s for s in txn.splits if s.account_guid == bank_guid]
        if not bank_splits:
            continue
        other_splits = [s for s in txn.splits if s.account_guid != bank_guid]
        if not other_splits:
            continue
        # ASSET/BANK accounts are debit-normal and not in FLIP_TYPES (see
        # parse_gnucash.py's own docstring) -- a positive raw split value on
        # the bank account IS a deposit (money in); a negative one is a
        # withdrawal, never a candidate here.
        amount = round(float(sum(s.value for s in bank_splits)), 2)
        if amount <= 0:
            continue
        if len(other_splits) == 1:
            deposits.append(BankMatchCandidate(
                date=txn.date_posted.isoformat(),
                amount=amount,
                account=colon_paths.get(other_splits[0].account_guid, ""),
                txn_guid=txn.guid,
            ))
        else:
            split_accounts = sorted({
                colon_paths.get(s.account_guid, "") for s in other_splits
            })
            deposits.append(BankMatchCandidate(
                date=txn.date_posted.isoformat(),
                amount=amount,
                account=None,
                split_accounts=split_accounts,
                txn_guid=txn.guid,
            ))
    return deposits


def _candidate_label(c: "BankMatchCandidate") -> str:
    """Human-readable label for one BankMatchCandidate, used in both the
    LOUD notes (TIE/SPLIT) and the workbook's Bank match sheet -- shared so
    the two never drift apart."""
    if c.account is None:
        accts = ", ".join(c.split_accounts or [])
        return f"{c.date} {c.amount:,.2f} -> split across {accts}"
    return f"{c.date} {c.amount:,.2f} -> {c.account}"


def match_payouts_to_bank(
    report, accounts: dict, gnucash_path: str,
    window_days: int = DEFAULT_BANK_MATCH_WINDOW_DAYS,
) -> tuple[dict[int, PayoutMatch], list[str], str | None]:
    """H35-05: match each of report.monthly's payouts (1-indexed, matching
    jv_emitter.build_journals()'s own enumerate) against a bank deposit
    already posted in the book at accounts["bank"]. Read-only end to end.

    Returns (matches, notes, unavailable_reason):
      - matches: dict[idx -> PayoutMatch], one entry per payout in
        report.monthly (always fully populated -- every payout gets an
        outcome, even NO_MATCH/TIE/SPLIT) when matching actually ran, even
        if every one of them came back NO_MATCH/TIE/SPLIT -- an empty
        result set is a legitimate outcome of a run that DID happen. It is
        {} ONLY when matching could not run at all (see
        `unavailable_reason` below).
      - notes: human-readable strings for the LOUD block -- one per
        NO_MATCH ("genuine gap"), TIE (naming every candidate) or SPLIT
        (naming the accounts it was split across), plus a single
        explanatory note when matching could not run at all. Never one for
        a MATCHED payout (matching cleanly is not loud).
      - unavailable_reason (H35-05 round 2, item 1): None when matching
        genuinely ran (whatever its per-payout outcomes were -- this is
        the ONLY way a caller may tell "ran with zero/all-gap results"
        apart from "could not run at all"; `matches` being falsy is NOT
        sufficient on its own). A human-readable reason string when
        matching could NOT run: no gnucash_path supplied, no
        accounts['bank'] configured, the book failed to load/parse, or the
        accounts['bank'] path was not found in the book. A caller that is
        about to WRITE a journal CSV with a bank leg MUST treat a non-None
        `unavailable_reason` as a hard stop (return an ERROR before any
        write) -- the bank import always runs first, so a journal with an
        unconditional bank leg written under any of these conditions would
        double-book the exact same cash movement the bank import already
        posted. Reading/reporting-only callers (build_posted_check,
        build_balance_tieout) may keep degrading gracefully as before.

    Never raises: a book-load failure or an unresolvable accounts["bank"]
    path degrades to ({}, [note], reason) exactly like Sections B/C above.

    H35-05 round 3, item 3: when accounts['bank'] has NO deposits at all
    within this FY (report.financial_year's 1 Apr-31 Mar window, extended
    by `window_days` on each side -- the same tolerance the per-payout
    matching below already applies), every payout's own "no bank credit
    found -- genuine gap" note is suppressed (it would misleadingly imply
    the import ran and this one payout is specifically missing) and ONE
    loud line is emitted instead, naming the bank path and the FY window,
    saying the bank import for this FY does not appear to have run. Each
    payout still gets a `matches[idx]` entry with outcome NO_MATCH -- the
    Bank match sheet still lists every payout, only the notes differ. When
    the bank DOES have deposits in the FY but a specific payout still has
    no match, that payout's own per-payout gap note is unchanged.
    """
    if not gnucash_path:
        reason = (
            "no GnuCash book was supplied -- the bank import's own postings "
            "cannot be read, so payouts cannot be matched against bank "
            "credits already booked."
        )
        return {}, [], reason
    if not accounts.get("bank"):
        reason = (
            "no accounts['bank'] is configured for this entity -- the GnuCash "
            "book (with accounts.bank set) is required so payouts can be "
            "matched against bank credits already booked."
        )
        return {}, [], reason

    book, err = _load_book_safely(gnucash_path)
    if err:
        reason = (
            f"{err} -- the GnuCash book (with accounts.bank) is required so "
            "payouts can be matched against bank credits already booked."
        )
        return {}, [f"Bank match (H35-05): {err} -- bank leg(s) not matched, unchanged."], reason

    colon_paths = _colon_paths(book)
    bank_path = _jv_strip_root(accounts["bank"])
    bank_guids = [g for g, p in colon_paths.items() if p == bank_path]
    if not bank_guids:
        reason = (
            f"accounts['bank'] path {bank_path!r} was not found in the supplied "
            "GnuCash book -- payouts cannot be matched against bank credits "
            "already booked."
        )
        return {}, [
            f"Bank match (H35-05): accounts['bank'] path {bank_path!r} was not "
            "found in the book -- bank leg(s) not matched, unchanged."
        ], reason

    all_deposits: list[BankMatchCandidate] = []
    for guid in bank_guids:
        all_deposits.extend(_bank_deposits(book, guid, colon_paths))
    used = [False] * len(all_deposits)

    # H35-05 round 3, item 3: when the bank import for this FY has not run
    # at all, EVERY payout would otherwise show its own "no bank credit
    # found -- genuine gap" line, which is misleading -- there is no gap
    # to speak of, the whole import is simply missing. Detect that case up
    # front: no deposit at all on accounts.bank falls within the FY (1 Apr
    # to 31 Mar), extended by `window_days` on each side to match the same
    # tolerance the per-payout matching below already applies (so a
    # deposit just outside the FY boundary but still within the matching
    # window is not wrongly treated as "the import never ran"). When true,
    # the per-payout loop below is told (via `fy_has_deposits=False`) to
    # suppress its own "genuine gap" note for every payout -- one single
    # loud line is emitted instead, after the loop.
    fy_start, fy_end = parse_gnucash.fy_window(report.financial_year)
    window_start = fy_start - _timedelta(days=window_days)
    window_end = fy_end + _timedelta(days=window_days)
    fy_has_deposits = any(
        window_start <= _datetime.strptime(c.date, "%Y-%m-%d").date() <= window_end
        for c in all_deposits
    )

    matches: dict[int, PayoutMatch] = {}
    notes: list[str] = []

    for idx, line in enumerate(report.monthly, start=1):
        payout_date_iso = _jv_month_end(line.month)
        payout_date = _datetime.strptime(payout_date_iso, "%Y-%m-%d").date()
        payout_amount = round(float(line.total_paid), 2)

        scored = []
        for i, c in enumerate(all_deposits):
            if used[i]:
                continue
            if abs(c.amount - payout_amount) > RECONCILIATION_TOLERANCE + 1e-9:
                continue
            c_date = _datetime.strptime(c.date, "%Y-%m-%d").date()
            delta = abs((c_date - payout_date).days)
            if delta > window_days:
                continue
            scored.append((delta, i, c))

        pm = PayoutMatch(
            idx=idx, month=line.month, payout_date=payout_date_iso,
            payout_amount=payout_amount, outcome=NO_MATCH,
        )

        if not scored:
            matches[idx] = pm
            # H35-05 round 3, item 3: only report this per-payout as a
            # "genuine gap" when the bank DOES have deposits in this FY at
            # all -- when it has none whatsoever, the single loud line
            # appended below covers it, and this per-payout line would be
            # misleading noise ("genuine gap" implies the import ran and
            # this one payout specifically is missing, which is not known
            # to be true here).
            if fy_has_deposits:
                notes.append(
                    f"payout {line.month} on {payout_date_iso}: no bank credit found "
                    "-- genuine gap."
                )
            continue

        scored.sort(key=lambda t: t[0])
        best_delta = scored[0][0]
        tied = [t for t in scored if t[0] == best_delta]

        if len(tied) > 1:
            pm.outcome = TIE
            pm.candidates = [
                BankMatchCandidate(
                    date=c.date, amount=c.amount, account=c.account,
                    split_accounts=c.split_accounts,
                )
                for _, _, c in tied
            ]
            matches[idx] = pm
            cand_text = "; ".join(_candidate_label(c) for c in pm.candidates)
            notes.append(
                f"payout {line.month} on {payout_date_iso}: TIE between "
                f"{len(tied)} equally-close bank credits, none auto-picked "
                f"-- candidates: {cand_text}."
            )
            continue

        _, win_i, win_c = tied[0]
        used[win_i] = True

        if win_c.account is None:
            # H35-05 round 2, item 3: the winning credit is a deposit split
            # across >=2 counter-accounts -- consumed one-to-one like any
            # other candidate (`used[win_i] = True` above), but there is no
            # single account to route a journal leg to, so this is a
            # distinct LOUD flag, never silently reported as a "genuine
            # gap" (NO_MATCH would be untrue -- a matching credit WAS
            # found) and never auto-routed to any one of the split
            # accounts.
            pm.outcome = SPLIT
            pm.credit_date = win_c.date
            pm.credit_amount = win_c.amount
            pm.candidates = [win_c]
            matches[idx] = pm
            notes.append(
                f"payout {line.month} on {payout_date_iso}: bank credit found "
                f"but split across {len(win_c.split_accounts or [])} accounts "
                f"({', '.join(win_c.split_accounts or [])}) -- cannot route the "
                "journal, no journal written."
            )
            continue

        pm.outcome = MATCHED
        pm.credit_date = win_c.date
        pm.credit_amount = win_c.amount
        pm.credit_account = win_c.account
        pm.credit_txn_guid = win_c.txn_guid
        matches[idx] = pm

    if not fy_has_deposits:
        notes.insert(0, (
            f"no deposits at all on {bank_path} between {fy_start.isoformat()} and "
            f"{fy_end.isoformat()} -- the bank import for this FY does not appear "
            "to have run; import the bank statement first, then re-run. No payout "
            "journals written."
        ))

    return matches, notes, None
