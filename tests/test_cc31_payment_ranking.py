"""tests/test_cc31_payment_ranking.py -- CC-31 (a)(b)(c): the CRED tolerance, ranking before AMBIGUOUS and a
payment line whose prior period is a whole-booked CSV export. Synthetic, made-up round amounts only."""
from __future__ import annotations

import dataclasses
from datetime import date, datetime

from agents.skill_cc_spend_booking import matcher as M
from tests import test_cc22_icici_csv as t22
from tests.test_cc24_csv_booking import _book, _res
from tests.test_cc_spend_booking import L, P, book_pay

m = t22.m
D = date(2025, 5, 10)


def test_default_tolerance_is_fifty_rupees_and_a_named_constant():
    assert M.CRED_MAX_OVERAGE == 50.0


def test_exact_amount_beats_a_cred_overage_when_both_fit():
    exact, cred = P("ex", 1000.00, D), P("cr", 999.85, D, desc="CRED CLUB")
    out = M.match_payments([L(1000.00, D)], [cred, exact])
    assert [p.guid for pr in out.pairings for p in pr.payments] == ["ex"] and not out.ambiguous


def test_nearest_date_decides_between_two_equal_fits():
    out = M.match_payments([L(1000.00, D)], [P("far", 1000.00, date(2025, 5, 14)), P("near", 1000.00, date(2025, 5, 11))])
    assert [p.guid for pr in out.pairings for p in pr.payments] == ["near"] and not out.ambiguous


def test_a_true_tie_on_one_card_stays_ambiguous():                                    # NEGATIVE
    out = M.match_payments([L(1000.00, D)], [P("a", 1000.00, date(2025, 5, 11)), P("b", 1000.00, date(2025, 5, 9))])
    assert out.ambiguous and not out.pairings


def test_a_tie_across_two_different_cards_stays_ambiguous():                          # NEGATIVE (guard 6.6)
    out = M.match_payments([L(1000.00, D, card="C1"), L(1000.00, D, card="C2")], [P("a", 1000.00, D)])
    assert out.ambiguous and not out.pairings


def test_one_payment_never_matches_two_lines_and_one_line_never_takes_two():          # NEGATIVE
    out = M.match_payments([L(2.00, D), L(2.00, D)], [P("a", 2.00, D)])
    used = [p.guid for pr in out.pairings for p in pr.payments]
    assert len(used) == len(set(used)) <= 1
    out = M.match_payments([L(2.00, D)], [P("a", 2.00, D), P("b", 2.00, D)])
    assert sum(len(pr.payments) for pr in out.pairings) <= 1


def test_two_same_day_pairs_resolve_one_to_one():
    out = M.match_payments([L(2.00, D), L(7.00, D)], [P("a", 2.00, D), P("b", 7.00, D)])
    assert len(out.pairings) == 2 and not out.ambiguous
    assert {(pr.line.amount, pr.payments[0].guid) for pr in out.pairings} == {(2.0, "a"), (7.0, "b")}


def test_a_gap_over_the_tolerance_is_not_matched():                                   # NEGATIVE
    out = M.match_payments([L(1051.00, D, desc="CRED PAYMENT")], [P("a", 1000.00, D, desc="CRED CLUB")])
    assert not out.pairings and out.payments_unmatched


def test_a_direct_payment_with_a_gap_is_not_matched():                                # NEGATIVE
    out = M.match_payments([L(1030.00, D)], [P("a", 1000.00, D)])
    assert not out.pairings


def test_the_bank_amount_above_the_line_is_not_matched():                             # NEGATIVE
    out = M.match_payments([L(1000.00, D, desc="CRED PAYMENT")], [P("a", 1030.00, D, desc="CRED CLUB")])
    assert not out.pairings


def test_the_tolerance_is_a_parameter_a_30_rupee_gap():
    line, pay = L(1030.00, D, desc="CRED PAYMENT"), P("a", 1000.00, D, desc="CRED CLUB")
    assert not M.match_payments([line], [pay], 15.0).pairings                           # NEGATIVE at 15
    assert M.match_payments([line], [pay], 50.0).pairings[0].overage == 30.0


# ---- (c) the prior period is a whole-booked CSV export ----

PDF_MAY = ((date(2025, 5, 1), date(2025, 5, 31)), [((2025, 5, 12), "PAYMENT RECEIVED", 1500.50, "Cr")])
CSV = [(t22.MC, [("05-Apr-25", "SHOP ONE MUMBAI", "1,200.50"), ("30-Apr-25", "NEW SHOP DELHI", "300.00")]),
       (t22.AMEX, [("06-Apr-25", "CAFE TWO PUNE", "300.00")])]


def _res_may_only(tmp_path, monkeypatch, with_csv=True, desc="PAYMENT RECEIVED", amt=1500.50):
    d = tmp_path / "src"
    t22._write(d, "ICICI-Sapphiro", "may.pdf", "x")
    if with_csv:
        t22._write(d, "ICICI-Sapphiro", "s.csv", t22._csv(CSV))

    def parse(lines, bank, card, source):
        period, (dt, _d, _a, dr) = PDF_MAY[0], PDF_MAY[1][0]
        r = m._mk_row(bank, card, datetime(*dt), desc, amt, dr)
        r.kind = "payment"
        st = m.Statement(bank, card, source, period, "", m.Parsed(rows=[r]), list(lines))
        r.period, r.source = st.label, source
        r.statement = f"{st.card} | {st.label}"
        return st
    t22._patch_pdf(monkeypatch, parse)
    res = _res(d)
    res.ties = [dataclasses.replace(t, result="PASS", printed=0.0, computed=0.0) for t in res.ties]
    return res


def test_a_payment_whose_prior_period_is_a_csv_is_matched_with_no_red_flag(tmp_path, monkeypatch):
    res = _res_may_only(tmp_path, monkeypatch)
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, [book_pay("p1", 1500.50, date(2025, 5, 12))])
    assert "RED FLAG" not in reply and "not among the statements read" not in reply
    assert "BOOKED FROM A CSV EXPORT" in reply
    assert len(rows) == 3                                           # the three CSV spends, once each


def test_without_a_covering_csv_the_red_flag_stays(tmp_path, monkeypatch):               # NEGATIVE
    res = _res_may_only(tmp_path, monkeypatch, with_csv=False)
    reply, _r, _c, _o = _book(tmp_path / "w", monkeypatch, res, [book_pay("p1", 1500.50, date(2025, 5, 12))])
    assert "RED FLAG" in reply and "BOOKED FROM A CSV EXPORT" not in reply


def test_the_csv_prior_pays_nothing_twice_on_a_rerun(tmp_path, monkeypatch):             # NEGATIVE
    res = _res_may_only(tmp_path, monkeypatch)
    _r, rows1, _c, _o = _book(tmp_path / "w1", monkeypatch, res, [book_pay("p1", 1500.50, date(2025, 5, 12))])
    assert len({r["Transaction ID"] for r in rows1}) == len(rows1)


# ---- review fixes: guard 6.6 (two cards), and the CRED overage of a CSV-prior settlement ----

def test_one_payment_that_could_pay_two_cards_is_ambiguous_whatever_the_date():              # guard 6.6
    out = M.match_payments([L(1000.00, date(2025, 5, 11), card="C1"), L(1000.00, date(2025, 5, 14), card="C2")],
                           [P("a", 1000.00, date(2025, 5, 12))])
    assert not out.pairings and out.ambiguous and out.payments_unmatched


def test_two_cards_exact_against_cred_is_still_ambiguous():                                  # guard 6.6
    out = M.match_payments([L(1000.00, D, card="C1"), L(1030.00, D, desc="CRED PAYMENT", card="C2")],
                           [P("a", 1000.00, D, desc="CRED CLUB")])
    assert not out.pairings and out.ambiguous


def test_the_same_card_with_different_dates_still_pairs_with_the_nearest():                  # NEGATIVE of the guard
    out = M.match_payments([L(1000.00, date(2025, 5, 11), card="C1"), L(1000.00, date(2025, 5, 14), card="C1")],
                           [P("a", 1000.00, date(2025, 5, 12))])
    assert len(out.pairings) == 1 and out.pairings[0].line.date == date(2025, 5, 11) and not out.ambiguous


def _cred_prior(tmp_path, monkeypatch, desc, amt):
    res = _res_may_only(tmp_path, monkeypatch, desc=desc, amt=amt)
    return res


def test_a_cred_overage_on_a_csv_prior_settlement_is_booked_once(tmp_path, monkeypatch):
    res = _cred_prior(tmp_path, monkeypatch, "CRED PAYMENT RECEIVED", 1530.50)
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, [book_pay("p1", 1500.50, date(2025, 5, 12), desc="CRED CLUB")])
    ov = [r for r in rows if "CRED payment" in r["Description"]]
    assert len(ov) == 1 and ov[0]["Amount Negated (Deposit)"] == "30.00" and ov[0]["Account"] == "Equity:Drawings"
    assert "RED FLAG" not in reply
    assert len({r["Transaction ID"] for r in rows}) == len(rows)


def test_the_overage_has_the_same_num_on_a_rerun_and_the_booked_check_catches_it(tmp_path, monkeypatch):   # NEGATIVE
    from agents.skill_cc_spend_booking.bookio import CCP_PATH, BookTxn
    bank = [book_pay("p1", 1500.50, date(2025, 5, 12), desc="CRED CLUB")]
    res = _cred_prior(tmp_path / "a", monkeypatch, "CRED PAYMENT RECEIVED", 1530.50)
    _r, rows1, _c, _o = _book(tmp_path / "w1", monkeypatch, res, bank)
    ov1 = next(r for r in rows1 if "CRED payment" in r["Description"])
    res2 = _cred_prior(tmp_path / "b", monkeypatch, "CRED PAYMENT RECEIVED", 1530.50)
    _r, rows2, _c, _o = _book(tmp_path / "w2", monkeypatch, res2, bank)
    assert next(r for r in rows2 if "CRED payment" in r["Description"])["Transaction ID"] == ov1["Transaction ID"]
    booked = [BookTxn("b1", date(2025, 5, 12), "x", ov1["Transaction ID"], (("Equity:Drawings", -30.0), (CCP_PATH, 30.0)))]
    res3 = _cred_prior(tmp_path / "c", monkeypatch, "CRED PAYMENT RECEIVED", 1530.50)
    reply, rows3, _c, _o = _book(tmp_path / "w3", monkeypatch, res3, bank + booked)
    assert not any("CRED payment" in r["Description"] for r in rows3) and "already booked" in reply


def test_an_exact_amount_csv_prior_pairing_books_no_overage(tmp_path, monkeypatch):            # NEGATIVE
    res = _cred_prior(tmp_path, monkeypatch, "CRED PAYMENT RECEIVED", 1500.50)
    _r, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, [book_pay("p1", 1500.50, date(2025, 5, 12), desc="CRED CLUB")])
    assert not any("CRED payment" in r["Description"] for r in rows)
