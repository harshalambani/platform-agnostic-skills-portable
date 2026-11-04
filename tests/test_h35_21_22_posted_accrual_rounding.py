"""H35-21 (the year-end accrual and opening reclass are classified by the
posted check, never offered as pending once posted) and H35-22 (a rounding
line of up to Rs 10 so the current account closes at the statement).
Synthetic fixtures only."""
import pytest

import test_skill_partner_comp_recon as T
from agents.skill_partner_comp_recon.engine import (
    PENDING_JOURNAL_VERDICT, booked_current_account_closing)
from agents.skill_partner_comp_recon.gnucash_tieout import (
    ALREADY_POSTED, NOT_POSTED, PostedCheckResult,
    accrual_book_gap, accrual_txn_id, build_balance_tieout,
    build_posted_check, restate_posted_accrual_rows)
from agents.skill_partner_comp_recon.jv_emitter import (
    JournalValidationError, build_accrual_journal, build_journals)

CLEARING = "Assets:Salary Clearing"
ACCTS = dict(T._SB_ACCOUNTS, salary_clearing=CLEARING)


def _report(stmt_delta=0.0, accrual=True):
    """Report whose statement current closing = booked + accrual + stmt_delta."""
    r = T._sb_report(50000.0, 0.0)
    booked = sum(m.share_of_profit_gross + m.firms_tax_sop + m.additional_share_of_profit
                 for m in r.monthly)
    r.llp_record["current_profit_share"] = booked + (T._ACCR_AMT if accrual else 0.0)
    r.llp_record["current_opening_balance"] = 100000.0
    base = booked_current_account_closing(r.monthly, r.llp_record)
    r.llp_record["current_closing_balance"] = round(
        base + (T._ACCR_AMT if accrual else 0.0) + stmt_delta, 2)
    return r


def _book(tmp_path, txns, name="b"):
    accounts, guids = T._gc_tree_with_equity()
    return T._write_gnucash_book(
        tmp_path / f"{name}.gnucash",
        T._gc_document_xml(accounts, [t(guids) for t in txns]))


def _accr_txn(num, amount=T._ACCR_AMT, other="Share of Profit", seed="a"):
    def make(guids):
        return T._gc_txn_xml(
            T._gc_guid("t" + seed), "2026-03-31", "accrual",
            [T._gc_split_xml(T._gc_guid("x1" + seed), amount, guids["Partner Current Account"]),
             T._gc_split_xml(T._gc_guid("x2" + seed), -amount, guids[other])], num=num)
    return make


def _status(results, txn_id):
    return {p.txn_id: p.status for p in results}[txn_id]


# ---- H35-21: posted check covers the accrual -------------------------------

def test_posted_check_classifies_accrual_posted_by_num(tmp_path):
    r = _report()
    aid = accrual_txn_id(r)
    res, _ = build_posted_check(r, ACCTS, _book(tmp_path, [_accr_txn(aid)]), "2025-26")
    assert _status(res, aid) == ALREADY_POSTED


def test_posted_check_unposted_accrual_is_not_posted(tmp_path):
    r = _report()
    aid = accrual_txn_id(r)
    res, _ = build_posted_check(r, ACCTS, _book(tmp_path, []), "2025-26")
    assert _status(res, aid) == NOT_POSTED


def test_partial_match_is_never_already_posted(tmp_path):
    r = _report()
    aid = accrual_txn_id(r)
    res, _ = build_posted_check(
        r, ACCTS, _book(tmp_path, [_accr_txn("", other="Bank", seed="p")], "p1"), "2025-26")
    assert _status(res, aid) != ALREADY_POSTED
    res, _ = build_posted_check(
        r, ACCTS, _book(tmp_path, [_accr_txn("", amount=6999.0, seed="q")], "p2"), "2025-26")
    assert _status(res, aid) != ALREADY_POSTED


def test_monthly_journal_classification_unchanged(tmp_path):
    r = _report(accrual=False)
    ids = [j.txn_id for j in build_journals(r, ACCTS)]
    res, _ = build_posted_check(r, ACCTS, _book(tmp_path, []), "2025-26")
    assert ids and all(_status(res, i) == NOT_POSTED for i in ids)


# ---- H35-21: tie-out ----------------------------------------------------------

def _tie(tmp_path, r, txns, name, posted=None):
    path = _book(tmp_path, txns, name)
    return {x.category.rsplit(": ", 1)[-1]: x
            for x in build_balance_tieout(r, ACCTS, path, "2025-26", posted_check=posted)}


def test_tieout_posted_accrual_is_not_a_variance(tmp_path):
    r = _report()
    aid = accrual_txn_id(r)
    pc = [PostedCheckResult(txn_id=aid, date="2026-03-31", description="x",
                            status=ALREADY_POSTED, detail="")]
    rows = _tie(tmp_path, r, [_accr_txn(aid)], "t1", pc)
    cur = rows["current_account"]
    assert cur.agree is True and PENDING_JOURNAL_VERDICT not in cur.note
    sop = rows["share_of_profit_income"]
    comp, book = sop.sources["Computed (this run's journal)"], sop.sources["GnuCash book (FY movement)"]
    # the posted accrual is on BOTH sides: the only gap left is the monthly
    # total (not in this synthetic book), never the accrual as well.
    assert comp - book == 300000.0
    assert aid not in sop.note


def test_tieout_unposted_accrual_is_not_counted_as_posted(tmp_path):
    r = _report()
    aid = accrual_txn_id(r)
    pc = [PostedCheckResult(txn_id=aid, date="2026-03-31", description="x",
                            status=NOT_POSTED, detail="")]
    rows = _tie(tmp_path, r, [], "t2", pc)
    assert rows["share_of_profit_income"].sources["Computed (this run's journal)"] == 300000.0


def test_statement_row_unposted_accrual_still_pending(tmp_path):
    r = T._accr_report(-T._ACCR_AMT)
    row = T._sb_rows(tmp_path, r, [], name="s0")[T._CUR]
    assert row.agree is True and PENDING_JOURNAL_VERDICT in row.note and "ACCR" in row.note


# ---- H35-21: statement row + restate -------------------------------------------

def test_statement_row_posted_accrual_not_pending(tmp_path):
    r = T._accr_report(-T._ACCR_AMT)
    aid = accrual_txn_id(r)
    row = T._sb_rows(tmp_path, r, [T._accr_posted_txn(num=aid)], name="s1")[T._CUR]
    assert PENDING_JOURNAL_VERDICT not in row.note


def test_restate_turns_pending_row_into_already_posted():
    from agents.skill_partner_comp_recon.engine import ReconciliationResult
    r = _report()
    aid = accrual_txn_id(r)
    row = ReconciliationResult(
        category="x", sources={}, agree=True,
        note=f"{PENDING_JOURNAL_VERDICT}: post {aid} ... not yet posted")
    pc = [PostedCheckResult(txn_id=aid, date="d", description="x",
                            status=ALREADY_POSTED, detail="")]
    restate_posted_accrual_rows([row], pc, r)
    assert row.note.startswith("ALREADY POSTED") and "Do not post it again" in row.note
    row2 = ReconciliationResult(category="x", sources={}, agree=True,
                                note=f"{PENDING_JOURNAL_VERDICT}: post {aid}")
    restate_posted_accrual_rows([row2], [PostedCheckResult(
        txn_id=aid, date="d", description="x", status=NOT_POSTED, detail="")], r)
    assert row2.note.startswith(PENDING_JOURNAL_VERDICT)


# ---- H35-22: rounding line -----------------------------------------------------

@pytest.mark.parametrize("delta", [3.25, -4.5, 10.0])
def test_rounding_line_added_within_band(delta):
    j, note, _ = build_accrual_journal(_report(delta), ACCTS)
    clr = [s for s in j.splits if s.account.endswith("Salary Clearing")]
    assert len(clr) == 1 and round(clr[0].credit - clr[0].debit, 2) == delta
    assert "Rounding line added" in note
    cur = sum(s.debit - s.credit for s in j.splits if s.account.endswith("Partner Current Account"))
    assert round(cur, 2) == round(T._ACCR_AMT + delta, 2)
    assert not any("apital" in s.account or "rawing" in s.account for s in j.splits)


@pytest.mark.parametrize("delta", [0.0, 10.01, -25.0])
def test_no_rounding_line_at_zero_or_above_band(delta):
    j, note, _ = build_accrual_journal(_report(delta), ACCTS)
    assert not any("Salary Clearing" in s.account for s in j.splits)
    assert "Rounding line added" not in note


def test_no_rounding_line_when_clearing_not_configured():
    accts = {k: v for k, v in ACCTS.items() if k != "salary_clearing"}
    j, note, _ = build_accrual_journal(_report(3.0), accts)
    assert not any("Clearing" in s.account for s in j.splits)
    assert "NOT booked" in note


@pytest.mark.parametrize("bad", ["Equity:Partner Capital Contribution",
                                 "Equity:Partner Current Account",
                                 "Equity:Drawings:Partner", "Equity:Partner Drawing"])
def test_rounding_never_targets_capital_drawings_or_current(bad):
    with pytest.raises(JournalValidationError):
        build_accrual_journal(_report(3.0), dict(ACCTS, salary_clearing=bad))


def test_rounding_alone_when_accrual_already_ties():
    j, _, _ = build_accrual_journal(_report(2.0, accrual=False), ACCTS)
    assert j is not None
    assert {s.account.rsplit(":", 1)[-1] for s in j.splits} == {
        "Partner Current Account", "Salary Clearing"}


def test_book_already_ties_adds_no_line():
    r = _report(3.0)
    j, _, _ = build_accrual_journal(r, ACCTS, book_gap=0.0)
    assert not any("Salary Clearing" in s.account for s in j.splits)
    j2, _, _ = build_accrual_journal(r, ACCTS, book_gap=3.0)
    assert any("Salary Clearing" in s.account for s in j2.splits)


def test_posted_accrual_is_final_gap_is_none(tmp_path):
    r = _report(3.0)
    aid = accrual_txn_id(r)
    pc = [PostedCheckResult(txn_id=aid, date="d", description="x",
                            status=ALREADY_POSTED, detail="")]
    path = _book(tmp_path, [_accr_txn(aid)], "g")
    assert accrual_book_gap(r, ACCTS, path, "2025-26", posted_check=pc) is None


def test_rerun_after_rounding_posted_does_not_add_again(tmp_path):
    r = _report(3.0)
    aid = accrual_txn_id(r)
    j, _, _ = build_accrual_journal(r, ACCTS)
    amt = round(sum(s.debit - s.credit for s in j.splits
                    if s.account.endswith("Partner Current Account")), 2)
    assert amt == round(T._ACCR_AMT + 3.0, 2)
    path = _book(tmp_path, [_accr_txn(aid, amount=amt)], "rr")
    pc, _ = build_posted_check(r, ACCTS, path, "2025-26")
    assert _status(pc, aid) == ALREADY_POSTED
    assert accrual_book_gap(r, ACCTS, path, "2025-26", posted_check=pc) is None


def test_rounding_does_not_touch_capital():
    j0, _, _ = build_accrual_journal(_report(0.0), ACCTS)
    j3, _, _ = build_accrual_journal(_report(3.0), ACCTS)
    assert not any("Capital" in s.account for s in j0.splits + j3.splits)
