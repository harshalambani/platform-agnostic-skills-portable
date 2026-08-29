"""Tests for agents.skill_partner_comp_recon.

Fixture data (tests/fixtures/partner_comp_recon_fy2025_26.yaml) is entirely
invented and self-consistent -- no figure is copied or derived from any
real document. See that file's header comment and AGENT.md for how each
number was chosen and what it is meant to exercise.
"""
from __future__ import annotations

import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_partner_comp_recon import engine, writer
from agents.skill_partner_comp_recon.agent import run
from agents.skill_partner_comp_recon.engine import (
    CANNOT_RECONCILE,
    build_report,
    classify_cohort_instalments,
    derive_misc,
    detect_mid_year_rate_change,
    driver,
    field_or_reason,
    fy_of_date,
    gross_up_one_off,
    required_cumulative_capital,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "partner_comp_recon_fy2025_26.yaml"


def _load_fixture() -> dict:
    import yaml

    return yaml.safe_load(FIXTURE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Package plumbing
# ---------------------------------------------------------------------------

def test_skill_partner_comp_recon_imports():
    import agents.skill_partner_comp_recon as pkg
    assert pkg.__doc__
    assert hasattr(run, "__call__")


# ---------------------------------------------------------------------------
# N1 -- every rate/percentage/period is an input, never a constant/fallback.
# A missing driver must produce an explicit cannot-reconcile row, never a
# default or a computed guess.
# ---------------------------------------------------------------------------

def test_missing_driver_produces_cannot_reconcile_not_a_default():
    value, reason = driver({}, "firms_tax_rate", "2025-26", "firm's tax rate")
    assert value is None
    assert reason is not None
    assert CANNOT_RECONCILE in reason
    assert "firm's tax rate" in reason
    assert "2025-26" in reason


def test_missing_field_produces_cannot_reconcile():
    value, reason = field_or_reason({}, "bank_credits_total", "bank credits total")
    assert value is None
    assert CANNOT_RECONCILE in reason


def test_capital_rule_missing_any_input_is_cannot_reconcile_never_a_guess():
    result = required_cumulative_capital(
        target_compensation=10_000_000, months_achieved=12, months_total=None,
        rate=0.40, fy="2025-26",
    )
    assert result.status == CANNOT_RECONCILE
    assert result.required_cumulative_capital is None
    assert "capital months total" in result.reason


def test_build_report_with_no_drivers_at_all_never_computes_a_capital_figure():
    data = {"financial_year": "2025-26", "drivers": {}, "monthly": [], "cohorts": []}
    report = build_report(data)
    assert report.capital_rule.status == CANNOT_RECONCILE
    assert report.capital_rule.required_cumulative_capital is None


# ---------------------------------------------------------------------------
# s.3.1 -- the misc adjustment is always derived, never read off the advice.
# ---------------------------------------------------------------------------

def test_derive_misc():
    # total_paid - remuneration - share_of_profit_gross - additional_share_of_profit
    assert derive_misc(595280, 300000, 500000, 0) == pytest.approx(-204720)
    assert derive_misc(896400, 300000, 500000, 650560) == pytest.approx(-554160)


# ---------------------------------------------------------------------------
# s.3.2 -- grossing up a net one-off, with the roundness check.
# ---------------------------------------------------------------------------

def test_gross_up_one_off_confirmed_when_it_lands_on_a_round_lakh():
    result = gross_up_one_off(650560, 0.349440, "2025-26")
    assert result.gross == pytest.approx(1_000_000, abs=1)
    assert result.roundness == pytest.approx(0, abs=1)
    assert result.status == "CONFIRMED"


def test_gross_up_one_off_suspect_when_rate_is_wrong_for_the_year():
    result = gross_up_one_off(650560, 0.30, "2025-26")
    assert result.gross == pytest.approx(929_371.43, abs=0.01)
    # nearest lakh to 929,371.43 is 900,000 -- distance is ~29,371.43, well
    # past ROUNDNESS_TOLERANCE (1000), so this is correctly flagged SUSPECT.
    assert result.roundness == pytest.approx(29_371.43, abs=0.01)
    assert result.status == "SUSPECT"


def test_gross_up_one_off_missing_rate_is_cannot_reconcile():
    result = gross_up_one_off(650560, None, "2025-26")
    assert result.status == CANNOT_RECONCILE
    assert result.gross is None


# ---------------------------------------------------------------------------
# s.3.3 -- firm's tax must never show up as a TDS credit in Form 26AS.
# ---------------------------------------------------------------------------

def test_firms_tax_conflation_detected():
    note = engine.firms_tax_conflated_with_26as(
        firms_tax_total=-3_144_960, form_26as_total_credit=3_504_960,
        computed_creditable_tds=360_000,
    )
    assert note is not None
    assert "conflat" in note.lower() or "Form 26AS" in note


def test_firms_tax_no_conflation_when_26as_matches_tds_only():
    note = engine.firms_tax_conflated_with_26as(
        firms_tax_total=-3_144_960, form_26as_total_credit=360_000,
        computed_creditable_tds=360_000,
    )
    assert note is None


# ---------------------------------------------------------------------------
# s.4 -- cohort FY straddle: an instalment is assigned to its PAYMENT fy,
# never its award fy, and a following-year instalment is excluded.
# ---------------------------------------------------------------------------

def test_fy_of_date():
    assert fy_of_date("2025-07-31") == "2025-26"
    assert fy_of_date("2026-01-15") == "2025-26"
    assert fy_of_date("2026-06-30") == "2026-27"


def test_cohort_instalment_paid_next_fy_is_labelled_future_and_excluded():
    cohort = _load_fixture()["cohorts"][0]
    rows = classify_cohort_instalments(cohort, reporting_fy="2025-26")
    assert len(rows) == 3
    reporting = [r for r in rows if r.membership == "reporting"]
    future = [r for r in rows if r.membership == "future"]
    assert len(reporting) == 2
    assert len(future) == 1
    assert future[0].instalment_fy == "2026-27"
    assert future[0].label == "future (FY 2026-27)"
    # The award FY is preserved for traceability but never used for FY
    # assignment.
    assert future[0].award_fy == "2024-25"


# ---------------------------------------------------------------------------
# s.5.1 -- the capital rule.
# ---------------------------------------------------------------------------

def test_required_cumulative_capital():
    result = required_cumulative_capital(
        target_compensation=10_000_000, months_achieved=12, months_total=48,
        rate=0.40, fy="2025-26",
    )
    assert result.status == "OK"
    assert result.required_cumulative_capital == pytest.approx(1_000_000)


# ---------------------------------------------------------------------------
# s.5.2 -- the mid-year capital-rate-change detector.
# ---------------------------------------------------------------------------

def test_mid_year_rate_change_detected():
    suspect = detect_mid_year_rate_change(
        [416667, 291667, 291667], target_compensation=10_000_000,
        months_achieved=12, months_total=48,
    )
    assert suspect is not None
    assert suspect.implied_old_rate == pytest.approx(0.500, abs=0.001)
    assert suspect.implied_new_rate == pytest.approx(0.400, abs=0.001)
    assert "RATE CHANGE SUSPECTED" in suspect.note


def test_no_rate_change_when_all_instalment_capitals_equal():
    suspect = detect_mid_year_rate_change(
        [291667, 291667, 291667], target_compensation=10_000_000,
        months_achieved=12, months_total=48,
    )
    assert suspect is None


# ---------------------------------------------------------------------------
# End-to-end: build_report() over the full fixture.
# ---------------------------------------------------------------------------

def test_build_report_end_to_end_against_fixture():
    data = _load_fixture()
    report = build_report(data)

    assert report.financial_year == "2025-26"
    assert len(report.monthly) == 12
    # Only the two in-FY instalments count toward the reporting FY; the
    # third (2026-06-30) is next FY and must not appear in cohort totals.
    reporting = [i for i in report.cohort_instalments if i.membership == "reporting"]
    assert len(reporting) == 2

    by_category = {r.category: r for r in report.reconciliation}
    for cat, r in by_category.items():
        if cat.startswith("Incentive schedule"):
            assert r.agree is None, "Leg 2 has no independent PDF source in this build"
        else:
            assert r.agree is True, f"expected AGREE for {cat!r}, got {r.agree!r} ({r.note})"

    # The rate-change detector must fire on the fixture's one cohort even
    # though the aggregate capital rule (above) agreed exactly -- an
    # aggregate match must never mask an instalment-level anomaly.
    assert len(report.rate_change_suspects) == 1
    assert "RATE CHANGE SUSPECTED" in report.rate_change_suspects[0].note

    assert len(report.one_offs) == 1
    assert report.one_offs[0].status == "CONFIRMED"

    assert report.tds_month_exceptions == []


# ---------------------------------------------------------------------------
# Parser guard tests -- s.7. These are deliberately NOT implemented. Do not
# "finish" them opportunistically -- see AGENT.md's Stage 1/Stage 2 split.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "modname, needle",
    [
        ("advisory", "specimen"),
        ("payment_schedule", "specimen"),
        ("payout_advice", "specimen"),
        ("llp_statement", "specimen"),
    ],
)
def test_parser_is_a_guarded_placeholder(modname, needle):
    module = __import__(
        f"agents.skill_partner_comp_recon.parsers.{modname}", fromlist=["parse"]
    )
    with pytest.raises(NotImplementedError) as excinfo:
        module.parse("does-not-matter.pdf")
    assert needle in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# s.6.2 -- the "=" trap. A label that happens to start with "=" must never
# be silently stored as an Excel formula.
# ---------------------------------------------------------------------------

def test_text_helper_escapes_a_leading_equals_sign():
    assert writer._text("=SUM(A1:A2)") == " =SUM(A1:A2)"
    assert writer._text("Plain label") == "Plain label"
    assert writer._text(1234) == 1234


_FORMULA_START = re.compile(r"^[A-Za-z_$(+-]")


def test_saved_workbook_has_no_accidental_formula_cells(tmp_path):
    """Every <f> element in the saved workbook's sheet XML is a genuine
    formula this package wrote on purpose (all of which start with a
    function name, a cell reference, '$', '(' or a sign) -- never a plain
    label that merely began with '=' and got stored as a formula by
    accident (the "=" trap; see writer.py's module docstring)."""
    data = _load_fixture()
    report = build_report(data)
    out_path = tmp_path / "workbook.xlsx"
    writer.write_report_workbook(report, str(out_path))

    with zipfile.ZipFile(out_path) as zf:
        sheet_names = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet")]
        assert sheet_names
        for name in sheet_names:
            xml_bytes = zf.read(name)
            root = ET.fromstring(xml_bytes)
            for f_el in root.iter():
                if f_el.tag.endswith("}f") or f_el.tag == "f":
                    formula_text = (f_el.text or "").strip()
                    assert formula_text, f"empty formula element in {name}"
                    assert _FORMULA_START.match(formula_text), (
                        f"suspicious formula in {name}: {formula_text!r} -- "
                        "does not look like a genuine formula; check for a "
                        "label that started with '=' and was not escaped"
                    )


# ---------------------------------------------------------------------------
# End-to-end via agent.run() -- the public entry point.
# ---------------------------------------------------------------------------

_ALL_SHEETS = [
    "Logic", "Drivers", "Monthly grid", "One-offs", "Cohorts", "Capital",
    "Reconciliation", "Exceptions", "Open items",
]


def test_agent_run_end_to_end_writes_all_sheets(tmp_path):
    out_path = tmp_path / "out.xlsx"
    result = run(input_path=str(FIXTURE_PATH), output_path=str(out_path))

    assert "ERROR" not in result
    assert out_path.is_file()

    from openpyxl import load_workbook
    wb = load_workbook(out_path)
    for sheet in _ALL_SHEETS:
        assert sheet in wb.sheetnames
    # The fixture supplies no payroll rows -- that sheet must be omitted
    # entirely, not written empty.
    assert "Payroll stream" not in wb.sheetnames


def test_agent_run_reports_rate_change_warning_in_summary(tmp_path):
    out_path = tmp_path / "out.xlsx"
    result = run(input_path=str(FIXTURE_PATH), output_path=str(out_path))
    assert "rate change" in result.lower()


def test_agent_run_missing_file_returns_error_string_not_an_exception():
    result = run(input_path="does-not-exist.yaml", output_path="whatever.xlsx")
    assert result.startswith("ERROR")


def test_agent_run_unsupported_extension_returns_error_string(tmp_path):
    bad = tmp_path / "input.txt"
    bad.write_text("financial_year: '2025-26'", encoding="utf-8")
    result = run(input_path=str(bad), output_path=str(tmp_path / "out.xlsx"))
    assert result.startswith("ERROR")
