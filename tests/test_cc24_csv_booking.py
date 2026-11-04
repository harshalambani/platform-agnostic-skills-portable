"""tests/test_cc24_csv_booking.py -- CC-24: Book Spends books the ICICI CSV rows.

Synthetic only: made-up card groups, merchants and amounts. The CSV files are read by the real
CC Transactions extraction and the result is handed to Book Spends.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime

from agents.skill_cc_spend_booking import journal as J
from agents.skill_cc_spend_booking.bookio import BookTxn, CCP_PATH
from tests import test_cc22_icici_csv as t22
from tests.test_cc_spend_booking import BANK, FOOD, book_pay, go

m = t22.m
MC, AMEX, SOLO = t22.MC, t22.AMEX, t22.SOLO
_CARDS = t22.CARDS

BLOCKS = [
    (MC, [("05-Apr-25", "SHOP ONE MUMBAI", "1,200.50"),
          ("07-Apr-25", "PAYMENT RECEIVED THANK YOU", "-5,000.00")]),
    (AMEX, [("06-Apr-25", "CAFE TWO PUNE", "300.00")]),
]


def _src(tmp_path, blocks=BLOCKS, folder="ICICI-Sapphiro", name="s.csv"):
    d = tmp_path / "src"
    t22._write(d, folder, name, t22._csv(blocks))
    return d


def _res(d):
    return m.run_extraction(d, d / "x.xlsx", date(2025, 4, 1), date(2025, 5, 31), cards=_CARDS)


def _book(work, monkeypatch, res, txns=None):
    work.mkdir(parents=True, exist_ok=True)
    return go(work, monkeypatch, [], [], txns or [], result_override=res)


def _amounts(rows):
    return sorted(r["Amount (Withdrawal)"] for r in rows if r["Amount (Withdrawal)"])


def test_csv_rows_are_booked_with_ccsb_nums_and_labelled(tmp_path, monkeypatch):
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, _res(_src(tmp_path)))
    assert _amounts(rows) == ["1200.50", "300.00"]
    assert len(rows) == 2                                          # the payment row is never a spend
    assert all(r["Transaction ID"].startswith("CCSB-") for r in rows)
    assert all(r["Transfer Account"] == CCP_PATH for r in rows)
    assert "CSV export" in reply and "no balance tie-out" in reply
    assert "2 journal(s)" in reply


def test_rerun_of_the_same_csv_books_nothing_new(tmp_path, monkeypatch):        # NEGATIVE
    _r, rows1, _c, _o = _book(tmp_path / "w1", monkeypatch, _res(_src(tmp_path / "a")))
    _r, rows1b, _c, _o = _book(tmp_path / "w2", monkeypatch, _res(_src(tmp_path / "b")))
    ids = [r["Transaction ID"] for r in rows1]
    assert ids and ids == [r["Transaction ID"] for r in rows1b]                  # stable across runs
    booked = [BookTxn(f"b{i}", date(2025, 4, 8), "x", n, ((FOOD, 1.0), (CCP_PATH, -1.0))) for i, n in enumerate(ids)]
    reply, rows2, _c, _o = _book(tmp_path / "w3", monkeypatch, _res(_src(tmp_path / "c")), booked)
    assert rows2 == []
    assert "already booked" in reply


def test_a_hand_booked_csv_row_is_written_and_flagged(tmp_path, monkeypatch):
    hand = BookTxn("h1", date(2025, 4, 5), "Shop one purchase", "", ((FOOD, 1200.50), (BANK, -1200.50)))
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, _res(_src(tmp_path)), [hand])
    assert "1200.50" in _amounts(rows)                                           # written, never withheld
    assert "300.00" in _amounts(rows)                                            # the other row is still offered
    assert "CHECK BEFORE IMPORT - possibly already in the book" in reply


def test_a_refused_csv_books_nothing_and_is_listed_with_its_reason(tmp_path, monkeypatch):   # NEGATIVE
    res = _res(_src(tmp_path, folder="ICICI-Amazon"))   # two networks are Sapphiro: the folder is wrong
    assert res.csv_statements == [] and res.csv_refused
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res)
    assert rows == []
    assert "REFUSED CSV exports" in reply and "say ICICI-Sapphiro" in reply
    assert "NOT complete" in reply


def test_a_csv_whose_card_does_not_match_its_folder_books_nothing(tmp_path, monkeypatch):    # NEGATIVE
    d = _src(tmp_path, blocks=[(SOLO, [("05-Apr-25", "SHOP SOLO", "10.00")])], folder="ICICI-Sapphiro")
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, _res(d))
    assert rows == []
    assert "REFUSED CSV exports" in reply


def test_an_unreadable_csv_books_nothing(tmp_path, monkeypatch):                 # NEGATIVE
    d = tmp_path / "src"
    t22._write(d, "ICICI-Sapphiro", "bad.csv", t22._csv(BLOCKS, header=t22.HEADER.replace("Sign", "S")))
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, _res(d))
    assert rows == [] and "REFUSED CSV exports" in reply


# ---- CSV + PDF of the same card ----

PDF_SPEC = {
    "apr.pdf": ((date(2025, 4, 1), date(2025, 4, 30)),
                [((2025, 4, 5), "SHOP ONE MUMBAI", 1200.50, "Dr"), ((2025, 4, 6), "CAFE TWO PUNE", 300.0, "Dr")]),
    "may.pdf": ((date(2025, 5, 1), date(2025, 5, 31)),
                [((2025, 5, 12), "PAYMENT RECEIVED", 1500.50, "Cr")]),
}
CSV_PLUS = [(MC, [("05-Apr-25", "SHOP ONE MUMBAI", "1,200.50"), ("20-Apr-25", "NEW SHOP DELHI", "77.00")]),
            (AMEX, [("06-Apr-25", "CAFE TWO PUNE", "300.00")])]


def _with_pdfs(tmp_path, monkeypatch, fail=()):
    d = tmp_path / "src"
    t22._write(d, "ICICI-Sapphiro", "apr.pdf", "x")
    t22._write(d, "ICICI-Sapphiro", "may.pdf", "x")
    t22._write(d, "ICICI-Sapphiro", "s.csv", t22._csv(CSV_PLUS))

    def parse(lines, bank, card, source):
        period, spec = PDF_SPEC[source]
        rows = [m._mk_row(bank, card, datetime(*dt), desc, amt, dr) for dt, desc, amt, dr in spec]
        for r in rows:
            r.kind = "payment" if "PAYMENT" in r.description else "spend"
        st = m.Statement(bank, card, source, period, "", m.Parsed(rows=rows), list(lines))
        for r in rows:
            r.period, r.source = st.label, source
            r.statement = f"{st.card} | {st.label}"
        return st
    t22._patch_pdf(monkeypatch, parse)
    res = _res(d)
    res.ties = [dataclasses.replace(t, result="FAIL" if st.source in fail else "PASS", gap="synthetic gap",
                                printed=1500.50 if st.source == "apr.pdf" else 0.0, computed=1500.50 if st.source == "apr.pdf" else 0.0)
                for st, t in zip(res.statements, res.ties)]
    return res


def test_a_row_in_both_the_pdf_and_the_csv_is_booked_once(tmp_path, monkeypatch):    # NEGATIVE
    res = _with_pdfs(tmp_path, monkeypatch)
    assert res.csv_statements and len(res.csv_statements[0].parsed.rows) == 1       # only the CSV-only row is left
    _r, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, [book_pay("p1", 1500.50, date(2025, 5, 12))])
    assert _amounts(rows) == ["1200.50", "300.00", "77.00"]                         # each exactly once
    assert len({r["Transaction ID"] for r in rows}) == 3


def test_an_untied_pdf_and_a_csv_with_the_same_row_book_it_once(tmp_path, monkeypatch):        # NEGATIVE
    res = _with_pdfs(tmp_path, monkeypatch, fail=("apr.pdf",))
    reply, rows, _c, _o = _book(tmp_path / "w", monkeypatch, res, [book_pay("p1", 1500.50, date(2025, 5, 12))])
    assert "NOT TIED - IMPORTED, CHECK BEFORE IMPORT" in reply and "REFUSED - statements" not in reply
    assert _amounts(rows) == ["1200.50", "300.00", "77.00"]                         # CC-26: from the PDF, once; never twice
    assert len({r["Transaction ID"] for r in rows}) == 3
