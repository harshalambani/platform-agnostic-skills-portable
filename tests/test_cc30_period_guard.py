"""tests/test_cc30_period_guard.py -- CC-30: a statement whose period is not read must never be included
silently in a ranged run; the older SBI "Statement dated" line is read; fee reversals pair with the latest
earlier fee. Synthetic data only."""
from __future__ import annotations

from datetime import date, datetime

from agents.skill_cc_sort import completeness as C
from agents.skill_cc_spend_booking import journal as J
from tests.test_cc05_sbi_statements import BLOCK, LEGEND, _head, m
from tests.test_cc_spend_booking import APR, MOD, go, row, stmt, tie

PERIOD_LINE = "for Statement Period: 05 Mar 26 to 04 Apr 26"
NO_PERIOD = [b for b in BLOCK if "Statement Period" not in b]


def _lines(extra=()):
    return _head() + list(extra) + NO_PERIOD + LEGEND


# ---- part 1: the SBI "Statement dated" line ---------------------------------------------------

def test_old_sbi_line_gives_a_date_only_period():
    info = C.classify_text("Card Statement\nfor Statement dated 20 Mar 2025\nPayments")
    assert info is not None and info.kind == "date" and info.end == date(2025, 3, 20) and info.pattern == "SBI-old"
    assert info.start is None
    per, note = m.detect_period(["for Statement dated 20 Mar 2025"])
    assert per == (date(2025, 2, 21), date(2025, 3, 20)) and note == "statement date only"


def test_a_printed_period_wins_over_the_statement_dated_line():
    text = "for Statement dated 20 Apr 2025\n" + PERIOD_LINE
    info = C.classify_text(text)
    assert info.kind == "period" and info.start == date(2026, 3, 5) and info.end == date(2026, 4, 4)


def test_a_bare_statement_date_header_does_not_match():                          # NEGATIVE
    assert C.classify_text("Statement Date\n1,000.00 2,000.00 20 Mar 2025") is None
    assert C.classify_text("Restatement dated 20 Mar 2025") is None             # word-bounded


def test_existing_banks_are_unchanged():                                        # NEGATIVE
    assert C.classify_text("Statement Date:20/03/2025").pattern == "HDFC-old"
    assert C.classify_text("Statement Date : 20-Mar-2025").pattern == "SBM"
    assert C.classify_text(PERIOD_LINE).pattern == "SBI"


# ---- part 2: the guard ------------------------------------------------------------------------

def _run(tmp_path, monkeypatch, lines, start, end):
    d = tmp_path / "in" / "SBI-BPCL-Octane"
    d.mkdir(parents=True, exist_ok=True)
    (d / "a.pdf").write_bytes(b"%PDF-1.4 synthetic")
    monkeypatch.setattr(m, "read_lines", lambda p: lines)
    return m.run_extraction(tmp_path / "in", tmp_path / "o.xlsx", start, end, "x")


def test_a_no_period_statement_is_held_in_a_ranged_run(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, _lines(), date(2026, 1, 1), date(2026, 12, 31))
    assert res.rows == [] and res.statements == [] and res.ties == []
    assert len(res.held) == 1 and res.held[0][0] == "a.pdf" and "period not found" in res.held[0][2]
    assert res.message.startswith("STATEMENT PERIOD NOT FOUND - NOT INCLUDED")
    assert any("not included" in i for i in res.issues)


def test_an_unranged_run_still_includes_it(tmp_path, monkeypatch):                # NEGATIVE
    res = _run(tmp_path, monkeypatch, _lines(), None, None)
    assert len(res.rows) == 4 and res.held == [] and len(res.statements) == 1


def test_a_statement_with_a_period_is_not_held(tmp_path, monkeypatch):            # NEGATIVE
    res = _run(tmp_path, monkeypatch, _head() + BLOCK + LEGEND, date(2026, 1, 1), date(2026, 12, 31))
    assert res.held == [] and len(res.rows) == 4


def test_the_old_sbi_line_makes_the_statement_a_normal_ranged_one(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, _lines(["for Statement dated 04 Apr 2026"]),
               date(2026, 1, 1), date(2026, 12, 31))
    assert res.held == [] and len(res.rows) == 4
    res2 = _run(tmp_path / "x", monkeypatch, _lines(["for Statement dated 04 Apr 2025"]),
                date(2026, 1, 1), date(2026, 12, 31))
    assert res2.rows == [] and res2.held == []                                    # read as FY24-25: outside, not held


def test_book_spends_books_nothing_from_a_held_statement(tmp_path, monkeypatch):
    s1 = stmt(APR, [row("SWIGGY ORDER", 150, (4, 5))])
    held = [("z.pdf", "Regalia", "statement period not found - not included")]
    res = MOD.Result(message="", issues=["z.pdf: statement period not found - not included"], rows=[],
                     ties=[], fees=[], duplicates=[], statements=[], pool=[], held=held)
    reply, rows, _c, _ = go(tmp_path, monkeypatch, [], [], [], result_override=res)
    text = reply.text if hasattr(reply, "text") else str(reply)
    assert text.startswith("STATEMENT PERIOD NOT FOUND - NOT INCLUDED")
    assert "z.pdf" in text and "NOT complete" in text and "completed successfully" not in text
    assert rows == []
    assert s1 is not None


def test_book_spends_with_nothing_held_has_no_block(tmp_path, monkeypatch):       # NEGATIVE
    s1 = stmt(APR, [row("SWIGGY ORDER", 150, (4, 5))])
    reply, _r, _c, _ = go(tmp_path, monkeypatch, [s1], [tie(s1, printed=150.0)], [])
    text = reply.text if hasattr(reply, "text") else str(reply)
    assert "PERIOD NOT FOUND" not in text


# ---- part 3: no date filter on PDF rows -------------------------------------------------------

def test_an_in_range_statement_keeps_rows_dated_before_the_range(tmp_path, monkeypatch):
    block = ["for Statement Period: 21 Mar 26 to 20 Apr 26",
             "25 Mar 26 EXAMPLE FUEL STATION CITY 250.00 D",
             "05 Apr 26 EXAMPLE SHOP CITY 100.00 D"]
    lines = _head(prev="0.00", cred="0.00", deb="350.00", fee="0.00", total="350.00") + block + LEGEND
    res = _run(tmp_path, monkeypatch, lines, date(2026, 4, 1), date(2026, 4, 30))
    assert res.held == [] and len(res.rows) == 2
    assert min(r.date for r in res.rows) == datetime(2026, 3, 25)


# ---- part 4: fee reversal pairing -------------------------------------------------------------

def _fee(d, y):
    return MOD.Row("HDFC", "Regalia", datetime(y, *d), "ANNUAL FEE", 500.0, "Dr", "fee")


def _rev(d, y):
    return MOD.Row("HDFC", "Regalia", datetime(y, *d), "ANNUAL FEE REVERSAL", 500.0, "Cr", "refund")


def test_collect_fees_pairs_the_reversal_with_the_latest_earlier_fee():
    f1, f2, rv = _fee((4, 10), 2024), _fee((4, 10), 2025), _rev((4, 20), 2025)
    fees = MOD.collect_fees([stmt(APR, [f1, f2, rv])])
    by_year = {f.row.date.year: f for f in fees}
    assert by_year[2025].reversed_by is rv
    assert by_year[2024].reversed_by is None                                      # NEGATIVE: last year's fee stays


def test_collect_fees_a_reversal_before_a_fee_does_not_pair():                    # NEGATIVE
    f, rv = _fee((4, 10), 2025), _rev((4, 5), 2025)
    assert MOD.collect_fees([stmt(APR, [f, rv])])[0].reversed_by is None


def _jn(kind, when, num):
    return J.Journal(num=num, kind=kind, date=when, real_date=when, description="ANNUAL FEE", amount=500.0,
                     side="D", card="Regalia", statement="s", source="s.pdf")


def test_pair_fee_reversals_uses_the_latest_earlier_fee():
    f1, f2 = _jn(J.K_FEE, date(2024, 4, 10), "a"), _jn(J.K_FEE, date(2025, 4, 10), "b")
    rv = _jn(J.K_FEE_REV, date(2025, 4, 20), "c")
    J.pair_fee_reversals([f1, f2, rv])
    assert "10 Apr 2025" in rv.paired_with and "2024" not in rv.paired_with
    assert not f1.paired_with                                                     # NEGATIVE: the earlier fee is untouched
    rv2 = _jn(J.K_FEE_REV, date(2025, 4, 25), "d")                                # one-to-one: the next takes the earlier fee
    J.pair_fee_reversals([f1, f2, rv, rv2])
    assert "10 Apr 2025" in rv.paired_with and "10 Apr 2024" in rv2.paired_with
