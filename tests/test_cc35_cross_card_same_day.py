"""tests/test_cc35_cross_card_same_day.py -- CC-35: a narrow exception to decision 6.6. A closed group of open lines and
unused payments over two or more cards is paired when exactly one one-to-one assignment puts each payment on a line of
the same date. Synthetic, made-up round amounts only."""
from __future__ import annotations

from datetime import date

from agents.skill_cc_spend_booking import matcher as M
from agents.skill_cc_spend_booking.bookio import CCP_PATH, BookTxn
from tests.test_cc_spend_booking import APR, MAY, L, P, book_pay, go, row, stmt, tie
from tests.test_cc33_cred_overage_gaps import _ov

D1, D2 = date(2025, 5, 10), date(2025, 5, 13)


def _live():
    lines = [L(1000.00, D1, card="C1"), L(1000.00, D2, desc="CRED PAYMENT", card="C2")]
    pays = [P("A", 1000.00, D1), P("B", 999.00, D2, desc="CRED CLUB")]
    return lines, pays


def _by_line(out):
    return {pr.line.card if hasattr(pr.line, "card") else pr.line.stmt.card: pr for pr in out.pairings}


def test_a_the_live_shape_pairs_both_one_exact_and_one_cred():
    out = M.match_payments(*_live())
    assert not out.ambiguous and not out.payments_unmatched and len(out.pairings) == 2
    m = {pr.line.stmt.card: pr for pr in out.pairings}
    assert m["C1"].payments[0].guid == "A" and m["C1"].kind == "exact" and m["C1"].overage == 0.0
    assert m["C2"].payments[0].guid == "B" and m["C2"].kind == "cred" and m["C2"].overage == 1.0
    assert all("CC-35" in pr.note for pr in out.pairings)


def test_b_two_possible_same_day_assignments_stay_ambiguous():                       # NEGATIVE
    lines = [L(1000.00, D1, card="C1"), L(1000.50, D1, desc="CRED PAYMENT", card="C2")]
    out = M.match_payments(lines, [P("A", 1000.00, D1), P("B", 1000.00, D1)])
    assert out.ambiguous and not [pr for pr in out.pairings if pr.note]


def test_c_one_payment_not_same_day_with_any_line_stays_ambiguous():                 # NEGATIVE
    lines, pays = _live()
    pays[1] = P("B", 999.00, date(2025, 5, 14), desc="CRED CLUB")
    out = M.match_payments(lines, pays)
    assert not [pr for pr in out.pairings if pr.note] and out.ambiguous


def test_d_an_open_group_stays_ambiguous():                                          # NEGATIVE
    lines, pays = _live()
    lines.append(L(1000.00, date(2025, 5, 11), card="C3"))       # a third card's line also fits payment A
    out = M.match_payments(lines, pays + [P("C", 5.00, date(2025, 5, 1))])   # counts equal, group still not closed
    assert not [pr for pr in out.pairings if pr.note]


def test_e_unequal_counts_stay_ambiguous():                                          # NEGATIVE
    lines, pays = _live()
    out = M.match_payments(lines, pays + [P("X", 1000.00, date(2025, 5, 11))])
    assert not [pr for pr in out.pairings if pr.note]
    out = M.match_payments(lines + [L(1000.00, date(2025, 5, 11), card="C3")], pays)
    assert not [pr for pr in out.pairings if pr.note]


def test_f_a_same_day_pair_outside_the_cred_tolerance_is_not_paired():               # NEGATIVE
    lines, pays = _live()
    pays[1] = P("B", 900.00, D2, desc="CRED CLUB")                 # 100.00 off: beyond 50
    out = M.match_payments(lines, pays)
    assert not [pr for pr in out.pairings if pr.note]
    assert not any(pr.payments[0].guid == "B" for pr in out.pairings)


def test_g_a_single_card_group_behaves_as_before():                                  # NEGATIVE
    out = M.match_payments([L(1000.00, D1, card="C1"), L(1000.00, D2, desc="CRED PAYMENT", card="C1")],
                           [P("A", 1000.00, D1), P("B", 999.00, D2, desc="CRED CLUB")])
    assert not [pr for pr in out.pairings if pr.note]
    out2 = M.match_payments([L(1000.00, date(2025, 5, 10))], [P("far", 1000.00, date(2025, 5, 14)), P("near", 1000.00, date(2025, 5, 11))])
    assert [p.guid for pr in out2.pairings for p in pr.payments] == ["near"] and not out2.pairings[0].note


def test_no_payment_or_line_is_paired_twice():                                       # NEGATIVE
    out = M.match_payments(*_live())
    g = [p.guid for pr in out.pairings for p in pr.payments]
    assert len(g) == len(set(g)) and len({id(pr.line) for pr in out.pairings}) == len(out.pairings)


# ---- agent level ----

def _two_cards(tmp_path, monkeypatch, extra_bank=()):
    tmp_path.mkdir(parents=True, exist_ok=True)
    a1 = stmt(APR, [row("SWIGGY ORDER", 1000.0, (4, 5), card="Regalia")], card="Regalia")
    a2 = stmt(APR, [row("CAFE ONE", 1000.0, (4, 6), card="Other")], card="Other")
    m1 = stmt(MAY, [row("PAYMENT RECEIVED", 1000.0, (5, 10), kind="payment", direction="Cr", card="Regalia")], card="Regalia")
    m2 = stmt(MAY, [row("CRED PAYMENT RECEIVED", 1000.0, (5, 13), kind="payment", direction="Cr", card="Other")], card="Other")
    bank = [book_pay("A", 1000.0, date(2025, 5, 10)), book_pay("B", 999.0, date(2025, 5, 13), desc="CRED CLUB")] + list(extra_bank)
    return go(tmp_path, monkeypatch, [a1, a2, m1, m2],
              [tie(a1, printed=1000.0), tie(a2, printed=1000.0), tie(m1, printed=0.0), tie(m2, printed=0.0)], bank)


def test_h_the_pairing_settles_both_statements_and_the_difference_is_booked_once(tmp_path, monkeypatch):
    reply, rows, _c, _o = _two_cards(tmp_path / "a", monkeypatch)
    spent = sorted(r["Amount (Withdrawal)"] for r in rows if r["Amount (Withdrawal)"])
    assert spent == ["1000.00", "1000.00"] and "RED FLAG" not in reply
    ov = _ov(rows)
    assert len(ov) == 1 and ov[0]["Amount Negated (Deposit)"] == "1.00"
    assert len({r["Transaction ID"] for r in rows}) == len(rows)
    # second run: the overage is in the book already (same Num), and the spends too -> nothing booked twice
    booked = [BookTxn("o1", date(2025, 5, 13), "x", ov[0]["Transaction ID"], (("Equity:Drawings", -1.0), (CCP_PATH, 1.0)))]
    reply2, rows2, _c, _o = _two_cards(tmp_path / "b", monkeypatch, extra_bank=booked)
    assert not _ov(rows2) and "already booked" in reply2


def test_i_the_report_shows_the_note_only_on_pairings_made_by_the_rule(tmp_path, monkeypatch):
    reply, _r, _c, _o = _two_cards(tmp_path / "a", monkeypatch)
    assert "PAIRED ACROSS CARDS (CC-35)" in reply and "CC-35" in reply
    # a same-card, ordinary pairing carries no note
    (tmp_path / "b").mkdir()
    s1 = stmt(APR, [row("SWIGGY ORDER", 1000.0, (4, 5))])
    s2 = stmt(MAY, [row("PAYMENT RECEIVED", 1000.0, (5, 10), kind="payment", direction="Cr")])
    reply2, _r, _c, _o = go(tmp_path / "b", monkeypatch, [s1, s2], [tie(s1, printed=1000.0), tie(s2, printed=0.0)],
                            [book_pay("A", 1000.0, date(2025, 5, 10))])
    assert "CC-35" not in reply2
