"""DBT-01: the balance self-check row on BS_Transcript is written again. The
Reconciliation sheet is unchanged: its existing "Identity checks" line already
covers an unbalanced book. Synthetic Balance Sheet reports only,
built from a small double-entry ledger so the totals follow the report's own
sign conventions (liabilities and equity positive, Retained Earnings = net income).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

import fixture_gen as fg  # noqa: E402
import parse_eguile  # noqa: E402
import write_workbook as ww  # noqa: E402

LABEL = "Assets - Equity - NetIncome (self-check, should be 0)"
CHECK_LABEL_START = "Balance sheet self-check"  # must NOT appear on Reconciliation


def _html(*, assets: float, liabilities: float, trading: float, equity: float,
          income: float, expense: float) -> str:
    """A Balance Sheet (eguile) report for a synthetic ledger. `expense` is a
    positive spend; the report prints expenses negative inside Retained Earnings."""
    g = lambda s: fg._guid(f"DBT01:{s}")  # noqa: E731
    re_total = income - expense
    body = [
        fg._section_header("Assets Accounts"),
        fg._account_row(0, g("Assets"), "Assets", None, 4),
        fg._account_row(1, g("Bank"), "Bank", assets, 3),
        fg._section_total("Total Assets Accounts", assets),
        fg._spacer(),
        fg._section_header("Liability Accounts"),
        fg._account_row(0, g("Liabilities"), "Liabilities", None, 4),
        fg._account_row(1, g("Loan"), "Loan", liabilities, 3),
        fg._section_total("Total Liability Accounts", liabilities),
        fg._spacer(),
        fg._section_header("Trading Accounts"),
        fg._account_row(0, g("Trading"), "Trading", None, 4),
        fg._account_row(1, g("Currency"), "Currency", trading, 3),
        fg._section_total("Total Trading Accounts", trading),
        fg._spacer(),
        fg._section_header("Equity Accounts"),
        fg._account_row(0, g("Equity"), "Equity", None, 4),
        fg._account_row(1, g("Capital"), "Capital", equity, 3),
        fg._section_total("Total Equity Accounts", equity),
        fg._spacer(),
        fg._section_total("Total Equity, Trading, and Liabilities", liabilities + trading + equity),
        fg._spacer(),
        fg._section_header("Retained Earnings"),
        fg._account_row(0, g("Income"), "Income", None, 4),
        fg._account_row(1, g("Fees"), "Fees", income, 3),
        fg._total_row(0, g("Income"), "Income", income, 4, section_style=True),
        fg._account_row(0, g("Expense"), "Expense", None, 4),
        fg._account_row(1, g("Rent"), "Rent", -expense, 3, negative_class=True),
        fg._total_row(0, g("Expense"), "Expense", -expense, 4, negative_class=True, section_style=True),
        fg._section_total("Total Retained Earnings", re_total),
        fg._spacer(),
        fg._section_total("Imbalance Amount", 0.00),
    ]
    return fg._wrap("DBT01 Balance Sheet (eguile) 31-03-2025", "\n".join(body))


# Balanced ledger with a non-zero net income, a loan and a trading balance:
# 1,000 = (150 + 25 + 500) + (400 - 75)
BALANCED = dict(assets=1000.00, liabilities=150.00, trading=25.00, equity=500.00,
                income=400.00, expense=75.00)
# The same ledger with the assets 7.25 short.
UNBALANCED = dict(BALANCED, assets=992.75)


def _tree(**kw):
    return parse_eguile.parse_html(_html(**kw))


def _sheet_rows(ws):
    return [(r[0], r[1]) for r in ws.iter_rows(min_col=1, max_col=2, values_only=True) if r[0] is not None]


def _bs_sheet(tree):
    wb = Workbook()
    entries = ww.write_bs_transcript(wb, tree)
    return wb["BS_Transcript"], entries


def _recon_text(tree, identity_failures=()):
    wb = Workbook()
    ww.write_reconciliation_sheet(
        wb, tree, list(identity_failures), [], [], [], "v1", "v1", "2026-01-01T00:00:00", {}, True)
    return {str(a): b for a, b in _sheet_rows(wb["Reconciliation"])}


# ---- the formula holds for a correctly balanced book --------------------------

def test_the_three_section_totals_of_a_balanced_book_net_to_zero():
    tree = _tree(**BALANCED)
    assert tree.section_totals["Assets Accounts"] == 1000.00
    assert tree.section_totals["Equity, Trading, and Liabilities"] == 675.00
    assert tree.section_totals["Retained Earnings"] == 325.00
    assert ww.balance_self_check_difference(tree) == pytest.approx(0.0, abs=1e-9)
    assert parse_eguile.verify(tree) == []          # the parser's own identity agrees


# ---- the row is written ------------------------------------------------------

def test_balanced_book_writes_the_row_with_zero():
    ws, _ = _bs_sheet(_tree(**BALANCED))
    rows = dict(_sheet_rows(ws))
    assert LABEL in rows                              # NEGATIVE of the old bug: the row exists
    assert rows[LABEL] == pytest.approx(0.0, abs=1e-9)


def test_unbalanced_book_shows_the_difference_on_the_row():             # NEGATIVE
    ws, _ = _bs_sheet(_tree(**UNBALANCED))
    rows = dict(_sheet_rows(ws))
    assert rows[LABEL] == pytest.approx(-7.25)
    assert rows[LABEL] != 0


def test_the_row_is_in_the_function_not_after_a_return():
    """A row after `return` is never written; this is the guard that it cannot
    quietly go dead again."""
    import ast
    src = (ROOT / "src/agents/skill_itr_workbook/scripts/write_workbook.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "write_bs_transcript")
    seen_return = False
    for stmt in fn.body:
        if seen_return:
            pytest.fail("code after the return in write_bs_transcript is never run")
        if isinstance(stmt, ast.Return):
            seen_return = True


# ---- the other cell references are unchanged --------------------------------

def test_entry_references_point_at_their_own_rows_and_follow_the_leaves():
    tree = _tree(**BALANCED)
    ws, entries = _bs_sheet(tree)
    leaves = [n for n in tree.all_nodes()
              if not n.section.startswith("RetainedEarnings") and not n.children]
    assert [p for p, _ in entries] == [n.path for n in leaves]
    # header rows: title (row 1), column heads (row 2); leaves start at row 3, one per row
    assert [ref for _, ref in entries] == [f"B{3 + i}" for i in range(len(leaves))]
    for (path, ref), n in zip(entries, leaves):
        row = int(ref[1:])
        assert ws.cell(row=row, column=1).value == path
        assert ws[ref].value == n.total
    # the self-check row comes after the last entry, never inside them
    label_row = next(r for r in range(1, ws.max_row + 1) if ws.cell(row=r, column=1).value == LABEL)
    assert label_row > int(entries[-1][1][1:])


def test_entries_are_the_same_with_and_without_a_balanced_book():
    a = _bs_sheet(_tree(**BALANCED))[1]
    b = _bs_sheet(_tree(**UNBALANCED))[1]
    assert [r for _, r in a] == [r for _, r in b]


# ---- the Reconciliation sheet is not touched ---------------------------------

@pytest.mark.parametrize("book", [BALANCED, UNBALANCED])
def test_reconciliation_has_no_self_check_line(book):                   # NEGATIVE
    text = _recon_text(_tree(**book))
    assert not [k for k in text if k.startswith(CHECK_LABEL_START)]


def test_identity_checks_already_flags_an_unbalanced_book():
    tree = _tree(**UNBALANCED)
    failures = parse_eguile.verify(tree)
    assert failures
    text = _recon_text(tree, failures)
    assert text["Identity checks"] != "OK"
    assert text["Identity checks"] == f"{len(failures)} FAILURE(S)"
    assert any(k.strip() == "failure" for k in text)


def test_the_existing_reconciliation_lines_are_still_there():
    text = _recon_text(_tree(**BALANCED))
    assert text["Identity checks"] == "OK"
    assert "Capital Gains reconciliation (lots vs control)" in text
    assert text["Imbalance Amount"] == 0.0
