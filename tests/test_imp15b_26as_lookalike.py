"""IMP-15 4b (26AS): a journal whose Num is not in the book but whose date and
amounts match a book transaction is KEPT in the import file and flagged
"check before import". Never withheld. Synthetic accounts and books only."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.test_tds_journal_fixes import m, read_csv, write_26as
from tests.test_tds18_already_posted import (
    ID1, ID2, Z1, Z2, _book, _first_run, _rows_by_id)  # noqa: F401


@pytest.fixture(autouse=True)
def _tmp_known(tmp_path, monkeypatch):
    from ui import _safe_paths
    monkeypatch.setattr(_safe_paths, "known_folders", lambda: [tmp_path.resolve()])


def _dated(path: Path, dates) -> Path:
    """Give each transaction of a synthetic book a posted date, in order."""
    text = path.read_text(encoding="utf-8")
    text = text.replace("<gnc-v2 ", '<gnc-v2 xmlns:ts="http://www.gnucash.org/XML/ts" ', 1)
    it = iter(dates)
    text = re.sub(
        r"(<gnc:transaction>)((?:<trn:num>[^<]*</trn:num>)?<trn:splits>)",
        lambda mo: (mo.group(1) + "<trn:date-posted><ts:date>%s 00:00:00 +0000</ts:date>"
                    "</trn:date-posted>" % next(it) + mo.group(2)),
        text)
    path.write_text(text, encoding="utf-8")
    return path


def _hand_booked(tmp_path, by, tid, date, name="b.gnucash"):
    """The same journal booked by hand: no Num, same date and accounts."""
    book = _book(tmp_path / name, [("", by[tid])])
    return _dated(book, [date])


def test_same_date_and_amounts_without_the_num_is_kept_and_flagged(tmp_path):
    x, by = _first_run(tmp_path, [Z1])
    book = _hand_booked(tmp_path, by, ID1, m.journal_date())
    out = tmp_path / "out.csv"
    stats = m.run(x, book, out)
    assert [i["id"] for i in stats["possibly_posted"]] == [ID1]
    assert set(_rows_by_id(out)) == {ID1}                    # NEGATIVE: not withheld
    assert stats["already_posted"] == [] and stats["nothing_to_import"] is False
    assert "check before import" in m.lookalike_text(stats["possibly_posted"])


def test_the_flag_reaches_the_summary_and_never_shows_an_amount(tmp_path):
    x, by = _first_run(tmp_path, [Z1])
    book = _hand_booked(tmp_path, by, ID1, m.journal_date())
    out = tmp_path / "out.csv"
    m.run(x, book, out)
    from agents.skill_26as_journal import tools as tl
    s = tl.final_summary(str(out), str(book))
    assert "POSSIBLY ALREADY IN THE BOOK" in s and ID1 in s
    assert "10000" not in m.lookalike_text(tl.posted_info(str(out))["possibly_posted"])


def test_a_different_date_is_not_flagged(tmp_path):
    x, by = _first_run(tmp_path, [Z1])
    book = _hand_booked(tmp_path, by, ID1, "2001-01-01")
    stats = m.run(x, book, tmp_path / "out.csv")
    assert stats["possibly_posted"] == []                    # NEGATIVE: wrong date
    assert set(_rows_by_id(tmp_path / "out.csv")) == {ID1}


def test_different_amounts_are_not_flagged(tmp_path):
    x, by = _first_run(tmp_path, [Z1])
    other = [(a, v * 2) for a, v in by[ID1]]
    book = _dated(_book(tmp_path / "b.gnucash", [("", other)]), [m.journal_date()])
    stats = m.run(x, book, tmp_path / "out.csv")
    assert stats["possibly_posted"] == []                    # NEGATIVE: wrong amounts


def test_a_num_hit_is_not_also_flagged_as_a_lookalike(tmp_path):
    x, by = _first_run(tmp_path, [Z1])
    book = _dated(_book(tmp_path / "b.gnucash", [(ID1, by[ID1])]), [m.journal_date()])
    stats = m.run(x, book, tmp_path / "out.csv")
    assert stats["already_posted"] == [ID1]
    assert stats["possibly_posted"] == []                    # NEGATIVE: claimed by Num once


def test_two_look_alikes_for_one_book_entry_flag_one_only():
    # two identical journals cannot both be the one hand-booked entry
    from agents import book_check as bc
    d = "2026-03-31"
    rows = [{"Transaction ID": t, "Date": d, "Account": a, "Amount": v}
            for t in ("J1", "J2") for a, v in (("A", "10.00"), ("B", "-10.00"))]
    txn = bc.BookTxn("0", "", __import__("datetime").date(2026, 3, 31),
                     (("A", 1000), ("B", -1000)))
    status = {"J1": (m.NOT_POSTED, ""), "J2": (m.NOT_POSTED, "")}
    got = m.lookalike_journals(rows, status, [txn])
    assert len(got) == 1 and got[0]["id"] == "J1"            # NEGATIVE: not both flagged
    assert m.lookalike_journals(rows, status, []) == []


def test_an_unreadable_book_still_withholds_nothing(tmp_path, monkeypatch):
    x, _by = _first_run(tmp_path, [Z1, Z2])
    book = _book(tmp_path / "b.gnucash")

    def boom(_p):
        raise OSError("cannot read")
    monkeypatch.setattr(m, "load_book_txns", boom)
    out = tmp_path / "out.csv"
    stats = m.run(x, book, out)
    assert set(_rows_by_id(out)) == {ID1, ID2}               # NEGATIVE: all written
    assert stats["posted_check_error"] and stats["nothing_to_import"] is False
