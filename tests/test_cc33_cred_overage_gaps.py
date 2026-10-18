"""tests/test_cc33_cred_overage_gaps.py -- CC-33: the CRED difference is booked for a PARTLY SETTLED statement and for
a CSV payment row paired with a bank payment, exactly once. Synthetic, made-up round amounts."""
from __future__ import annotations

from datetime import date

from agents.skill_cc_spend_booking.bookio import CCP_PATH, BookTxn
from tests import test_cc22_icici_csv as t22
from tests.test_cc24_csv_booking import _book, _res, _src
from tests.test_cc_spend_booking import APR, MAY, book_pay, go, row, stmt, tie
from tests.test_cc32_settlement import _spent, two

CRED = "CRED payment"
APR_ROWS = [row("SWIGGY ORDER", 2000.0, (4, 5))]


def _cp(amt, day=12):
    return row("CRED PAYMENT RECEIVED", amt, (5, day), kind="payment", direction="Cr")


def _partly(tmp_path, monkeypatch, line=1030.0, paid=1000.0, extra_bank=(), result="PASS"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    s1 = stmt(APR, APR_ROWS)
    s2 = stmt(MAY, [_cp(line)])
    bank = [book_pay("p1", paid, date(2025, 5, 12), desc="CRED CLUB")] + list(extra_bank)
    return go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=2000.0), tie(s2, printed=0.0, result=result)], bank)


def _ov(rows):
    return [r for r in rows if CRED in r["Description"]]


def test_partly_settled_overage_is_booked_once_and_spends_are_not(tmp_path, monkeypatch):
    reply, rows, _c, _o = _partly(tmp_path, monkeypatch)
    assert "PARTLY SETTLED" in reply
    ov = _ov(rows)
    assert len(ov) == 1 and ov[0]["Amount Negated (Deposit)"] == "30.00" and ov[0]["Account"] == "Equity:Drawings"
    assert "2000.00" not in _spent(rows)                       # NEGATIVE: the statement's spends stay unbooked
    assert "RED FLAG" not in reply and "overage 30.00 booked" in reply.replace("CRED overage ", "overage ")
    assert len({r["Transaction ID"] for r in rows}) == len(rows)


def test_partly_exact_payment_books_no_overage(tmp_path, monkeypatch):                    # NEGATIVE
    reply, rows, _c, _o = _partly(tmp_path, monkeypatch, line=1000.0)
    assert "PARTLY SETTLED" in reply and not _ov(rows)


def test_partly_overage_not_rebooked_when_already_in_the_book(tmp_path, monkeypatch):     # NEGATIVE
    _r, rows1, _c, _o = _partly(tmp_path / "a", monkeypatch)
    (tmp_path / "b").mkdir(exist_ok=True)
    num = _ov(rows1)[0]["Transaction ID"]
    booked = BookTxn("b1", date(2025, 5, 12), "x", num, (("Equity:Drawings", -30.0), (CCP_PATH, 30.0)))
    reply, rows2, _c, _o = _partly(tmp_path / "b", monkeypatch, extra_bank=[booked])
    assert not _ov(rows2) and "already booked" in reply


def test_partly_then_settled_books_the_difference_once_in_total(tmp_path, monkeypatch):
    _r, rows1, _c, _o = _partly(tmp_path / "a", monkeypatch)
    (tmp_path / "b").mkdir(exist_ok=True)
    num = _ov(rows1)[0]["Transaction ID"]
    booked = BookTxn("b1", date(2025, 5, 12), "x", num, (("Equity:Drawings", -30.0), (CCP_PATH, 30.0)))
    (tmp_path / "b").mkdir(exist_ok=True)
    (tmp_path / "c").mkdir(exist_ok=True)
    # run 2: the April statement is now fully paid by the same bank payment (statement total 1000)
    s1 = stmt(APR, [row("SWIGGY ORDER", 1000.0, (4, 5))])
    s2 = stmt(MAY, [_cp(1030.0)])
    bank = [book_pay("p1", 1000.0, date(2025, 5, 12), desc="CRED CLUB"), booked]
    reply, rows2, _c, _o = go(tmp_path / "b", monkeypatch, [s1, s2], [tie(s1, printed=1000.0), tie(s2, printed=0.0)], bank)
    assert "1000.00" in _spent(rows2)                        # now settled: spends booked
    assert not _ov(rows2) and "already booked" in reply      # the difference is not booked a second time
    # and without the booked row the settled run would book it (same Num as run 1)
    _r, rows3, _c, _o = go(tmp_path / "c", monkeypatch, [s1, s2], [tie(s1, printed=1000.0), tie(s2, printed=0.0)], bank[:1])
    assert [r["Transaction ID"] for r in _ov(rows3)] == [num]


def test_partly_not_tied_statement_still_books_its_own_rows(tmp_path, monkeypatch):       # NEGATIVE
    s1 = stmt(APR, APR_ROWS)
    s2 = stmt(MAY, [_cp(1030.0), row("SWIGGY ORDER", 300.0, (5, 3))])
    reply, rows, _c, _o = go(tmp_path, monkeypatch, [s1, s2],
                             [tie(s1, printed=2000.0), tie(s2, result="FAIL", printed=300.0)],
                             [book_pay("p1", 1000.0, date(2025, 5, 12), desc="CRED CLUB")])
    assert len(_ov(rows)) == 1 and "NOT TIED" in reply
    assert _spent(rows).count("300.00") == 1
    assert len({r["Transaction ID"] for r in rows}) == len(rows)


def test_a_payment_that_could_pay_two_cards_books_nothing(tmp_path, monkeypatch):         # NEGATIVE (decision 6.6)
    s1 = stmt(APR, APR_ROWS, card="Regalia")
    s2 = stmt(MAY, [_cp(1030.0)], card="Regalia")
    s3 = stmt(MAY, [row("CRED PAYMENT RECEIVED", 1000.0, (5, 12), kind="payment", direction="Cr", card="Other")], card="Other")
    reply, rows, _c, _o = go(tmp_path, monkeypatch, [s1, s2, s3],
                             [tie(s1, printed=2000.0), tie(s2, printed=0.0), tie(s3, printed=0.0)],
                             [book_pay("p1", 1000.0, date(2025, 5, 12), desc="CRED CLUB")])
    assert not _ov(rows)


# ---- CSV payment rows paired with a bank payment ----

def _csv(tmp_path, monkeypatch, amt, bank_amt):
    blocks = [(t22.MC, [("05-Apr-25", "SHOP ONE MUMBAI", "1,200.50"),
                        ("07-Apr-25", "CRED PAYMENT RECEIVED", f"-{amt}")]),
              (t22.AMEX, [("06-Apr-25", "CAFE TWO PUNE", "300.00")])]
    res = _res(_src(tmp_path, blocks=blocks))
    return res, [book_pay("p1", bank_amt, date(2025, 4, 8), desc="CRED CLUB")]


def test_a_csv_pairing_overage_is_booked_once(tmp_path, monkeypatch):
    res, bank = _csv(tmp_path, monkeypatch, "1030.00", 1000.0)
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, bank)
    ov = _ov(rows)
    assert len(ov) == 1 and ov[0]["Amount Negated (Deposit)"] == "30.00" and ov[0]["Account"] == "Equity:Drawings"
    assert "booked" in reply and "RED FLAG" not in reply
    assert len({r["Transaction ID"] for r in rows}) == len(rows)
    assert any(r["Amount (Withdrawal)"] == "1200.50" for r in rows)      # the CSV spend is still booked


def test_a_csv_exact_payment_books_no_overage(tmp_path, monkeypatch):                    # NEGATIVE
    res, bank = _csv(tmp_path, monkeypatch, "1000.00", 1000.0)
    _r, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, bank)
    assert not _ov(rows)


def test_a_csv_overage_is_not_rebooked_on_a_rerun(tmp_path, monkeypatch):                # NEGATIVE
    res, bank = _csv(tmp_path / "a", monkeypatch, "1030.00", 1000.0)
    _r, rows1, _c, _o = _book(tmp_path / "w1", monkeypatch, res, bank)
    num = _ov(rows1)[0]["Transaction ID"]
    booked = BookTxn("b1", date(2025, 4, 8), "x", num, (("Equity:Drawings", -30.0), (CCP_PATH, 30.0)))
    res2, bank2 = _csv(tmp_path / "b", monkeypatch, "1030.00", 1000.0)
    reply, rows2, _c, _o = _book(tmp_path / "w2", monkeypatch, res2, bank2 + [booked])
    assert not _ov(rows2) and "already booked" in reply


def test_a_csv_pairing_that_also_settles_a_pdf_statement_books_the_difference_once(tmp_path, monkeypatch):   # NEGATIVE
    from tests.test_cc32_settlement import CSV_PAID, _pdf_apr_and_csv
    blocks = [(CSV_PAID[0][0], [CSV_PAID[0][1][0], ("07-May-25", "CRED PAYMENT RECEIVED", "-530.00")]), CSV_PAID[1]]
    res = _pdf_apr_and_csv(tmp_path, monkeypatch, blocks)
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, [book_pay("p1", 500.0, date(2025, 5, 8), desc="CRED CLUB")])
    assert "500.00" in _spent(rows)                                   # the PDF statement is settled and booked
    assert len(_ov(rows)) == 1 and _ov(rows)[0]["Amount Negated (Deposit)"] == "30.00"
    assert len({r["Transaction ID"] for r in rows}) == len(rows)
