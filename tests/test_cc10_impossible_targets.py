"""CC-10: a card spend is never READY on an account it can never hit (an income
account; an asset account reached by anything but a saved user rule). Synthetic
data only; the book, statements and mapper are fakes."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests import test_cc_spend_booking as T                     # noqa: E402
from tests.test_cc_spend_booking import APR, MAY, Guard, book_pay, go, may_rows, row, stmt, tie   # noqa: E402

INC = "Income:Dividend - Shares:Synthetic Co"
BIZ_EXP = "Income:xBusiness Income:Business Expenses:Travel"
LAND = "Assets:Fixed Assets:Synthetic Plot"
SUSP = "Liabilities:Suspense"


def _run(tmp_path, monkeypatch, rows, mapping, types):
    monkeypatch.setattr(T, "MAP_OVERRIDE", mapping)
    monkeypatch.setattr(Guard, "types", types)
    monkeypatch.setattr(T, "PATHS", T.PATHS | {INC, BIZ_EXP, LAND, SUSP})
    s1 = stmt(APR, rows)
    total = sum(r.amount if r.direction == "Dr" else -r.amount for r in rows)
    s2 = stmt(MAY, may_rows(pay=total))
    return go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=total), tie(s2, printed=300.0)],
              [book_pay("p1", total, date(2025, 5, 12))])


def _accounts(rows):
    return {r["Description"]: r["Account"] for r in rows}


def test_history_matched_spend_to_an_income_account_is_never_ready(tmp_path, monkeypatch):     # NEGATIVE
    reply, rows, _c, _o = _run(tmp_path, monkeypatch, [row("SHARE CO TOKEN", 700, (4, 5))],
                               {"SHARE": (INC, "history", "History: token match [check direction]")},
                               {INC: "INCOME"})
    assert INC not in {r["Account"] for r in rows}
    assert "cannot be booked to an income account" in str(reply) and INC in str(reply)
    assert "NEEDS REVIEW" in str(reply)


def test_an_expense_type_account_under_an_income_parent_is_still_ready(tmp_path, monkeypatch):
    reply, rows, _c, _o = _run(tmp_path, monkeypatch, [row("AIRLINE TICKET", 700, (4, 5))],
                               {"AIRLINE": (BIZ_EXP, "high", "History: AIRLINE")},
                               {BIZ_EXP: "EXPENSE"})
    assert _accounts(rows).get("AIRLINE TICKET") == BIZ_EXP


def test_weak_matched_spend_to_an_asset_account_is_never_ready(tmp_path, monkeypatch):         # NEGATIVE
    reply, rows, _c, _o = _run(tmp_path, monkeypatch, [row("UPI-SYNTH PERSON", 900, (4, 6))],
                               {"UPI": (LAND, "weak", "Weak match: prefix UPI")}, {LAND: "ASSET"})
    assert LAND not in {r["Account"] for r in rows}
    assert "asset account" in str(reply)


def test_history_and_ai_matches_to_an_asset_are_also_refused(tmp_path, monkeypatch):           # NEGATIVE
    for conf in ("history", "llm", "medium", "high"):
        reply, rows, _c, _o = _run(tmp_path, monkeypatch, [row("UPI-SYNTH PERSON", 900, (4, 6))],
                                   {"UPI": (LAND, conf, "x")}, {LAND: "ASSET"})
        assert LAND not in {r["Account"] for r in rows}, conf


def test_a_saved_user_rule_to_an_asset_account_is_still_honoured(tmp_path, monkeypatch):
    reply, rows, _c, _o = _run(tmp_path, monkeypatch, [row("UPI-SYNTH PERSON", 900, (4, 6))],
                               {"UPI": (LAND, "override", "Override: saved rule")}, {LAND: "ASSET"})
    assert _accounts(rows).get("UPI-SYNTH PERSON") == LAND


def test_a_saved_rule_cannot_make_an_income_account_ready(tmp_path, monkeypatch):               # NEGATIVE
    reply, rows, _c, _o = _run(tmp_path, monkeypatch, [row("SHARE CO TOKEN", 700, (4, 5))],
                               {"SHARE": (INC, "override", "Override: saved rule")}, {INC: "INCOME"})
    assert INC not in {r["Account"] for r in rows}


def test_a_refund_to_an_income_account_is_refused_on_its_credit_side(tmp_path, monkeypatch):    # NEGATIVE
    rows_in = [row("SHARE CO TOKEN", 700, (4, 5)),
               row("SHARE CO TOKEN", 200, (4, 9), kind="refund", direction="Cr")]
    reply, rows, _c, _o = _run(tmp_path, monkeypatch, rows_in,
                               {"SHARE": (INC, "history", "History")}, {INC: "INCOME"})
    assert INC not in {r["Account"] for r in rows}


def test_a_refund_mirroring_a_suspense_purchase_is_never_high(tmp_path, monkeypatch):           # NEGATIVE
    rows_in = [row("GADGET STORE", 500, (4, 5)),
               row("GADGET STORE", 200, (4, 9), kind="refund", direction="Cr")]
    reply, rows, _c, _o = _run(tmp_path, monkeypatch, rows_in,
                               {"GADGET": (SUSP, "suspense", "no match")}, {})
    confs = [r["Confidence"] for r in rows if r["Account"] == SUSP]
    assert len(confs) == 2 and set(confs) == {"suspense"}
    assert "high" not in {r["Confidence"] for r in rows if "GADGET" in r["Description"]}


def test_a_refund_of_a_high_purchase_stays_high(tmp_path, monkeypatch):
    rows_in = [row("CAFE ORDER", 500, (4, 5)), row("CAFE ORDER", 100, (4, 9), kind="refund", direction="Cr")]
    reply, rows, _c, _o = _run(tmp_path, monkeypatch, rows_in, {}, {})
    assert {r["Confidence"] for r in rows if "CAFE" in r["Description"]} == {"high"}


def test_the_refused_spend_goes_to_suspense_with_the_original_in_the_reason(tmp_path, monkeypatch):
    reply, rows, _c, out = _run(tmp_path, monkeypatch, [row("SHARE CO TOKEN", 700, (4, 5))],
                                {"SHARE": (INC, "history", "History: token")}, {INC: "INCOME"})
    import openpyxl
    wb = openpyxl.load_workbook(str(out).replace(".csv", "_report.xlsx"))
    cells = " ".join(str(c.value) for ws in wb for r in ws.iter_rows() for c in r if c.value)
    assert "NEEDS REVIEW" in cells and SUSP in cells and INC in cells
