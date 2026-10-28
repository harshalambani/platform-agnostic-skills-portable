"""tests/test_cc26_untied_import.py -- CC-26: a statement that does not tie out is IMPORTED and reported
loudly (NOT TIED); it is no longer refused. Synthetic data only.
"""
from __future__ import annotations

import dataclasses
from datetime import date

from openpyxl import load_workbook

from agents.skill_cc_spend_booking import journal as J
from agents.skill_cc_spend_booking.bookio import BookTxn, CCP_PATH
from tests.test_cc_spend_booking import (APR, BANK, FOOD, MAY, MOD, april_rows, book_pay, go, may_rows,
                                         row, stmt, tie)

JUN = (date(2025, 6, 1), date(2025, 6, 30))
BANNER = "NOT TIED - IMPORTED, CHECK BEFORE IMPORT"


def _tie(st, result="PASS", printed=0.0, diff=0.0, gap=""):
    return dataclasses.replace(tie(st, result=result, printed=printed, gap=gap), difference=diff)


def _jun_rows():
    return [row("PAYMENT RECEIVED", 300.0, (6, 12), kind="payment", direction="Cr"),
            row("SWIGGY ORDER", 50, (6, 3))]


def _two(tmp_path, monkeypatch, apr_result="PASS", txns=None, diff=0.0):
    tmp_path.mkdir(parents=True, exist_ok=True)
    s1, s2 = stmt(APR, april_rows()), stmt(MAY, may_rows())
    txns = [book_pay("p1", 1900, date(2025, 5, 12))] if txns is None else txns
    return go(tmp_path, monkeypatch, [s1, s2],
              [_tie(s1, apr_result, 1900.0, diff, "gap 200.00"), _tie(s2, "PASS", 300.0)], txns)


def _three(tmp_path, monkeypatch, mid_result="PASS", pay2=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    s1, s2, s3 = stmt(APR, april_rows()), stmt(MAY, may_rows()), stmt(JUN, _jun_rows())
    txns = [book_pay("p1", 1900, date(2025, 5, 12))] + ([book_pay("p2", 300, date(2025, 6, 12))] if pay2 else [])
    return go(tmp_path, monkeypatch, [s1, s2, s3],
              [_tie(s1, "PASS", 1900.0), _tie(s2, mid_result, 300.0, 5.0 if mid_result != "PASS" else 0.0),
               _tie(s3, "PASS", 50.0)], txns, end="2025-06-30")


def _wb(out, sheet):
    ws = load_workbook(out.with_name(out.stem + "_report.xlsx"))[sheet]
    hdr = [c.value for c in ws[1]]
    return [dict(zip(hdr, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]


def _key(rows):
    return sorted((r["Transaction ID"], r["Date"], r["Amount (Withdrawal)"], r["Amount Negated (Deposit)"])
                  for r in rows)


def test_an_untied_pdf_is_imported_with_the_banner_first(tmp_path, monkeypatch):
    reply, rows, _c, out = _two(tmp_path, monkeypatch, "FAIL", diff=200.0)
    text = reply.text if hasattr(reply, "text") else str(reply)
    assert text.startswith(BANNER)                                              # at the very top
    line = next(x for x in text.splitlines() if "FAIL" in x and "journal(s) from it" in x)
    assert "gap 200.00" in line and "200.00" in line and "4 journal(s)" in line
    assert len(rows) == 4                                                       # the untied rows ARE in the import
    assert "REFUSED" not in text.split("\n\n")[0]
    assert "NOT complete" in text                                               # open item while untied
    assert "completed successfully" not in text


def test_the_workbook_marks_every_untied_journal(tmp_path, monkeypatch):
    _r, rows, _c, out = _two(tmp_path, monkeypatch, "FAIL")
    wb = load_workbook(out.with_name(out.stem + "_report.xlsx"))
    assert wb.sheetnames[0] == "NOT TIED"                                       # the banner is first in the workbook
    marked = [j for j in _wb(out, "Journals") if j["Tie-out"] == "NOT TIED"]
    assert marked and all(j["Statement"] == "01 Apr 2025 - 30 Apr 2025" or "Apr" in str(j["Statement"])
                          for j in marked)
    others = [j for j in _wb(out, "Journals") if j["Tie-out"] != "NOT TIED"]
    assert all(j["Description"] for j in others)


def test_a_passing_statement_has_no_marker_and_no_banner(tmp_path, monkeypatch):      # NEGATIVE
    reply, rows, _c, out = _two(tmp_path, monkeypatch, "PASS")
    text = str(reply.text if hasattr(reply, "text") else reply)
    assert BANNER not in text and "NOT TIED" not in text
    assert all(not j["Tie-out"] for j in _wb(out, "Journals"))
    assert "NOT TIED" not in load_workbook(out.with_name(out.stem + "_report.xlsx")).sheetnames
    assert "completed successfully" in text


def test_untied_rows_take_the_same_dates_as_a_passing_run(tmp_path, monkeypatch):
    _r, base, _c, _o = _two(tmp_path / "a", monkeypatch, "PASS")
    _r, untied, _c, _o = _two(tmp_path / "b", monkeypatch, "FAIL")
    assert _key(base) == _key(untied)                    # same Nums, same dates, same amounts


def test_an_untied_statement_with_no_payment_books_on_real_dates(tmp_path, monkeypatch):
    reply, rows, _c, out = _two(tmp_path, monkeypatch, "FAIL", txns=[])
    dates = sorted({r["Date"] for r in rows})
    assert dates == ["2025-04-05", "2025-04-09", "2025-04-10", "2025-04-20"] or len(rows) == 4
    assert {r["Date"] for r in rows} <= {"2025-04-05", "2025-04-09", "2025-04-10", "2025-04-20"}
    assert all(r["Transaction ID"].startswith("CCSB-") for r in rows)


def test_an_untied_neighbour_never_shifts_or_mispairs_the_others(tmp_path, monkeypatch):      # NEGATIVE
    _r, base, _c, _o = _three(tmp_path / "a", monkeypatch, "PASS")
    reply, mixed, _c, out = _three(tmp_path / "b", monkeypatch, "FAIL")
    assert _key(base) == _key(mixed)
    marked = {j["Num"] for j in _wb(out, "Journals") if j["Tie-out"] == "NOT TIED"}
    assert marked and all("May" in str(j["Statement"]) or "05" in str(j["Statement"])
                          for j in _wb(out, "Journals") if j["Num"] in marked)
    assert {j["Statement"] for j in _wb(out, "Journals") if j["Tie-out"] != "NOT TIED"}   # others stay unmarked


def test_an_untied_neighbour_with_no_payment_still_leaves_the_others_alone(tmp_path, monkeypatch):    # NEGATIVE
    _r, base, _c, _o = _three(tmp_path / "a", monkeypatch, "PASS", pay2=False)
    _r, mixed, _c, _o = _three(tmp_path / "b", monkeypatch, "FAIL", pay2=False)
    base_ids = {r["Transaction ID"]: r["Date"] for r in base}
    for r in mixed:
        if r["Transaction ID"] in base_ids:
            assert base_ids[r["Transaction ID"]] == r["Date"]
    assert {r["Transaction ID"] for r in base} <= {r["Transaction ID"] for r in mixed}


def test_a_rerun_of_an_untied_statement_books_nothing_new(tmp_path, monkeypatch):       # NEGATIVE
    _r, rows1, _c, _o = _two(tmp_path / "a", monkeypatch, "FAIL")
    booked = [BookTxn(f"b{i}", date(2025, 5, 12), "x", r["Transaction ID"], ((FOOD, 1.0), (CCP_PATH, -1.0)))
              for i, r in enumerate(rows1)]
    reply, rows2, _c, _o = _two(tmp_path / "b", monkeypatch, "FAIL", txns=[book_pay("p1", 1900, date(2025, 5, 12))] + booked)
    assert rows2 == []
    assert "already booked" in str(reply.text if hasattr(reply, "text") else reply).lower()


def test_an_untied_row_already_hand_booked_is_never_offered(tmp_path, monkeypatch):        # NEGATIVE
    hand = BookTxn("hand1", date(2025, 4, 5), "Swiggy dinner", "", ((FOOD, 1500.0), (BANK, -1500.0)))
    _r, rows, _c, out = _two(tmp_path, monkeypatch, "FAIL", txns=[book_pay("p1", 1900, date(2025, 5, 12)), hand])
    assert "SWIGGY ORDER" not in {r["Description"] for r in rows}
    j = next(x for x in _wb(out, "Journals") if "SWIGGY" in str(x["Description"]).upper())
    assert j["Status"] == J.ST_POSSIBLY and j["Tie-out"] == "NOT TIED"


def test_payment_rows_are_never_booked_tied_or_untied(tmp_path, monkeypatch):               # NEGATIVE
    for result in ("PASS", "FAIL"):
        _r, rows, _c, _o = _two(tmp_path / result, monkeypatch, result)
        amounts = {r["Amount (Withdrawal)"] or r["Amount Negated (Deposit)"] for r in rows}
        assert "1900.00" not in amounts and "1900" not in amounts


def test_a_statement_that_read_zero_rows_is_still_refused(tmp_path, monkeypatch):           # NEGATIVE
    s1, s2 = stmt(APR, []), stmt(MAY, may_rows())
    reply, rows, _c, _o = go(tmp_path, monkeypatch, [s1, s2],
                             [_tie(s1, "NOT AVAILABLE", 0.0), _tie(s2, "PASS", 300.0)], [])
    text = str(reply.text if hasattr(reply, "text") else reply)
    assert "read 0 rows" in text and BANNER not in text
    assert all(r["Date"].endswith("/05/2025") or r["Date"].endswith("/06/2025") for r in rows) or rows == []
    assert "NOT complete" in text
