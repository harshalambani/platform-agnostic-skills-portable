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
    WITHIN_TOLERANCE_LIMIT,
    within_tolerance_band,
    ReconciliationResult,
    fy_prefix,
    journal_txn_id,
    reconcile_category,
)
from .jv_emitter import (
    ACCOUNT_KEYS,
    ROUNDING_KEY,
    JournalValidationError,
    build_accrual_journal,
    build_journals,
    excluded_month_legs,
)
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


def _book_matched_raw_for_account(
    stripped_path: str,
    matched_by_account: dict[str, list],
    txns_by_guid: dict,
    colon_paths: dict,
) -> float:
    """H35-05 round 4, defect 2: the bank import's OWN original counter-leg
    raw value(s), on `stripped_path`, for every MATCHED bank_matches entry
    whose credit_account is this same path -- looked up by the matched
    transaction's own guid (PayoutMatch.credit_txn_guid), never re-derived
    by a second date/amount search. Returns 0.0 when this account was not
    the matched counter-account of anything.

    Shared by BOTH build_balance_tieout()'s main ACCOUNT_KEYS loop (when a
    configured account happens to ALSO be a matched bank-import
    counter-account -- e.g. a payout credited straight to
    share_of_profit_income) and its "bank-match counter-account" extra-row
    loop (for a matched counter-account that is NOT one of ACCOUNT_KEYS) --
    the two must never compute this independently (do not duplicate it)."""
    total = 0.0
    for m in matched_by_account.get(stripped_path, []):
        guids = list(m.credit_txn_guids) or ([m.credit_txn_guid] if m.credit_txn_guid else [])
        for g in guids:
            txn = txns_by_guid.get(g)
            if txn is None:
                continue
            total += float(sum(
                sp.value for sp in txn.splits
                if colon_paths.get(sp.account_guid) == stripped_path
            ))
    return total


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


def _accrual_journal_safely(report, accounts: dict, book_gap=None):
    """H35-21: the year-end ACCR journal this run would write, or None. Never
    raises. build_journals() returns only the monthly (and opening-reclass)
    journals, so every check that must cover "every journal this run writes"
    adds this one explicitly."""
    try:
        journal, _note, _res = build_accrual_journal(report, accounts or {}, book_gap=book_gap)
    except JournalValidationError:
        return None
    return journal


def accrual_txn_id(report) -> str:
    return journal_txn_id(
        fy_prefix(report.financial_year), getattr(report, "firm_name", "") or "", "ACCR")


# ---------------------------------------------------------------------------
# Section C -- balance tie-out.
# ---------------------------------------------------------------------------

_RECLASS_ACCOUNTS = ("current_account", "capital_contribution")


def build_balance_tieout(
    report, accounts: dict, gnucash_path: str, year_key: str,
    posted_check: "list[PostedCheckResult] | None" = None,
    bank_matches: dict | None = None,
    settings_error: str | None = None,
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
        if settings_error:
            return _blank(settings_error)
        return _blank("no partner_comp_accounts configured for this entity")

    book, err = _load_book_safely(gnucash_path)
    if err:
        return _blank(err)

    journals, err = _journals_safely(report, accounts, bank_matches=bank_matches)
    if err:
        return _blank(err)

    # H35-21: a year-end accrual that is already in the book is part of what
    # the book holds, so it belongs on the computed side too -- otherwise the
    # whole accrual shows as a false VARIANCE on current_account and
    # share_of_profit_income. Only a definite ALREADY POSTED counts; an
    # unposted or partial one stays out, exactly as before.
    _accr = _accrual_journal_safely(report, accounts)
    if _accr is not None and all(j.txn_id != _accr.txn_id for j in journals):
        _accr_status = {p.txn_id: p.status for p in (posted_check or [])}.get(_accr.txn_id)
        if _accr_status is None and posted_check is None:
            _accr_status = (ALREADY_POSTED if any(
                t.num and t.num == _accr.txn_id for t in book.transactions) else None)
        if _accr_status == ALREADY_POSTED:
            journals = list(journals) + [_accr]

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

    # H35-05 round 3, item 1 / round 4, defect 2: computed ONCE, up front,
    # and shared by BOTH the main ACCOUNT_KEYS loop below (a configured
    # account that also happens to be a matched bank-import counter-account
    # -- round 4, defect 2) and the "bank-match counter-account" extra-row
    # loop further down (a matched counter-account that is NOT one of
    # ACCOUNT_KEYS) -- never computed twice.
    known_paths = {
        _jv_strip_root(p) for p in accounts.values()
        if isinstance(p, str) and p.strip()
    }
    txns_by_guid = {t.guid: t for t in book.transactions}
    matched_by_account: dict[str, list] = {}
    for m in (bank_matches or {}).values():
        if m.outcome == MATCHED and m.credit_account:
            matched_by_account.setdefault(m.credit_account, []).append(m)

    results: list[ReconciliationResult] = []
    row_path: dict[str, str] = {}
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
        row_path[category] = stripped_path

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

        # H35-05 round 4, defect 2: this configured account may ALSO be the
        # bank import's matched counter-account for one or more payouts
        # (e.g. a payout credited straight to share_of_profit_income). When
        # so, the book's FY movement for this account already includes the
        # bank import's own original credit -- so "Computed" must include
        # it too, exactly as the "bank-match counter-account" extra-row
        # loop below already does for a non-configured account, or this row
        # shows a false VARIANCE equal to the matched credit(s) once
        # posted. Shared helper -- never re-derived here.
        matched_raw = _book_matched_raw_for_account(
            stripped_path, matched_by_account, txns_by_guid, colon_paths,
        )
        computed_raw += matched_raw

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
        matched_figure = parse_gnucash.normalize_value(matched_raw, acct_type) if matched_raw else 0.0
        extra_bits = []
        if reclass_raw:
            extra_bits.append(f"plus posted reclassification journal {rect_id} ({reclass_figure:,.2f})")
        if matched_raw:
            extra_bits.append(
                "plus the bank import's own matched counter-leg(s) on this "
                f"account ({matched_figure:,.2f})"
            )
        reclass_bit = f"; {'; '.join(extra_bits)}" if extra_bits else ""

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
        if reclass_raw or matched_raw:
            # Always name the reclassification line and/or the folded-in
            # bank-match counter-leg(s) explicitly -- even on a silent
            # AGREE, where reconcile_category()'s own note would otherwise
            # be empty -- so "Computed" is never a figure that silently
            # includes a posted prior-period movement or matched bank
            # credit with no trace of it in the note.
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
    rerouted_accounts = sorted({
        acct for acct in matched_by_account if acct not in known_paths
    })

    _COMPUTED_LABEL = (
        "Computed (this run's rerouted leg(s) plus the bank import's own "
        "matched counter-leg(s) -- expected nil)"
    )

    for stripped_path in rerouted_accounts:
        category = f"{_TIEOUT_LABEL}: bank-match counter-account {stripped_path}"
        row_path[category] = stripped_path

        computed_raw = sum(
            s.debit - s.credit
            for j in journals
            for s in j.splits
            if s.account == stripped_path
        )
        book_matched_raw = _book_matched_raw_for_account(
            stripped_path, matched_by_account, txns_by_guid, colon_paths,
        )

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

    return _name_excluded_months(results, row_path, report, accounts, bank_matches)


def _without_rounding(accounts: dict) -> dict:
    return {k: v for k, v in (accounts or {}).items() if k != ROUNDING_KEY}


def _current_account_book_gap(report, accounts: dict, book, year_key: str,
                              journals, status: dict):
    """H35-22: the Dr amount that would make the current account in the book
    close exactly at the statement figure, counting what the book already
    holds, this run's unposted monthly journals and the (rounding-free)
    year-end accrual. None when it cannot be worked out. A rounding already
    booked (by hand or by a posted ACCR) is inside the book figure, so it is
    never asked for a second time."""
    llp = getattr(report, "llp_record", None) or {}
    stmt = llp.get("current_closing_balance")
    path = (accounts or {}).get("current_account")
    if stmt is None or not isinstance(path, str) or not path.strip():
        return None
    stripped = _jv_strip_root(path)
    colon = _colon_paths(book)
    guid = next((g for g, pth in colon.items() if pth == stripped), None)
    if guid is None:
        return None
    end = parse_gnucash.fy_window(year_key)[1]
    total_raw = sum(float(sp.value) for t in book.transactions if t.date_posted <= end
                    for sp in t.splits if sp.account_guid == guid)
    for j in (journals or []):
        if status.get(j.txn_id) == NOT_POSTED:
            total_raw += sum(sp.debit - sp.credit for sp in j.splits if sp.account == stripped)
    base = _accrual_journal_safely(report, _without_rounding(accounts))
    if base is not None and not any(t.num and t.num == base.txn_id for t in book.transactions):
        base_status, _d = _fallback_classify(
            base, parse_gnucash.fy_transactions(book, year_key), colon)
        if base_status == NOT_POSTED:
            total_raw += sum(sp.debit - sp.credit for sp in base.splits if sp.account == stripped)
    acct_type = book.accounts[guid].type
    sign = parse_gnucash.normalize_value(1.0, acct_type)
    expected = parse_gnucash.normalize_value(total_raw, acct_type)
    return round((stmt - expected) * sign, 2)


def _unposted_accrual_journal(report, accounts: dict, book, year_key: str,
                              journals=None, status: dict | None = None):
    """H35-15: the year-end ACCR accrual journal (jv_emitter.
    build_accrual_journal) when it is NOT yet in the book, else None.

    build_journals() only returns the monthly M-journals; the accrual is
    built separately, so a check that adds "this run's unposted journals"
    to the book must add it explicitly. It is classified exactly like the
    M-journals: a Transaction ID/Num hit in the book means ALREADY POSTED
    (the book balance already holds it -- counting it again would book it
    twice); only a clean NOT POSTED is counted. Anything partial or
    ambiguous is left out, and the gap stays visible.

    H35-22: the journal returned carries the rounding line sized against the
    BOOK (see _current_account_book_gap), the same one the run writes."""
    status = status or {}
    gap = _current_account_book_gap(report, accounts, book, year_key, journals, status)
    journal = _accrual_journal_safely(report, accounts, book_gap=gap)
    if journal is None:
        return None
    if any(t.num and t.num == journal.txn_id for t in book.transactions):
        return None
    status_, _detail = _fallback_classify(
        journal, parse_gnucash.fy_transactions(book, year_key), _colon_paths(book))
    return journal if status_ == NOT_POSTED else None


def accrual_book_gap(report, accounts: dict, gnucash_path: str, year_key: str,
                     posted_check=None, bank_matches=None):
    """Public wrapper for agent.py: the book-sized rounding gap for the
    year-end journal, or None when the book is unavailable or the accrual is
    already posted (a posted journal is final -- never re-sized)."""
    if not gnucash_path or not accounts:
        return None
    book, err = _load_book_safely(gnucash_path)
    if err:
        return None
    status = {p.txn_id: p.status for p in (posted_check or [])}
    if status.get(accrual_txn_id(report)) == ALREADY_POSTED:
        return None
    journals, jerr = _journals_safely(report, accounts, bank_matches=bank_matches)
    if jerr:
        return None
    return _current_account_book_gap(report, accounts, book, year_key, journals, status)


def restate_posted_accrual_rows(rows, posted_check, report) -> None:
    """H35-21: rows built before the book was read (the L5 tie-out and the
    profit-share row) offer the year-end accrual as a "PENDING JOURNAL
    POSTING" closure. When the book already holds that journal it is not
    pending: restate those rows in place so the report never says "Post
    <id>" for a journal that is already posted. A row that tied only because
    of the pending accrual stays a tie (the journal is in the book); a row
    that still disagrees keeps its figures but loses the word pending."""
    acc_id = accrual_txn_id(report)
    if {p.txn_id: p.status for p in (posted_check or [])}.get(acc_id) != ALREADY_POSTED:
        return
    for r in rows:
        note = r.note or ""
        if acc_id not in note:
            continue
        if PENDING_JOURNAL_VERDICT in note and r.agree:
            r.note = (
                f"ALREADY POSTED -- journal {acc_id} (the year-end accrual) is already in "
                "the book and is not pending; with it this row ties to the statement. "
                "Do not post it again."
            )
        elif "not yet posted" in note or "not-yet-posted" in note:
            r.note = (
                f"NOTE: journal {acc_id} is ALREADY POSTED in the book, not pending. "
                + note.replace("not yet posted", "already posted")
                      .replace("not-yet-posted", "already-posted")
            )


_STATEMENT_BOOK_ROWS = (
    ("capital_contribution", "capital_closing_balance", "closing capital"),
    ("current_account", "current_closing_balance", "closing current account"),
)


def build_statement_book_check(
    report, accounts: dict, gnucash_path: str, year_key: str,
    posted_check: "list[PostedCheckResult] | None" = None,
    bank_matches: dict | None = None,
    settings_error: str | None = None,
) -> list[ReconciliationResult]:
    """H35-15: the LLP statement's closing capital and closing current
    account against the GnuCash balance of the entity's partner capital and
    current accounts at 31 March, after this skill's OWN journals.

    Book side = the account's cumulative balance to the year end (credit
    positive, like the statement) PLUS the movement of this run's journals
    that are not yet posted. A gap that this skill's unposted journal fully
    explains gets the existing pending-posting verdict. Any other gap is a
    DIFFER and is never plugged or auto-posted."""
    from .jv_emitter import _strip_root  # noqa: PLC0415
    llp = getattr(report, "llp_record", None)
    out: list[ReconciliationResult] = []
    book = err = None
    journals = None
    if gnucash_path and accounts:
        book, err = _load_book_safely(gnucash_path)
        if book is not None:
            journals, jerr = _journals_safely(report, accounts, bank_matches=bank_matches)
            err = jerr
    status = {p.txn_id: p.status for p in (posted_check or [])}
    end = parse_gnucash.fy_window(year_key)[1]
    for key, stmt_key, label in _STATEMENT_BOOK_ROWS:
        category = f"Statement vs book: {label} at 31 March"
        stmt = llp.get(stmt_key) if llp else None
        path = accounts.get(key) if accounts else None
        src = {"LLP Statement": stmt, "GnuCash book at 31 Mar (plus this skill's unposted journals)": None}
        why = None
        if stmt is None:
            why = "the LLP Statement of Account's " + label + " was not supplied or not parsed"
        elif not gnucash_path:
            why = "no GnuCash book supplied"
        elif not accounts or not isinstance(path, str) or not path.strip():
            why = settings_error or f"no {key} account configured for this entity"
        elif book is None or err:
            why = err or "the GnuCash book could not be read"
        if why:
            out.append(ReconciliationResult(
                category=category, sources=src, agree=None,
                note=f"{CANNOT_RECONCILE} -- {why}."))
            continue
        stripped = _strip_root(path)
        colon = _colon_paths(book)
        guid = next((g for g, p in colon.items() if p == stripped), None)
        if guid is None:
            out.append(ReconciliationResult(
                category=category, sources=src, agree=None,
                note=f"{CANNOT_RECONCILE} -- account path {stripped!r} not found in the supplied GnuCash book."))
            continue
        acct_type = book.accounts[guid].type
        raw = sum(float(sp.value) for t in book.transactions if t.date_posted <= end
                  for sp in t.splits if sp.account_guid == guid)
        book_bal = parse_gnucash.normalize_value(raw, acct_type)
        pend = [j for j in journals if status.get(j.txn_id) == NOT_POSTED
                and any(s.account == stripped for s in j.splits)]
        accr = _unposted_accrual_journal(report, accounts, book, year_key, journals, status)
        if accr is not None and any(s.account == stripped for s in accr.splits):
            pend = pend + [accr]
        pend_raw = sum(s.debit - s.credit for j in pend for s in j.splits if s.account == stripped)
        pend_fig = parse_gnucash.normalize_value(pend_raw, acct_type) if pend_raw else 0.0
        expected = round(book_bal + pend_fig, 2)
        src["GnuCash book at 31 Mar (plus this skill's unposted journals)"] = expected
        gap = round(stmt - expected, 2)
        raw_gap = round(stmt - book_bal, 2)
        # ONE book figure everywhere: the Sources column and every note quote
        # `expected` (book + unposted journals). The book alone and the
        # unposted amount are only ever named as its two parts.
        parts = (f"{expected:,.2f} = book {book_bal:,.2f} + journal(s) not yet posted "
                 f"{pend_fig:,.2f}") if pend else f"{expected:,.2f}"
        if abs(gap) <= RECONCILIATION_TOLERANCE:
            if pend and abs(raw_gap) > RECONCILIATION_TOLERANCE:
                ids = ", ".join(sorted({j.txn_id for j in pend}))
                out.append(ReconciliationResult(
                    category=category, sources=src, agree=True,
                    note=f"{PENDING_JOURNAL_VERDICT}: book figure {parts}. Journal(s) {ids} "
                         f"are not yet posted and explain the whole difference to the "
                         f"statement ({stmt:,.2f})."))
            else:
                out.append(ReconciliationResult(
                    category=category, sources=src, agree=True,
                    note=f"The book figure ({parts}) agrees with the statement ({stmt:,.2f})."))
        elif within_tolerance_band(gap):
            out.append(ReconciliationResult(
                category=category, sources=src, agree=True,
                note=f"The book figure ({parts}) agrees with the statement ({stmt:,.2f}) only "
                     f"within Rs {WITHIN_TOLERANCE_LIMIT:g}: difference Rs {abs(gap):,.2f}. "
                     "Nothing is plugged or posted for this.",
                within_tolerance_diff=round(abs(gap), 2), statement_ref=True))
        else:
            tail = (" The journal(s) not yet posted were taken into account; the gap remains."
                    if pend else "")
            out.append(ReconciliationResult(
                category=category, sources=src, agree=False,
                note=f"DIFFERS -- statement {stmt:,.2f} against book figure {parts} at 31 March: "
                     f"unexplained difference {gap:,.2f}.{tail} Nothing is plugged or posted for this; "
                     "find the cause in the book or the statement."))
    return out


def _name_excluded_months(results, row_path, report, accounts, bank_matches):
    """Tie-out excluded months: a payout month with no unique bank match
    (no_match or tie, treated alike) has no journal, so the row's Computed
    figure leaves it out. Name every such month whose would-be legs touch the
    row's account for a non-zero amount. The amount is only described in the
    note, never added to a figure. A month that would not have touched the
    account (for example a month with no capital transferred) is not named,
    so a plain AGREE stays plain."""
    try:
        skipped = excluded_month_legs(report, accounts, bank_matches)
    except JournalValidationError:
        return results
    if not skipped:
        return results
    out = []
    for r in results:
        path = row_path.get(r.category)
        if path is None or r.informational or r.not_checked:
            out.append(r)
            continue
        hits = []
        for month, splits in skipped.items():
            amt = round(sum(s.debit - s.credit for s in splits if s.account == path), 2)
            if abs(amt) >= 0.005:
                hits.append((month, amt))
        if not hits:
            out.append(r)
            continue
        months = ", ".join(m for m, _ in hits)
        detail = "; ".join(f"{m}: {a:,.2f}" for m, a in hits)
        plural = len(hits) > 1
        extra = (
            f"EXCLUDES {months} -- no unique bank match for "
            f"{'those payouts' if plural else 'that payout'}, so no journal was "
            f"built and the Computed figure leaves {'them' if plural else 'it'} "
            f"out (would-be amount on this account, not included: {detail})."
        )
        r.note = f"{r.note} {extra}".strip()
        r.excluded_months = months
        out.append(r)
    return out


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
    settings_error: str | None = None,
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
        if settings_error:
            return [], f"{_POSTED_LABEL}: not available ({settings_error})"
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

    # H35-21: the year-end accrual is a journal this run writes too, so it is
    # classified exactly like the monthly ones (same definite trn:num match,
    # same exact fallback).
    accr = _accrual_journal_safely(report, accounts)
    if accr is not None and all(j.txn_id != accr.txn_id for j in journals):
        journals = list(journals) + [accr]

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

# H35-12: a payout the firm paid as several separate bank credits is matched
# as a SET of 2..MAX_SPLIT_PARTS credits (see _split_sets() below).
MAX_SPLIT_PARTS = 3

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
    # H35-12: a MATCHED payout paid as 2..MAX_SPLIT_PARTS separate bank credits
    # to the same counter-account. `parts` holds every credit (empty for a
    # single-credit match); credit_amount is then their SUM, credit_date the
    # latest part's date, credit_txn_guids every part's guid.
    parts: list = field(default_factory=list)
    credit_txn_guids: list = field(default_factory=list)
    # H35-12: TIE between several qualifying SETS -- list[list[BankMatchCandidate]].
    candidate_sets: list = field(default_factory=list)

    @property
    def is_split_match(self) -> bool:
        return self.outcome == MATCHED and len(self.parts) > 1

    @property
    def outcome_label(self) -> str:
        if self.is_split_match:
            return f"MATCHED (split, {len(self.parts)} credits)"
        return self.outcome.upper()


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


def _split_sets(pm: "PayoutMatch", deposits: list, used: list, window_days: int) -> list:
    """H35-12: every set of 2..MAX_SPLIT_PARTS still-unused bank credits that
    together pay `pm` -- all posted to the SAME single counter-account, each
    dated in the payout's calendar month or within +/- window_days of the
    payout date, summing to the payout within RECONCILIATION_TOLERANCE.
    Returns [[(deposit index, candidate), ...], ...]. Credits spread over
    several counter-accounts (account is None) never take part."""
    from itertools import combinations  # noqa: PLC0415

    if pm.payout_amount <= 0:
        return []
    pay_date = _datetime.strptime(pm.payout_date, "%Y-%m-%d").date()
    eligible = []
    for i, c in enumerate(deposits):
        if used[i] or c.account is None:
            continue
        c_date = _datetime.strptime(c.date, "%Y-%m-%d").date()
        same_month = (c_date.year, c_date.month) == (pay_date.year, pay_date.month)
        if not (same_month or abs((c_date - pay_date).days) <= window_days):
            continue
        eligible.append((i, c))
    by_account: dict = {}
    for item in eligible:
        by_account.setdefault(item[1].account, []).append(item)
    found = []
    for items in by_account.values():
        for n in range(2, MAX_SPLIT_PARTS + 1):
            for combo in combinations(items, n):
                total = round(sum(c.amount for _i, c in combo), 2)
                if abs(total - pm.payout_amount) <= RECONCILIATION_TOLERANCE + 1e-9:
                    found.append(sorted(combo, key=lambda t: (t[1].date, t[0])))
    return found


def match_payouts_to_bank(
    report, accounts: dict, gnucash_path: str,
    window_days: int = DEFAULT_BANK_MATCH_WINDOW_DAYS,
    settings_error: str | None = None,
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
    if not accounts.get("bank") and settings_error:
        reason = (
            f"{settings_error} The bank account path comes from the entity's "
            "settings, so payouts cannot be matched against bank credits "
            "already booked."
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

    # H35-05 round 3, item 3 (round 4, defect 1 fix): when the bank import
    # for this FY has not run at all, EVERY payout would otherwise show its
    # own "no bank credit found -- genuine gap" line, which is misleading --
    # there is no gap to speak of, the whole import is simply missing.
    # Detect that case up front: no deposit at all on accounts.bank falls
    # within the STRICT FY window (1 Apr to 31 Mar, parse_gnucash.
    # fy_window() -- no +/- window_days widening here).
    #
    # THE DEFECT round 4 fixes: round 3 widened this check by window_days
    # on each side, the SAME tolerance the per-payout matching below
    # already applies. On a real book, one leftover deposit from the PRIOR
    # FY, a few days before 1 Apr, falls inside that widened window -- the
    # check then wrongly reports "has deposits", and every payout prints
    # its own misleading "no bank credit found -- genuine gap" line, the
    # exact double-report round 3 was meant to stop. "Has the import run"
    # and "does this payout have a matching credit" are two different
    # questions and must not share one window: the former is decided on
    # the strict FY only; the latter (per-payout matching, below) keeps its
    # own +/- window_days tolerance, completely unchanged.
    fy_start, fy_end = parse_gnucash.fy_window(report.financial_year)
    fy_has_deposits = any(
        fy_start <= _datetime.strptime(c.date, "%Y-%m-%d").date() <= fy_end
        for c in all_deposits
    )

    matches: dict[int, PayoutMatch] = {}
    notes_by_idx: dict[int, str] = {}
    lines_by_idx: dict[int, object] = {}

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

        lines_by_idx[idx] = line
        if not scored:
            matches[idx] = pm
            # H35-05 round 3, item 3: only report this per-payout as a
            # "genuine gap" when the bank DOES have deposits in this FY at
            # all -- when it has none whatsoever, the single loud line
            # appended below covers it, and this per-payout line would be
            # misleading noise ("genuine gap" implies the import ran and
            # this one payout specifically is missing, which is not known
            # to be true here).
            # (H35-12: the note is written after the split phase below, once
            # we know no set of credits matches this payout either.)
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
            notes_by_idx[idx] = (
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
            notes_by_idx[idx] = (
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

    # H35-12: split phase. Single-credit matching above has run for EVERY
    # payout; only payouts still unmatched are tried here, and only with
    # credits no single match consumed.
    for idx in sorted(matches):
        pm = matches[idx]
        if pm.outcome != NO_MATCH:
            continue
        line = lines_by_idx[idx]
        sets = _split_sets(pm, all_deposits, used, window_days)
        if len(sets) == 1:
            chosen = sets[0]
            for i, _c in chosen:
                used[i] = True
            parts = [c for _i, c in chosen]
            pm.outcome = MATCHED
            pm.parts = parts
            pm.credit_amount = round(sum(c.amount for c in parts), 2)
            pm.credit_date = max(c.date for c in parts)
            pm.credit_account = parts[0].account
            pm.credit_txn_guid = parts[0].txn_guid
            pm.credit_txn_guids = [c.txn_guid for c in parts]
        elif len(sets) > 1:
            pm.outcome = TIE
            pm.candidate_sets = [[c for _i, c in st] for st in sets]
            pm.candidates = [c for st in pm.candidate_sets for c in st]
            set_text = "; ".join(
                f"set {n}: " + " + ".join(_candidate_label(c) for c in st)
                for n, st in enumerate(pm.candidate_sets, start=1)
            )
            notes_by_idx[idx] = (
                f"payout {line.month} on {pm.payout_date}: TIE between "
                f"{len(sets)} different sets of bank credits that each add up to "
                f"the payout, none auto-picked -- {set_text}."
            )
        elif fy_has_deposits:
            notes_by_idx[idx] = (
                f"payout {line.month} on {pm.payout_date}: no bank credit found "
                "-- genuine gap."
            )

    notes: list[str] = [notes_by_idx[i] for i in sorted(notes_by_idx)]

    if not fy_has_deposits:
        notes.insert(0, (
            f"no deposits at all on {bank_path} between {fy_start.isoformat()} and "
            f"{fy_end.isoformat()} -- the bank import for this FY does not appear "
            "to have run; import the bank statement first, then re-run. No payout "
            "journals written."
        ))

    return matches, notes, None
