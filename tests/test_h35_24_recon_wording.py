"""H35-24: partner recon wording. A = a row whose ONLY missing source is the
filed return reads NOT CHECKED (return not supplied) and is not open;
B = the current-account tie-out shows monthly, accrual and their sum.
Synthetic fixtures only."""
from tests import test_skill_partner_comp_recon as T
from agents.skill_partner_comp_recon.engine import (
    CANNOT_RECONCILE, build_report, statement_reference_row)

CLOSING_RETURN = "Closing capital: statement vs the filed return"
CURRENT = "L5 tie-out: current-account closing balance"


def _row(report, cat):
    return [r for r in report.reconciliation if r.category == cat][0]


def test_return_only_missing_is_not_checked_and_not_open():
    r = statement_reference_row("X vs the filed return", 1000.0, "L5", {"Return": None})
    assert r.not_checked is True
    assert r.status_label == "NOT CHECKED (return not supplied)"
    assert CANNOT_RECONCILE not in r.note


def test_missing_advisory_still_cannot_reconcile_and_open():          # NEGATIVE
    r = statement_reference_row("X vs Advisory", 1000.0, "L5", {"Advisory": None})
    assert r.not_checked is False
    assert r.agree is None and CANNOT_RECONCILE in r.note


def test_missing_return_and_another_source_stays_cannot_reconcile():  # NEGATIVE
    r = statement_reference_row("X", 1000.0, "L5", {"Return": None, "Advisory": None})
    assert r.not_checked is False and CANNOT_RECONCILE in r.note


def test_real_disagreement_is_never_relabelled_not_checked():         # NEGATIVE
    r = statement_reference_row("X vs the filed return", 1000.0, "L5", {"Return": 5000.0})
    assert r.agree is False and r.not_checked is False
    assert r.note.startswith("STATEMENT DISAGREES")


def test_closing_capital_row_without_l5_still_cannot_reconcile():     # NEGATIVE
    data = T._load_fixture()
    data["llp_record"] = None
    row = _row(build_report(data), CLOSING_RETURN)
    assert row.not_checked is False
    assert row.agree is None


def _data(closing):
    return T._h35_04_rework_data(
        current_opening_balance=400000, current_closing_balance=closing,
        current_profit_share=350000)


def test_current_account_row_shows_monthly_accrual_and_sum():
    row = _row(build_report(_data(450000)), CURRENT)
    assert row.agree is True
    assert "Booked (monthly) 400,000.00" in row.note
    assert "year-end accrual 50,000.00" in row.note
    assert "sum 450,000.00" in row.note


def test_breakdown_does_not_change_verdicts():                        # NEGATIVE
    ok = _row(build_report(_data(450000)), CURRENT)
    bad = _row(build_report(_data(470000)), CURRENT)
    assert ok.agree is True
    assert bad.agree is False and bad.note.startswith("STATEMENT DISAGREES")
    assert "sum 450,000.00" in bad.note


# ---- review fix: print the sum only when the verdict used the accrual ----

def test_row_agreeing_without_accrual_prints_no_sum():                # NEGATIVE
    row = _row(build_report(_data(400000)), CURRENT)
    assert row.agree is True
    assert "sum" not in row.note.lower()
    assert "not part of this row's comparison" in row.note


def test_any_printed_sum_equals_statement_figure_on_closure_rows():
    row = _row(build_report(_data(450000)), CURRENT)
    assert row.agree is True and "sum 450,000.00" in row.note
    assert row.sources["LLP Statement (L5)"] == 450000


def test_display_never_changes_verdict_for_no_accrual_row():          # NEGATIVE
    row = _row(build_report(_data(400000)), CURRENT)
    assert row.agree is True and not row.note.startswith("STATEMENT DISAGREES")
