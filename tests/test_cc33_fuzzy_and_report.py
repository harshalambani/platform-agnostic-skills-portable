"""tests/test_cc33_fuzzy_and_report.py -- CC-33 widening: the fuzzy already-booked check for a CRED overage needs a
CCP split; CC-34: two report-text fixes (stale CSV sentence, NOT SETTLED + NOT TIED). Synthetic only."""
from __future__ import annotations

from datetime import date

from agents.skill_cc_spend_booking import journal as J
from agents.skill_cc_spend_booking.bookio import CCP_PATH, BookTxn
from tests import test_cc22_icici_csv as t22
from tests.test_cc24_csv_booking import _book, _res, _src
from tests.test_cc_spend_booking import APR, BANK, MAY, go, row, stmt, tie
from tests.test_cc33_cred_overage_gaps import _cp

DRAWX = "Expenses:Drawings"          # a Drawings account that sits under Expenses (so the fuzzy check can see it)
D = date(2025, 5, 12)


def _ov(num="CCSB-ov1", amount=1.0):
    return J.Journal(num=num, kind=J.K_OVERAGE, date=D, real_date=D, description="CRED payment: overage",
                     amount=amount, side="D", card="C", statement="s", source="s.pdf")


def _spend(amount=1.0):
    return J.Journal(num="CCSB-sp1", kind=J.K_SPEND, date=D, real_date=D, description="d", amount=amount,
                     side="W", card="C", statement="s", account="Expenses:Food", raw_description="")


# ---- FIX 1 ----

def test_a_drawings_entry_without_ccp_does_not_withhold_the_overage():                      # NEGATIVE
    j = _ov()
    unrelated = BookTxn("t1", D, "", "", ((DRAWX, -1.0), (BANK, 1.0)))
    J.mark_booked([j], [unrelated], CCP_PATH)
    assert j.status == J.ST_READY


def test_a_hand_booked_dr_ccp_cr_drawings_still_withholds_the_overage():
    j = _ov()
    hand = BookTxn("t1", D, "", "", ((CCP_PATH, 1.0), (DRAWX, -1.0)))
    J.mark_booked([j], [hand], CCP_PATH)
    assert j.status == J.ST_POSSIBLY


def test_an_exact_num_match_is_still_already_booked():
    j = _ov()
    J.mark_booked([j], [BookTxn("t1", D, "", j.num, ((CCP_PATH, 1.0), (DRAWX, -1.0)))], CCP_PATH)
    assert j.status == J.ST_BOOKED


def test_a_spend_keeps_the_old_fuzzy_behaviour_without_ccp():                                # NEGATIVE
    s = _spend()
    J.mark_booked([s], [BookTxn("t1", D, "", "", (("Expenses:Food", 1.0), (BANK, -1.0)))], CCP_PATH)
    assert s.status == J.ST_POSSIBLY


# ---- FIX 2 (CC-34) ----

def _unpaid(tmp_path, monkeypatch, april_result):
    tmp_path.mkdir(parents=True, exist_ok=True)
    s1 = stmt(APR, [row("SWIGGY ORDER", 500.0, (4, 5))])
    s2 = stmt(MAY, [row("CAFE ONE", 40.0, (5, 6))])
    return go(tmp_path, monkeypatch, [s1, s2], [tie(s1, result=april_result, printed=500.0), tie(s2, printed=40.0)], [])


def test_not_settled_and_not_tied_says_its_rows_are_booked_on_real_dates(tmp_path, monkeypatch):
    reply, rows, _c, _o = _unpaid(tmp_path, monkeypatch, "FAIL")
    sec = reply.split("NOT SETTLED")[1].split("\n\n")[0]
    assert "booked on their real dates" in sec
    assert "500.00" in {r["Amount (Withdrawal)"] for r in rows} or any("SWIGGY" in r["Description"] for r in rows)


def test_a_tied_not_settled_statement_still_says_nothing_booked(tmp_path, monkeypatch):    # NEGATIVE
    reply, rows, _c, _o = _unpaid(tmp_path, monkeypatch, "PASS")
    sec = reply.split("NOT SETTLED")[1].split("\n\n")[0]
    assert "nothing booked" in sec and "real dates" not in sec and "NOT TIED" not in sec


def test_a_report_without_a_not_tied_statement_does_not_gain_the_wording(tmp_path, monkeypatch):   # NEGATIVE
    reply, _r, _c, _o = _unpaid(tmp_path, monkeypatch, "PASS")
    assert "booked on their real dates" not in reply


def _csv_run(tmp_path, monkeypatch, with_payment):
    pay = [("07-Apr-25", "PAYMENT RECEIVED THANK YOU", "-1000.00")] if with_payment else []
    blocks = [(t22.MC, [("05-Apr-25", "SHOP ONE MUMBAI", "1,200.50")] + pay),
              (t22.AMEX, [("06-Apr-25", "CAFE TWO PUNE", "300.00")])]
    from tests.test_cc_spend_booking import book_pay
    txns = [book_pay("p1", 1000.0, date(2025, 4, 8))] if with_payment else []
    return _book(tmp_path, monkeypatch, _res(_src(tmp_path / "s", blocks=blocks)), txns)[0]


def test_the_stale_csv_sentence_is_gone(tmp_path, monkeypatch):
    for n, flag in (("a", True), ("b", False)):
        reply = _csv_run(tmp_path / n, monkeypatch, flag)
        assert "CSV payment rows are not matched to bank payments" not in reply
        assert "CSV EXPORTS" in reply


def test_the_pointer_to_the_matched_section_appears_only_with_matches(tmp_path, monkeypatch):    # NEGATIVE
    with_m = _csv_run(tmp_path / "a", monkeypatch, True)
    without = _csv_run(tmp_path / "b", monkeypatch, False)
    assert "CSV PAYMENT ROWS MATCHED TO BANK PAYMENTS" in with_m
    assert "listed under CSV PAYMENT ROWS MATCHED" not in without
