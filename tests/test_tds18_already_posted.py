"""TDS-18: the 26AS journal recognises journals already in the book. A re-run
leaves them out of the import file, says so, and offers no CSV when nothing is
new. Synthetic accounts, deductors and books only."""
from __future__ import annotations

import builtins
import csv
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_tds_journal_fixes import m, read_csv, write_26as
from tests.test_tds15_net_interest import (
    BOOK_SPECS, GENERIC_FD, ZEN_INC, book_with_txns)

Z1 = (1, "ZENITH BANK", "194A", 10000.0, 1000.0)
Z2 = (2, "ZENITH BANK", "194A", 4000.0, 400.0)
ID1 = "2526-TDSJ01"
ID2 = "2526-TDSJ02"


@pytest.fixture(autouse=True)
def _tmp_known(tmp_path, monkeypatch):
    from ui import _safe_paths
    monkeypatch.setattr(_safe_paths, "known_folders", lambda: [tmp_path.resolve()])


def _book(path, txns=()):
    """txns: [(num, [(account, value), ...])] posted into the book with a Num."""
    plain = book_with_txns(path, BOOK_SPECS, [t for _n, t in txns])
    text = Path(plain).read_text(encoding="utf-8")
    # insert each Num on its own transaction, in order
    parts = text.split("<gnc:transaction><trn:splits>")
    out = parts[0]
    for (num, _t), rest in zip(txns, parts[1:]):
        out += f"<gnc:transaction><trn:num>{num}</trn:num><trn:splits>" + rest
    Path(path).write_text(out, encoding="utf-8")
    return Path(path)


def _rows_by_id(csv_path):
    by = {}
    for r in read_csv(csv_path):
        by.setdefault(r["Transaction ID"], []).append((r["Account"], float(r["Amount"])))
    return by


def _first_run(tmp_path, rows):
    x = write_26as(tmp_path / "x.xlsx", {"Part I": rows})
    book = _book(tmp_path / "empty.gnucash")
    out = tmp_path / "first.csv"
    m.run(x, book, out)
    return x, _rows_by_id(out)


def test_everything_posted_means_nothing_to_import_and_no_csv(tmp_path):
    x, by = _first_run(tmp_path, [Z1])
    book = _book(tmp_path / "b.gnucash", [(ID1, by[ID1])])
    out = tmp_path / "out.csv"
    stats = m.run(x, book, out)
    assert stats["nothing_to_import"] is True
    assert stats["already_posted"] == [ID1]
    assert not out.exists()                                           # NEGATIVE: no CSV written
    assert not m.part_i_path_for(out).exists()


def test_a_stale_csv_from_an_earlier_run_is_not_left_to_be_offered(tmp_path):
    x, by = _first_run(tmp_path, [Z1])
    book = _book(tmp_path / "b.gnucash", [(ID1, by[ID1])])
    out = tmp_path / "out.csv"
    out.write_text("stale", encoding="utf-8")
    m.run(x, book, out)
    assert not out.exists()


def test_an_unposted_journal_is_still_offered_and_a_posted_one_is_left_out(tmp_path):
    x, by = _first_run(tmp_path, [Z1, Z2])
    book = _book(tmp_path / "b.gnucash", [(ID1, by[ID1])])
    out = tmp_path / "out.csv"
    stats = m.run(x, book, out)
    got = _rows_by_id(out)
    assert set(got) == {ID2}                                          # ID1 left out, ID2 offered
    assert ID1 not in got                                             # NEGATIVE
    assert stats["nothing_to_import"] is False and stats["already_posted"] == [ID1]


def test_nothing_posted_offers_everything_as_before(tmp_path):
    x, by = _first_run(tmp_path, [Z1, Z2])
    out = tmp_path / "out.csv"
    stats = m.run(x, _book(tmp_path / "b.gnucash"), out)
    assert set(_rows_by_id(out)) == {ID1, ID2}
    assert stats["already_posted"] == [] and stats["posted_mismatches"] == []


def test_same_num_with_different_amounts_is_a_mismatch_not_posted(tmp_path):
    x, by = _first_run(tmp_path, [Z1])
    changed = [(a, v + 1.0 if v > 0 else v) for a, v in by[ID1]]
    changed[-1] = (changed[-1][0], changed[-1][1] - 1.0)
    book = _book(tmp_path / "b.gnucash", [(ID1, changed)])
    out = tmp_path / "out.csv"
    stats = m.run(x, book, out)
    assert [i["id"] for i in stats["posted_mismatches"]] == [ID1]
    assert stats["already_posted"] == []                              # NEGATIVE: not "posted"
    assert stats["nothing_to_import"] is False or not out.exists()
    # and the summary raises it loudly
    from agents.skill_26as_journal import tools as tl
    assert tl.final_summary(str(out), str(book)).startswith("**RED FLAG")


def test_a_genuine_debit_balance_on_an_unposted_journal_still_raises_the_red_flag(tmp_path):
    x, by = _first_run(tmp_path, [Z1, Z2])
    # ID1 is posted; the book also holds a small credit on the generic account,
    # so the UNPOSTED ID2 (Dr generic 3600) pushes it to a debit balance.
    book = _book(tmp_path / "b.gnucash", [
        (ID1, by[ID1]),
        ("OTHER-1", [("Assets:Bank", 100.0), (GENERIC_FD, -100.0)])])
    stats = m.run(x, book, tmp_path / "out.csv")
    assert [i["account"] for i in stats["preflight_issues"]] == [GENERIC_FD]
    assert {r[0] for r in stats["preflight_issues"][0]["rows"]} == {ID2}


def test_a_debit_balance_that_comes_only_from_a_posted_journal_is_not_a_red_flag(tmp_path):
    x, by = _first_run(tmp_path, [Z1])
    # the posted ID1 already left the generic account on a debit balance
    book = _book(tmp_path / "b.gnucash", [(ID1, by[ID1])])
    out = tmp_path / "out.csv"
    stats = m.run(x, book, out)
    assert stats["preflight_issues"] == []
    from agents.skill_26as_journal import tools as tl
    text = tl.final_summary(str(out), str(book))
    assert "RED FLAG" not in text and "Nothing to import" in text


def test_main_says_nothing_to_import_and_prints_no_saved_line(tmp_path, capsys):
    x, by = _first_run(tmp_path, [Z1])
    book = _book(tmp_path / "b.gnucash", [(ID1, by[ID1])])
    m.main(["build_tds_journals.py", str(x), str(book), str(tmp_path / "out.csv")])
    out = capsys.readouterr().out
    assert "Nothing to import" in out and "ALREADY POSTED" in out
    assert "Saved:" not in out


def test_the_book_is_never_opened_for_write(tmp_path, monkeypatch):
    x, by = _first_run(tmp_path, [Z1])
    book = _book(tmp_path / "b.gnucash", [(ID1, by[ID1])])
    before = book.read_bytes()
    real_open, real_path_open = builtins.open, Path.open
    writes = []

    def spy(target, mode="r", *a, **k):
        if Path(str(target)) == book and any(c in mode for c in "wax+"):
            writes.append(mode)
        return real_open(target, mode, *a, **k)

    def spy_path(self, mode="r", *a, **k):
        if self == book and any(c in mode for c in "wax+"):
            writes.append(mode)
        return real_path_open(self, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", spy)
    monkeypatch.setattr(Path, "open", spy_path)
    m.run(x, book, tmp_path / "out.csv")
    assert writes == [] and book.read_bytes() == before


def test_an_unreadable_book_is_reported_and_nothing_is_filtered(tmp_path, monkeypatch):
    x, by = _first_run(tmp_path, [Z1])
    book = _book(tmp_path / "b.gnucash", [(ID1, by[ID1])])

    def boom(_p):
        raise ValueError("unreadable")

    monkeypatch.setattr(m, "load_book_journals_by_num", boom)
    out = tmp_path / "out.csv"
    stats = m.run(x, book, out)
    assert "unreadable" in stats["posted_check_error"]
    assert stats["nothing_to_import"] is False                        # NEGATIVE: never "clean"
    assert ID1 in _rows_by_id(out)


def test_mismatch_plus_all_others_posted_never_says_every_journal_is_in_book(tmp_path, capsys):
    from agents.skill_26as_journal import tools as tl
    x, by = _first_run(tmp_path, [Z1, Z2])
    changed = [(a, v + 1.0 if v > 0 else v) for a, v in by[ID2]]
    changed[-1] = (changed[-1][0], changed[-1][1] - 1.0)
    book = _book(tmp_path / "b.gnucash", [(ID1, by[ID1]), (ID2, changed)])
    out = tmp_path / "out.csv"
    stats = m.run(x, book, out)
    assert stats["nothing_to_import"] is True
    assert not out.exists()                                           # no CSV
    summ = tl.final_summary(str(out), str(book))
    assert summ.startswith("**RED FLAG")
    assert "every journal is already in the book" not in summ
    assert "1 already posted; 1 left out" in summ
    # NEGATIVE control: no mismatch -> the plain wording is still used
    book2 = _book(tmp_path / "b2.gnucash", [(ID1, by[ID1]), (ID2, by[ID2])])
    out2 = tmp_path / "out2.csv"
    m.run(x, book2, out2)
    assert "every journal is already in the book" in tl.final_summary(str(out2), str(book2))


# ---- IN BOOK, DIFFERENT ACCOUNT vs DIFFERENT AMOUNTS ----

def _rows(tid, legs):
    return [{"Transaction ID": tid, "Account": a, "Amount": str(v)} for a, v in legs]


def test_same_amounts_different_account_is_its_own_status_and_names_accounts():
    run_legs = [("Assets:FD:Zen", 900.0), ("Assets:Tax", 100.0), ("Income:Zen", -1000.0)]
    book_legs = {"Income:Interest": 900.0, "Assets:Tax": 100.0, "Income:Zen": -1000.0}
    st = m.classify_posted(_rows("J1", run_legs), {"J1": [book_legs]})
    assert st["J1"][0] == m.POSTED_ACCOUNT_MISMATCH
    flag = m.mismatch_red_flag({"id": "J1", "detail": st["J1"][1], "kind": st["J1"][0]})
    assert flag.startswith("RED FLAG - J1")
    assert "Income:Interest" in flag and "Assets:FD:Zen" in flag
    assert "same amounts" in flag and "different amounts" not in flag     # NEGATIVE


def test_one_amount_differs_is_different_amounts_never_different_account():
    run_legs = [("A", 900.0), ("B", -900.0)]
    st = m.classify_posted(_rows("J1", run_legs), {"J1": [{"A": 800.0, "B": -800.0}]})
    assert st["J1"][0] == m.POSTED_MISMATCH
    flag = m.mismatch_red_flag({"id": "J1", "detail": st["J1"][1], "kind": st["J1"][0]})
    assert "different amounts" in flag and "different account" not in flag.lower()


def test_same_accounts_and_amounts_is_still_already_posted():
    st = m.classify_posted(_rows("J1", [("A", 5.0), ("B", -5.0)]), {"J1": [{"A": 5.0, "B": -5.0}]})
    assert st["J1"][0] == m.POSTED


def test_ambiguous_account_pairing_lists_both_sides():
    run_legs = [("X1", 50.0), ("X2", 50.0), ("Y", -100.0)]
    st = m.classify_posted(_rows("J1", run_legs), {"J1": [{"P1": 50.0, "P2": 50.0, "Y": -100.0}]})
    assert st["J1"][0] == m.POSTED_ACCOUNT_MISMATCH
    assert "P1" in st["J1"][1] and "X2" in st["J1"][1]


def test_different_account_journal_is_never_posted_and_never_reoffered(tmp_path):
    from agents.skill_26as_journal import tools as tl
    x, by = _first_run(tmp_path, [Z1])
    legs = list(by[ID1])
    swap = next(a for a, _v in legs if a not in ("Assets:Bank",))
    other = "Assets:Bank"
    assert other not in {a for a, _ in legs} or True
    book_legs = [(other if a == swap else a, v) for a, v in legs]
    book = _book(tmp_path / "b.gnucash", [(ID1, book_legs)])
    out = tmp_path / "out.csv"
    stats = m.run(x, book, out)
    assert [i["kind"] for i in stats["posted_mismatches"]] in (
        [m.POSTED_ACCOUNT_MISMATCH], [m.POSTED_MISMATCH])
    assert stats["already_posted"] == []                              # NEGATIVE
    assert not out.exists()                                           # never re-offered
    summ = tl.final_summary(str(out), str(book))
    assert summ.startswith("**RED FLAG")
    assert "every journal is already in the book" not in summ
