"""tests/test_cc32_settlement.py -- CC-32: when a statement counts as settled. Synthetic, made-up round amounts."""
from __future__ import annotations

import dataclasses
from datetime import date, datetime
from types import SimpleNamespace

from agents.skill_cc_spend_booking import matcher as M
from agents.skill_cc_spend_booking.bookio import CCP_PATH, BookTxn
from tests import test_cc22_icici_csv as t22
from tests.test_cc24_csv_booking import _book, _res
from tests.test_cc_spend_booking import APR, DRAW, FOOD, MAY, book_pay, go, row, stmt, tie

m = t22.m


def _spent(rows):
    return sorted(r["Amount (Withdrawal)"] for r in rows if r["Amount (Withdrawal)"])


def _workbook_text(out):
    import openpyxl
    wb = openpyxl.load_workbook(out.with_name(out.stem + "_report.xlsx"))
    return " | ".join(str(c.value) for ws in wb for r in ws.iter_rows() for c in r if c.value is not None)


def two(tmp_path, monkeypatch, may_rows, bank, printed_apr=500.0, apr_rows=None, printed_may=0.0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    s1 = stmt(APR, apr_rows or [row("SWIGGY ORDER", 500.0, (4, 5))])
    s2 = stmt(MAY, may_rows)
    return go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=printed_apr), tie(s2, printed=printed_may)], bank)


def PAY(amt, day=12):
    return row("PAYMENT RECEIVED", amt, (5, day), kind="payment", direction="Cr")


REFUND = row("MERCHANT REFUND", 200.0, (5, 14), kind="refund", direction="Cr")


# ---- (a) a statement in credit owes nothing ----

def test_a_statement_in_credit_is_settled_with_nothing_owed(tmp_path, monkeypatch):
    rows_ = [row("SWIGGY ORDER", 500.0, (4, 5)), row("BIG REFUND", 600.0, (4, 20), kind="refund", direction="Cr")]
    s1 = stmt(APR, rows_)
    reply, rows, _c, out = go(tmp_path, monkeypatch, [s1], [tie(s1, printed=-100.0)], [])
    assert "from 1 settled statement(s)" in reply and "AWAITING PAYMENT" not in reply
    assert "500.00" in _spent(rows)
    assert "in credit, nothing owed" in _workbook_text(out)


def test_a_closing_balance_just_above_tolerance_is_still_awaiting_payment(tmp_path, monkeypatch):   # NEGATIVE
    s1 = stmt(APR, [row("SWIGGY ORDER", 1.01, (4, 5))])
    reply, rows, _c, out = go(tmp_path, monkeypatch, [s1], [tie(s1, printed=1.01)], [])
    assert "AWAITING PAYMENT" in reply and rows == []
    assert "in credit, nothing owed" not in reply + _workbook_text(out)


# ---- (b) refunds and cashback reduce the balance carried ----

def test_payment_plus_refund_settles_the_statement(tmp_path, monkeypatch):
    reply, rows, _c, _o = two(tmp_path, monkeypatch, [PAY(300.0), REFUND], [book_pay("p1", 300.0, date(2025, 5, 12))])
    assert "500.00" in _spent(rows) and "PARTLY SETTLED" not in reply


def test_without_the_refund_it_is_partly_settled_and_nothing_is_booked(tmp_path, monkeypatch):       # NEGATIVE
    reply, rows, _c, _o = two(tmp_path, monkeypatch, [PAY(300.0)], [book_pay("p1", 300.0, date(2025, 5, 12))])
    assert "PARTLY SETTLED" in reply and "500.00" not in _spent(rows)


def test_an_unmatched_payment_line_never_counts(tmp_path, monkeypatch):                              # NEGATIVE
    reply, rows, _c, _o = two(tmp_path, monkeypatch, [PAY(300.0), PAY(200.0, 20)],
                              [book_pay("p1", 300.0, date(2025, 5, 12))])
    assert "PARTLY SETTLED" in reply and "500.00" not in _spent(rows)


def test_cashback_credit_also_reduces_the_balance(tmp_path, monkeypatch):
    cb = row("CASHBACK CREDIT", 200.0, (5, 14), kind="cashback", direction="Cr")
    reply, rows, _c, _o = two(tmp_path, monkeypatch, [PAY(300.0), cb], [book_pay("p1", 300.0, date(2025, 5, 12))])
    assert "500.00" in _spent(rows) and "PARTLY SETTLED" not in reply


# ---- (c) the next period is a whole-booked CSV export ----

def _pdf_apr_and_csv(tmp_path, monkeypatch, csv_blocks):
    d = tmp_path / "src"
    t22._write(d, "ICICI-Sapphiro", "apr.pdf", "x")
    t22._write(d, "ICICI-Sapphiro", "s.csv", t22._csv(csv_blocks))

    def parse(lines, bank, card, source):
        r = m._mk_row(bank, card, datetime(2025, 4, 5), "SHOP ONE MUMBAI", 500.0, "Dr")
        r.kind = "spend"
        st = m.Statement(bank, card, source, APR, "", m.Parsed(rows=[r]), list(lines))
        r.period, r.source = st.label, source
        r.statement = f"{st.card} | {st.label}"
        return st
    t22._patch_pdf(monkeypatch, parse)
    res = _res(d)
    res.ties = [dataclasses.replace(t, result="PASS", printed=500.0, computed=500.0) for t in res.ties]
    return res


CSV_PAID = [(t22.MC, [("01-May-25", "CAFE ONE PUNE", "40.00"), ("07-May-25", "PAYMENT RECEIVED THANK YOU", "-500.00")]),
            (t22.AMEX, [("02-May-25", "SHOP TWO DELHI", "60.00")])]


def test_csv_payment_rows_matched_to_the_bank_settle_the_prior_statement(tmp_path, monkeypatch):
    res = _pdf_apr_and_csv(tmp_path, monkeypatch, CSV_PAID)
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, [book_pay("p1", 500.0, date(2025, 5, 8))])
    assert "500.00" in _spent(rows) and "AWAITING PAYMENT" not in reply


def test_csv_payment_rows_not_matched_to_the_bank_do_not_count(tmp_path, monkeypatch):               # NEGATIVE
    res = _pdf_apr_and_csv(tmp_path, monkeypatch, CSV_PAID)
    _reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, [])
    assert "500.00" not in _spent(rows)


def test_a_csv_payment_dated_before_the_statement_end_does_not_count(tmp_path, monkeypatch):        # NEGATIVE
    blocks = [(t22.MC, [("01-May-25", "CAFE ONE PUNE", "40.00")]),
              (t22.AMEX, [("02-May-25", "SHOP TWO DELHI", "60.00"),
                          ("25-Apr-25", "PAYMENT RECEIVED THANK YOU", "-500.00")])]
    res = _pdf_apr_and_csv(tmp_path, monkeypatch, blocks)
    _r, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, [book_pay("p1", 500.0, date(2025, 4, 26))])
    assert "500.00" not in _spent(rows)


# ---- (d) overlapping periods: the closing balance picks the prior ----

def _st(card, period, summary=None):
    return SimpleNamespace(card=card, period=period, parsed=SimpleNamespace(summary=summary or {}, rows=[]))


def _tie(printed):
    return SimpleNamespace(printed=printed)


def test_the_statement_whose_closing_equals_the_carried_balance_is_the_prior():
    a = _st("C", (date(2025, 3, 1), date(2025, 4, 20)))
    b = _st("C", (date(2025, 3, 10), date(2025, 4, 30)))
    k = _st("C", (date(2025, 5, 1), date(2025, 5, 31)), {"prev": 400.0})
    ties = {id(a): _tie(400.0), id(b): _tie(900.0)}
    assert M.prior_statement(k, [a, b, k], ties, lambda s, p: None) is a


def test_no_balance_match_behaves_as_before():                                                       # NEGATIVE
    a = _st("C", (date(2025, 3, 1), date(2025, 4, 30)))
    k = _st("C", (date(2025, 5, 1), date(2025, 5, 31)), {"prev": 400.0})
    ties = {id(a): _tie(999.0)}
    assert M.prior_statement(k, [a, k], ties, lambda s, p: a) is a
    assert M.prior_statement(k, [a, k], ties, lambda s, p: None) is None


def test_two_statements_with_the_same_closing_are_not_guessed():                                    # NEGATIVE
    a = _st("C", (date(2025, 3, 1), date(2025, 4, 20)))
    b = _st("C", (date(2025, 3, 10), date(2025, 4, 30)))
    k = _st("C", (date(2025, 5, 1), date(2025, 5, 31)), {"prev": 400.0})
    ties = {id(a): _tie(400.0), id(b): _tie(400.0)}
    assert M.prior_statement(k, [a, b, k], ties, lambda s, p: None) is None


# ---- (e) a cashback Dr row is the exact reverse of a cashback credit ----

def test_a_cashback_debit_row_reverses_the_credit_in_the_same_account(tmp_path, monkeypatch):
    cr = row("CASHBACK CREDIT", 50.0, (4, 28), kind="cashback", direction="Cr")
    dr = row("CASHBACK REVERSED", 20.0, (4, 29), kind="cashback", direction="Dr")
    s1 = stmt(APR, [row("SWIGGY ORDER", 500.0, (4, 5)), cr, dr])
    s2 = stmt(MAY, [PAY(470.0)])
    _r, rows, _c, _o = go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=470.0), tie(s2, printed=0.0)],
                          [book_pay("p1", 470.0, date(2025, 5, 12))])
    rev = next(r for r in rows if "REVERSED" in r["Description"])
    got = next(r for r in rows if "CASHBACK CREDIT" in r["Description"])
    assert rev["Account"] == got["Account"] == DRAW
    assert rev["Amount (Withdrawal)"] == "20.00" and got["Amount Negated (Deposit)"] == "50.00"        # opposite sides
    assert rev["Account"] != FOOD and "Suspense" not in rev["Account"]                                 # NEGATIVE


# ---- (f) the NOT SETTLED wording ----

def test_unmatched_payment_lines_on_the_next_statement_are_named_in_the_reason(tmp_path, monkeypatch):
    reply, rows, _c, _o = two(tmp_path, monkeypatch, [PAY(500.0)], [])
    assert "Payments without statement" in reply
    assert "500.00" not in _spent(rows)
    assert "shows no payment" not in reply                                                           # NEGATIVE


def test_no_payment_at_all_keeps_the_plain_wording(tmp_path, monkeypatch):                          # NEGATIVE
    reply, _r, _c, _o = two(tmp_path, monkeypatch, [row("SWIGGY ORDER", 70.0, (5, 3))], [], printed_may=70.0)
    assert "shows no payment" in reply


# ---- invariants ----

def test_spends_are_booked_once_and_nothing_is_booked_twice_on_a_rerun(tmp_path, monkeypatch):
    bank = [book_pay("p1", 300.0, date(2025, 5, 12))]
    _r, rows1, _c, _o = two(tmp_path / "a", monkeypatch, [PAY(300.0), REFUND], bank)
    ids = [r["Transaction ID"] for r in rows1]
    assert ids and len(ids) == len(set(ids))
    booked = [BookTxn(f"b{i}", date(2025, 5, 12), "x", n, ((FOOD, 1.0), (CCP_PATH, -1.0))) for i, n in enumerate(ids)]
    reply, rows2, _c, _o = two(tmp_path / "b", monkeypatch, [PAY(300.0), REFUND], bank + booked)
    assert rows2 == [] and "already booked" in reply                                                 # NEGATIVE
    _r, rows3, _c, _o = two(tmp_path / "c", monkeypatch, [PAY(300.0), REFUND], bank)
    assert [r["Transaction ID"] for r in rows3] == ids                                               # old Nums identical
