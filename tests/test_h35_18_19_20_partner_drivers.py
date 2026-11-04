"""H35-18 / H35-19 / H35-20: partner skill rounding band, s.194T default rate,
and the 40% / 48-month capital rule driven by the admission date.
All entities and figures are invented."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents.skill_partner_comp_recon.jv_emitter import build_journals  # noqa: E402
from agents.skill_partner_comp_recon.agent import apply_capital_rule_defaults  # noqa: E402
from agents.skill_partner_comp_recon.engine import (  # noqa: E402
    build_report, capital_months_from_admission,
    statement_reference_row,
)

CAP_CAT = "Closing capital: rule vs Advisory"
S194T = "s.194T TDS on interest on capital"
ACCOUNTS = {
    "bank": "Assets:Bank:Current Account",
    "tds_expense": "Expenses:Tax:TDS 194T",
    "interest_on_capital": "Income:PGBP:Interest on Capital",
    "remuneration_income": "Income:PGBP:Remuneration",
    "share_of_profit_income": "Income:PGBP:Share of Profit",
}


def _lines(data):
    return [[(s.account, s.debit, s.credit) for s in j.splits]
            for j in build_journals(build_report(data), ACCOUNTS)]


# ---- H35-18 ---------------------------------------------------------------
def _pj(amount):
    return statement_reference_row(
        "Cat", 100000.0, "LLP Statement (L5)", {"Booked (monthly)": 90000.0},
        pending_journal={"applies_to": "Booked (monthly)", "amount": amount,
                         "journal_ids": ["J1"], "description": "monthly line"})


def test_h35_18_rs6_residual_after_pending_journal_is_within_rounding():
    r = _pj(9994.0)
    assert r.agree is True
    assert "within rounding" in r.note
    assert "STATEMENT DISAGREES" not in r.note and "Genuine residual" not in r.note


def test_h35_18_rs10_boundary_is_inside_the_band():
    assert _pj(9990.0).agree is True


def test_h35_18_rs10_01_still_says_statement_disagrees():  # NEGATIVE
    r = _pj(9989.99)
    assert r.agree is False
    assert r.note.startswith("STATEMENT DISAGREES")
    assert "Genuine residual" in r.note and "within rounding" not in r.note


def test_h35_18_larger_residual_still_disagrees():  # NEGATIVE
    r = _pj(9500.0)
    assert r.agree is False and r.note.startswith("STATEMENT DISAGREES")


# ---- H35-19 ---------------------------------------------------------------
def _s194t_data(rate=None, rem_rate=None):
    drivers = {}
    if rate is not None:
        drivers["capital_interest_tds_rate"] = rate
    if rem_rate is not None:
        drivers["remuneration_tds_rate"] = rem_rate
    april = {"month": "2031-04", "remuneration": 100000, "share_of_profit_gross": 0,
             "additional_share_of_profit": 0, "firms_tax_sop": 0, "tds": -1500.0,
             "capital_transferred": 0, "total_paid": 110500.0,
             "interest_on_capital": 12000.0, "tds_schedule": -1500.0,
             "tds_payslip": -300.0}
    return {"financial_year": "2031-32", "firm_name": "Testcorp Alpha LLP",
            "drivers": drivers, "monthly": [april],
            "llp_record": {"capital_interest_on_capital": 12000.0}, "external": {}}


def _s194t_row(data):
    return next(r for r in build_report(data).reconciliation if r.category == S194T)


def test_h35_19_configured_rate_is_never_overridden_by_the_default():  # NEGATIVE
    # 5% x 12000 = 600 vs computed 1200 -> variance; a 10% default would agree.
    row = _s194t_row(_s194t_data(rate=0.05))
    assert row.agree is False
    assert "5%" in row.note and "10%" not in row.note


def test_h35_19_default_ten_percent_row_names_its_source():
    data = _s194t_data()
    data["drivers"]["capital_interest_tds_rate"] = 0.10
    data["driver_sources"] = {
        "capital_interest_tds_rate":
        "statutory default 10% (s.194T) -- not set for this entity"}
    row = _s194t_row(data)
    assert row.agree is True
    assert "statutory default 10%" in row.note


def test_h35_19_remuneration_rate_fallback_is_named():
    row = _s194t_row(_s194t_data(rem_rate=0.10))
    assert row.agree is True and "s.194T rate used: 10%" in row.note


def test_h35_19_default_never_changes_a_journal_amount():  # NEGATIVE
    a = _s194t_data()
    b = _s194t_data()
    b["drivers"]["capital_interest_tds_rate"] = 0.10
    assert _lines(a) == _lines(b)


# ---- H35-20 ---------------------------------------------------------------
@pytest.mark.parametrize("admitted,expected", [
    ("2025-04-01", 12), ("2025-04-02", 11), ("2024-04-01", 24),
    ("2025-03-31", 12), ("2025-10-15", 5), ("2020-01-01", 48)])
def test_h35_20_whole_months_to_31_march(admitted, expected):
    months, note = capital_months_from_admission(admitted, "2025-26", 48)
    assert months == expected, note


def test_h35_20_months_never_exceed_the_total():  # NEGATIVE
    assert capital_months_from_admission("2000-01-01", "2025-26", 48)[0] == 48


def test_h35_20_admission_after_year_end_is_not_applicable_never_negative():  # NEGATIVE
    months, note = capital_months_from_admission("2026-04-01", "2025-26", 48)
    assert months is None and note.startswith("Not applicable")


def test_h35_20_missing_or_bad_date_gives_no_count():  # NEGATIVE
    for bad in (None, "", "not-a-date"):
        months, note = capital_months_from_admission(bad, "2025-26", 48)
        assert months is None and note


def _cap_data(drivers=None):
    return {"financial_year": "2025-26", "monthly": [], "cohorts": [],
            "drivers": dict({"target_compensation": 1_000_000.0}, **(drivers or {})),
            "driver_sources": {}}


def test_h35_20_defaults_and_admission_date_run_the_rule():
    data = _cap_data()
    apply_capital_rule_defaults(data, "2025-04-01")
    rep = build_report(data)
    assert rep.capital_rule.status == "OK"
    # 40% x 1,000,000 x 12/48
    assert rep.capital_rule.required_cumulative_capital == pytest.approx(100000.0)


def test_h35_20_explicit_drivers_are_never_overridden():  # NEGATIVE
    data = _cap_data({"capital_rate": 0.25, "capital_months_total": 24,
                      "capital_months_achieved": 6})
    apply_capital_rule_defaults(data, "2020-01-01")
    d = data["drivers"]
    assert (d["capital_rate"], d["capital_months_total"],
            d["capital_months_achieved"]) == (0.25, 24, 6)


def test_h35_20_no_date_no_drivers_never_passes_and_says_where_to_set_it():  # NEGATIVE
    data = _cap_data()
    apply_capital_rule_defaults(data, None)
    rep = build_report(data)
    assert rep.capital_rule.status != "OK"
    row = next(r for r in rep.reconciliation if r.category.startswith(CAP_CAT))
    assert row.agree is not True and row.informational is True
    assert "Entities screen" in row.note


def test_h35_20_after_year_end_row_is_not_applicable():  # NEGATIVE
    data = _cap_data()
    apply_capital_rule_defaults(data, "2026-06-01")
    rep = build_report(data)
    assert rep.capital_rule.status != "OK"
    row = next(r for r in rep.reconciliation if r.category.startswith(CAP_CAT))
    assert row.note.startswith("Not applicable")


def test_h35_20_rule_never_alters_a_journal_line():  # NEGATIVE
    plain = _s194t_data(rem_rate=0.10)
    with_rule = _s194t_data(rem_rate=0.10)
    with_rule["drivers"]["target_compensation"] = 1_000_000.0
    apply_capital_rule_defaults(with_rule, "2025-04-01")
    assert _lines(plain) == _lines(with_rule)
