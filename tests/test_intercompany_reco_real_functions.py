"""
tests/test_intercompany_reco_real_functions.py -- the inter-entity
reconciliation skills, driven through their real functions on synthetic books.

Covers skill_gnucash_intercompany (reconcile_intercompany.reconcile / main,
excel_report.write_workbook) and skill_gnucash_intercompany_matrix
(matrix_recon.run_matrix / main, matrix_report.write_matrix_workbook). Nothing
is faked: the books are small gzip'd GnuCash XML files built by
gnc_book_fixture, and the workbooks are written and read back.

Every person, bank and amount is invented (Alice Rao, Bob Rao, Carol Rao;
filenames carry the owner and the FY, as the skill expects).

Negative tests: a real mismatch IS reported, a matching pair is NOT reported,
and a one-sided entry is NOT silently dropped (it shows in the difference, the
exception list and the workbook).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
_PAIR = ROOT / "src" / "agents" / "skill_gnucash_intercompany" / "scripts"
_MATRIX = ROOT / "src" / "agents" / "skill_gnucash_intercompany_matrix" / "scripts"
for _p in (str(_PAIR), str(_MATRIX), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gnc_book_fixture as fx  # noqa: E402
import reconcile_intercompany as ric  # noqa: E402
import matrix_recon as mr  # noqa: E402


def make_book(tmp_path, stem, contras, txns):
    """contras: {key: (display name, type)}. txns: (date, description,
    bank_paise, contra_key); the contra leg is the opposite of the bank leg."""
    accts = [
        fx.account_xml("root", "Root Account", "ROOT", None),
        fx.account_xml("assets", "Assets", "ASSET", "root", ["placeholder"]),
        fx.account_xml("bank", "Test Bank - 000XXXX1111", "BANK", "assets"),
        fx.account_xml("exp", "Expenses", "EXPENSE", "root", ["placeholder"]),
        fx.account_xml("misc", "Misc", "EXPENSE", "exp"),
    ]
    for key, (name, atype) in contras.items():
        accts.append(fx.account_xml(key, name, atype, "assets"))
    tx = [fx.txn_xml(desc, d, [("bank", bank), (key, -bank)])
          for d, desc, bank, key in txns]
    return fx.write_book(tmp_path / f"{stem}.gnucash", accts, tx)


def alice(tmp_path, txns, extra_contras=None):
    c = {"bob": ("Bob Rao", "ASSET")}
    c.update(extra_contras or {})
    return make_book(tmp_path, "AliceRao2526", c, txns)


def bob(tmp_path, txns, extra_contras=None):
    c = {"alice": ("Alice Rao", "LIABILITY")}
    c.update(extra_contras or {})
    return make_book(tmp_path, "BobRao2526", c, txns)


# Alice lends Bob 10,000.00 (her bank goes out, the Bob account goes up).
A_LEND = ("2025-06-10", "Loan to Bob", -1_000_000, "bob")
B_LEND = ("2025-06-11", "Loan from Alice", 1_000_000, "alice")


def _text(ws):
    return " ".join(str(c.value) for row in ws.iter_rows() for c in row if c.value)


def test_matching_pair_ties_and_reports_nothing(tmp_path):
    a = alice(tmp_path, [A_LEND])
    b = bob(tmp_path, [B_LEND])
    r = ric.reconcile(a, b)
    assert r["book_a"].owner == "Alice Rao" and r["book_b"].owner == "Bob Rao"
    assert len(r["pairs"]) == 1
    assert r["a_exc"] == [] and r["b_exc"] == []
    assert r["difference"] == 0
    out = tmp_path / "ok.xlsx"
    assert ric.main([a, b, str(out)]) == 0
    wb = load_workbook(out)
    assert wb.sheetnames == ["Summary", "Matched", "Exceptions Alice", "Exceptions Bob"]
    text = _text(wb["Summary"])
    assert "TIES OUT" in text and "OUT OF BALANCE" not in text
    assert "Loan to Bob" not in _text(wb["Exceptions Alice"])
    assert "Loan from Alice" not in _text(wb["Exceptions Bob"])
    assert "Loan to Bob" in _text(wb["Matched"])


def test_amount_mismatch_is_reported_everywhere(tmp_path):
    a = alice(tmp_path, [A_LEND])
    b = bob(tmp_path, [("2025-06-11", "Loan from Alice", 1_050_000, "alice")])
    r = ric.reconcile(a, b)
    assert r["difference"] == -500.0
    assert r["pairs"] == []
    assert [m.amount for m in r["a_exc"]] == [10000.0]
    assert [m.amount for m in r["b_exc"]] == [-10500.0]
    out = tmp_path / "bad.xlsx"
    assert ric.main([a, b, str(out)]) == 2
    wb = load_workbook(out)
    text = _text(wb["Summary"])
    assert "OUT OF BALANCE" in text and "TIES OUT" not in text
    assert "Loan to Bob" in _text(wb["Exceptions Alice"])
    assert "Loan from Alice" in _text(wb["Exceptions Bob"])
    assert "Loan to Bob" not in _text(wb["Matched"])


def test_one_sided_entry_is_an_exception_and_moves_the_difference(tmp_path):
    a = alice(tmp_path, [A_LEND, ("2025-08-01", "Second loan to Bob", -250_000, "bob")])
    b = bob(tmp_path, [B_LEND])
    r = ric.reconcile(a, b)
    assert len(r["pairs"]) == 1
    assert [m.desc for m in r["a_exc"]] == ["Second loan to Bob"]
    assert r["b_exc"] == []
    assert r["difference"] == 2500.0
    out = tmp_path / "one_sided.xlsx"
    assert ric.main([a, b, str(out)]) == 2
    assert "Second loan to Bob" in _text(load_workbook(out)["Exceptions Alice"])


def test_one_sided_entry_is_hunted_in_the_other_books_other_accounts(tmp_path):
    """Bob received the money but posted it to a misc account, not to the
    Alice account: the exception carries that posting as a suggestion."""
    a = alice(tmp_path, [A_LEND])
    b = make_book(tmp_path, "BobRao2526", {"alice": ("Alice Rao", "LIABILITY")},
                  [("2025-06-11", "Money in", 1_000_000, "misc")])
    r = ric.reconcile(a, b)
    assert len(r["a_exc"]) == 1
    sugg = r["a_suggestions"][id(r["a_exc"][0])]
    assert sugg and abs(sugg[0].amount) == 10000.0
    out = tmp_path / "hunt.xlsx"
    ric.main([a, b, str(out)])
    assert "Money in" in _text(load_workbook(out)["Exceptions Alice"])


def test_opening_balance_is_carried_and_not_double_counted(tmp_path):
    a = alice(tmp_path, [("2025-01-15", "Old loan to Bob", -500_000, "bob"), A_LEND])
    b = bob(tmp_path, [("2025-01-16", "Old loan from Alice", 500_000, "alice"), B_LEND])
    r = ric.reconcile(a, b)
    assert (r["a_open"], r["b_open"]) == (5000.0, -5000.0)
    assert (r["a_move_sum"], r["b_move_sum"]) == (10000.0, -10000.0)
    assert (r["a_close"], r["b_close"]) == (15000.0, -15000.0)
    assert r["difference"] == 0 and len(r["pairs"]) == 1


def test_opening_balance_difference_is_reported_not_hidden(tmp_path):
    a = alice(tmp_path, [("2025-01-15", "Old loan to Bob", -500_000, "bob"), A_LEND])
    b = bob(tmp_path, [B_LEND])  # Bob never booked the old loan
    r = ric.reconcile(a, b)
    assert r["a_open"] == 5000.0 and r["b_open"] == 0
    assert r["difference"] == 5000.0
    assert ric.main([a, b, str(tmp_path / "o.xlsx")]) == 2


def test_date_tolerance_is_respected_both_ways(tmp_path):
    a = alice(tmp_path, [A_LEND])
    b = bob(tmp_path, [("2025-07-05", "Loan from Alice", 1_000_000, "alice")])
    tight = ric.reconcile(a, b, tol_days=7)
    assert tight["pairs"] == [] and len(tight["a_exc"]) == 1 and len(tight["b_exc"]) == 1
    assert tight["difference"] == 0   # totals agree even though dates do not
    wide = ric.reconcile(a, b, tol_days=30)
    assert len(wide["pairs"]) == 1 and wide["a_exc"] == [] and wide["b_exc"] == []


def test_one_book_entry_is_never_matched_twice(tmp_path):
    a = alice(tmp_path, [A_LEND, ("2025-06-10", "Loan to Bob again", -1_000_000, "bob")])
    b = bob(tmp_path, [B_LEND])
    r = ric.reconcile(a, b)
    assert len(r["pairs"]) == 1
    assert len(r["a_exc"]) == 1 and r["b_exc"] == []
    assert r["difference"] == 10000.0


def test_entries_outside_the_period_are_not_movements(tmp_path):
    a = alice(tmp_path, [A_LEND, ("2026-04-02", "Next year", -100_000, "bob")])
    b = bob(tmp_path, [B_LEND])
    r = ric.reconcile(a, b)
    assert r["a_exc"] == [] and r["a_move_sum"] == 10000.0
    assert r["a_close"] == 10000.0 and r["difference"] == 0


def test_huf_account_is_a_distinct_entity_and_is_not_counted(tmp_path):
    a = alice(tmp_path, [A_LEND, ("2025-09-01", "To Bob HUF", -300_000, "bobhuf")],
              extra_contras={"bobhuf": ("Bob Rao HUF", "ASSET")})
    b = bob(tmp_path, [B_LEND])
    r = ric.reconcile(a, b)
    assert r["a_move_sum"] == 10000.0 and r["difference"] == 0
    assert all("HUF" not in getattr(x, "name", "") for x in r["a_contra"])


def test_missing_contra_account_is_an_error_not_a_zero(tmp_path):
    a = alice(tmp_path, [A_LEND])
    b = make_book(tmp_path, "BobRao2526", {"carol": ("Carol Rao", "LIABILITY")},
                  [("2025-06-11", "x", 1_000_000, "carol")])
    with pytest.raises(ValueError):
        ric.reconcile(a, b)
    assert ric.main([a, b, str(tmp_path / "e.xlsx")]) == 1


def test_a_transfer_between_two_contra_accounts_of_one_book(tmp_path):
    """Probe: a journal whose two legs both sit inside the contra tree. The
    balance must not move, and nothing is double-counted."""
    accts = [
        fx.account_xml("root", "Root Account", "ROOT", None),
        fx.account_xml("assets", "Assets", "ASSET", "root", ["placeholder"]),
        fx.account_xml("bank", "Test Bank - 000XXXX1111", "BANK", "assets"),
        fx.account_xml("bob", "Bob Rao", "ASSET", "assets"),
        fx.account_xml("bob2", "Bob Rao", "ASSET", "bob"),
    ]
    tx = [fx.txn_xml("Loan", "2025-06-10", [("bank", -1_000_000), ("bob", 1_000_000)]),
          fx.txn_xml("Reclass", "2025-07-01", [("bob", -400_000), ("bob2", 400_000)])]
    a = fx.write_book(tmp_path / "AliceRao2526.gnucash", accts, tx)
    b = bob(tmp_path, [B_LEND])
    r = ric.reconcile(a, b)
    assert r["a_close"] == 10000.0 and r["difference"] == 0
    assert round(sum(m.amount for m in r["a_exc"]), 2) == 0


# --------------------------------------------------------------------------- #
# Matrix
# --------------------------------------------------------------------------- #
def _three_books(tmp_path, carol_amount=400_000):
    a = make_book(tmp_path, "AliceRao2526",
                  {"bob": ("Bob Rao", "ASSET"), "carol": ("Carol Rao", "ASSET")},
                  [A_LEND, ("2025-07-01", "Loan to Carol", -400_000, "carol")])
    b = bob(tmp_path, [B_LEND])
    c = make_book(tmp_path, "CarolRao2526", {"alice": ("Alice Rao", "LIABILITY")},
                  [("2025-07-01", "Loan from Alice", carol_amount, "alice")])
    return [a, b, c]


def _pairs(m):
    return {(p["ia"], p["ib"]): p for p in m["pairs"]}


def test_matrix_ties_when_every_reconciled_pair_ties(tmp_path):
    paths = _three_books(tmp_path)
    m = mr.run_matrix(paths)
    by = _pairs(m)
    assert by[(0, 1)]["ok"] and by[(0, 1)]["res"]["difference"] == 0
    assert by[(0, 2)]["ok"] and by[(0, 2)]["res"]["difference"] == 0
    assert by[(1, 2)]["ok"] is False      # no contra: n/a, never a tie
    out = tmp_path / "matrix.xlsx"
    assert mr.main([str(out), *paths]) == 0
    wb = load_workbook(out)
    assert wb.sheetnames == ["Matrix", "Pairs", "All Exceptions"]
    assert "n/a" in _text(wb["Matrix"])
    assert "Out of balance" not in _text(wb["Pairs"])


def test_matrix_reports_a_real_mismatch_in_every_sheet(tmp_path):
    paths = _three_books(tmp_path, carol_amount=450_000)
    m = mr.run_matrix(paths)
    by = _pairs(m)
    assert by[(0, 1)]["res"]["difference"] == 0
    assert by[(0, 2)]["res"]["difference"] == -500.0
    out = tmp_path / "matrix_bad.xlsx"
    assert mr.main([str(out), *paths]) == 2
    wb = load_workbook(out)
    assert "-500" in _text(wb["Matrix"])
    assert "Out of balance" in _text(wb["Pairs"])
    ex = _text(wb["All Exceptions"])
    assert "Loan to Carol" in ex and "Loan from Alice" in ex


def test_matrix_one_sided_entry_is_listed_under_all_exceptions(tmp_path):
    a = alice(tmp_path, [A_LEND, ("2025-08-01", "Unmatched loan", -250_000, "bob")])
    b = bob(tmp_path, [B_LEND])
    out = tmp_path / "matrix_one.xlsx"
    assert mr.main([str(out), a, b]) == 2
    ex = _text(load_workbook(out)["All Exceptions"])
    assert "Unmatched loan" in ex and "Loan to Bob" not in ex


def test_matrix_needs_two_books(tmp_path):
    assert mr.main([str(tmp_path / "o.xlsx"), alice(tmp_path, [A_LEND])]) == 1


def test_matrix_turns_any_pair_error_into_na(tmp_path):
    """Documents current behaviour: an unreadable book is shown as n/a for its
    pair and the run exits 0 when nothing else is wrong."""
    a = alice(tmp_path, [A_LEND])
    bad = tmp_path / "BobRao2526.gnucash"
    bad.write_bytes(b"not a gnucash file")
    m = mr.run_matrix([a, str(bad)])
    assert m["pairs"][0]["ok"] is False
    assert mr.main([str(tmp_path / "o.xlsx"), a, str(bad)]) == 0
