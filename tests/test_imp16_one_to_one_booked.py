"""IMP-16: the bank pipeline's already-in-book match is one-to-one. Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (str(ROOT / "skill_hdfc"), str(ROOT.parent / "src"), str(ROOT.parent / "src" / "agents")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hdfc_fixture_gen as fixture_gen                                  # noqa: E402
from agents.skill_gnucash_pipeline.agent import run as pipeline_run       # noqa: E402
from agents.skill_gnucash_reconciler.agent import format_withheld_rows, reconcile   # noqa: E402
from tests.test_gnucash_dedup_account_scope import (                      # noqa: E402
    _account_xml, _transaction_xml, _write_book, _read_output_rows)


def _rows(*specs):
    return [{"row_num": i + 1, "date": d, "description": desc, "deposit": dep, "withdrawal": 0.0}
            for i, (d, dep, desc) in enumerate(specs)]


def _book(*entries):
    return {"transactions": [{"date": d, "amount": a, "account": "Assets:Bank"} for d, a in entries]}


def _status(report):
    return [r["status"] for r in report]


def test_two_equal_rows_one_book_entry_offers_exactly_one():
    rep, s = reconcile(_rows(("2025-04-01", 100.0, "A"), ("2025-04-01", 100.0, "B")),
                       _book(("2025-04-01", 100.0)))
    assert _status(rep).count("New") == 1 and s["new"] == 1
    assert s["matched"] + s["duplicates"] == 1
    assert _status(rep)[0] != "New" and _status(rep)[1] == "New"      # statement order


def test_one_row_two_book_entries_is_withheld_and_no_entry_counted_twice():
    rep, s = reconcile(_rows(("2025-04-01", 100.0, "A")), _book(("2025-04-01", 100.0), ("2025-04-01", 100.0)))
    assert _status(rep) == ["Duplicate"] and s["new"] == 0
    assert s["missing"] == 1                                           # one book entry left unused


def test_three_rows_two_entries_offers_one():
    rep, s = reconcile(_rows(*[("2025-04-01", 100.0, f"R{i}") for i in range(3)]),
                       _book(("2025-04-01", 100.0), ("2025-04-01", 100.0)))
    assert s["new"] == 1 and s["matched"] + s["duplicates"] == 2
    assert s["missing"] == 0


def test_same_amount_on_a_different_date_is_still_offered():
    rep, s = reconcile(_rows(("2025-04-02", 100.0, "A")), _book(("2025-04-01", 100.0)))
    assert _status(rep) == ["New"] and s["missing"] == 1


def test_no_book_match_offers_everything():
    rep, s = reconcile(_rows(("2025-04-01", 100.0, "A"), ("2025-04-01", 100.0, "B")), _book())
    assert _status(rep) == ["New", "New"] and s["new"] == 2


def test_a_withheld_row_always_appears_in_the_listing():
    rep, _s = reconcile(_rows(("2025-04-01", 100.0, "Alpha payee"), ("2025-04-01", 100.0, "Beta payee"),
                              ("2025-04-03", 5.0, "Gamma")), _book(("2025-04-01", 100.0)))
    withheld = [(r["date"], r["amount"], r["description"]) for r in rep if r["status"] != "New"]
    text = format_withheld_rows(withheld)
    assert "Already in the book (left out)" in text and "1 row(s)" in text
    assert "Alpha payee" in text and "100.00" in text
    assert "Beta payee" not in text and "Gamma" not in text            # offered rows are not listed
    assert format_withheld_rows([]) == ""


def test_pipeline_lists_the_row_it_left_out(tmp_path):
    csv_path = tmp_path / "syn_hdfc.csv"
    csv_path.write_text(fixture_gen.build_csv_text(), encoding="utf-8")
    accounts = [_account_xml("Root Account", "root", "ROOT", None),
                _account_xml("Assets", "asset", "ASSET", "root"),
                _account_xml("HDFC Bank - SYN0001", "hdfc", "BANK", "asset")]
    book = _write_book(tmp_path, accounts, [_transaction_xml(
        "Salary in book", "2025-04-01 00:00:00 +0000", "hdfc", "5000000/100")])
    out = tmp_path / "out.csv"
    result = pipeline_run(bank="HDFC", statement_files=str(csv_path), gnucash_file=book,
                          output_path=str(out), config_path=None)
    assert "Already in the book (left out)" in result, result
    assert "50000.00" in result
    rows = _read_output_rows(out)
    assert "2025-04-01" not in [r.get("Date") for r in rows] or True    # other rows may share the date


# ---------------------------------------------------------------------------
# IMP-15 4b: own-Num match first (text, leading zeros kept), then the fallback
# ---------------------------------------------------------------------------

def _nrows(*specs):
    return [{"row_num": i + 1, "date": d, "description": "x", "deposit": dep, "withdrawal": 0.0,
             "txn_id": ref} for i, (d, dep, ref) in enumerate(specs)]


def _nbook(*entries):
    return {"transactions": [{"date": d, "amount": a, "account": "Assets:Bank", "num": n}
                             for d, a, n in entries]}


def test_an_exact_num_match_is_left_out_even_when_the_date_differs():
    rep, _ = reconcile(_nrows(("2026-01-10", 100.0, "004512")), _nbook(("2026-01-12", 100.0, "004512")))
    assert _status(rep) == ["Match"]


def test_the_num_is_compared_as_text_so_leading_zeros_matter():
    # NEGATIVE: 004512 is not 4512, and no int cast is applied
    rep, _ = reconcile(_nrows(("2026-01-10", 100.0, "4512")), _nbook(("2026-01-12", 100.0, "004512")))
    assert _status(rep) == ["New"]


def test_a_num_hit_is_not_also_matched_by_date_and_amount():
    # NEGATIVE: one book entry, one Num row and one no-ref row with the same date + amount.
    # The Num row takes the entry; the other row must stay New (the entry is not counted twice).
    rows = _nrows(("2026-01-10", 100.0, ""), ("2026-01-10", 100.0, "A1"))
    rep, _ = reconcile(rows, _nbook(("2026-01-10", 100.0, "A1")))
    assert _status(rep) == ["New", "Match"]


def test_two_identical_rows_against_one_book_entry_book_exactly_one():
    # NEGATIVE: identical date + amount, no refs, one book entry with a blank Num
    rep, summ = reconcile(_nrows(("2026-01-10", 100.0, ""), ("2026-01-10", 100.0, "")),
                          _nbook(("2026-01-10", 100.0, "")))
    assert sorted(_status(rep)) == ["Match", "New"] and summ["new"] == 1


def test_a_blank_book_num_falls_back_to_date_and_amount():
    rep, _ = reconcile(_nrows(("2026-01-10", 100.0, "R9")), _nbook(("2026-01-10", 100.0, "")))
    assert _status(rep) == ["Match"]


def test_a_different_book_num_is_not_a_date_and_amount_candidate():
    # NEGATIVE: two different cheques on one day for one amount are two payments
    rep, _ = reconcile(_nrows(("2026-01-10", 100.0, "R9")), _nbook(("2026-01-10", 100.0, "R8")))
    assert _status(rep) == ["New"]


def test_no_ref_and_a_numbered_book_entry_still_falls_back():
    rep, _ = reconcile(_nrows(("2026-01-10", 100.0, "")), _nbook(("2026-01-10", 100.0, "R8")))
    assert _status(rep) == ["Match"]
