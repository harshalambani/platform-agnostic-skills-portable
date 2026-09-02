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
import yaml

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
from agents.skill_partner_comp_recon.jv_emitter import (
    JOURNAL_HEADERS,
    build_journals,
    fy_prefix,
    write_journal_csv,
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


# ---------------------------------------------------------------------------
# H3.5 -- manifest reshape: skill.yaml now takes the raw documents
# directly. `input_path` is demoted to a test-only run() parameter (used
# throughout this file above) and must never render in the UI; every
# optional document-backed leg must degrade to an explicit "not
# available" note rather than a zero/default; a missing REQUIRED input
# must fail loud, naming it, before any parsing is attempted.
# ---------------------------------------------------------------------------

def _skill_yaml_inputs() -> list[dict]:
    skill_yaml_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "agents" / "skill_partner_comp_recon" / "skill.yaml"
    )
    manifest = yaml.safe_load(skill_yaml_path.read_text(encoding="utf-8"))
    return manifest["inputs"]


def test_input_path_absent_from_manifest_but_run_still_accepts_it():
    names = {inp["name"] for inp in _skill_yaml_inputs()}
    assert "input_path" not in names, (
        "input_path must be demoted to a test-only run() parameter -- it "
        "must never appear in skill.yaml's inputs: (else it renders in the UI)"
    )
    # run() must still accept it -- every e2e test above this section calls
    # run(input_path=...) and depends on that continuing to work.
    import inspect

    assert "input_path" in inspect.signature(run).parameters


def _advices_dir_with_one_pdf(tmp_path: Path) -> Path:
    advices_dir = tmp_path / "advices"
    advices_dir.mkdir()
    (advices_dir / "jan.pdf").write_bytes(b"%PDF-1.4 not a real pdf")
    return advices_dir


def test_missing_required_input_fails_loud_and_names_it(tmp_path):
    advices_dir = _advices_dir_with_one_pdf(tmp_path)
    advisory_path = tmp_path / "advisory.pdf"
    advisory_path.write_bytes(b"%PDF-1.4 not a real pdf")

    # entity missing
    result = run(
        entity="",
        advices_dir=str(advices_dir),
        advisory_path=str(advisory_path),
        output_path=str(tmp_path / "out.xlsx"),
    )
    assert result.startswith("ERROR")
    assert "entity" in result

    # advices_dir missing
    result = run(
        entity="Harshal",
        advices_dir="",
        advisory_path=str(advisory_path),
        output_path=str(tmp_path / "out.xlsx"),
    )
    assert result.startswith("ERROR")
    assert "advices_dir" in result

    # advisory_path missing
    result = run(
        entity="Harshal",
        advices_dir=str(advices_dir),
        advisory_path="",
        output_path=str(tmp_path / "out.xlsx"),
    )
    assert result.startswith("ERROR")
    assert "advisory_path" in result


def test_optional_inputs_absent_degrade_to_not_available_never_zero_or_default(tmp_path):
    advices_dir = _advices_dir_with_one_pdf(tmp_path)
    advisory_path = tmp_path / "advisory.pdf"
    advisory_path.write_bytes(b"%PDF-1.4 not a real pdf")

    result = run(
        entity="Harshal",
        advices_dir=str(advices_dir),
        advisory_path=str(advisory_path),
        llp_statement="",
        gnucash_path="",
        xlsx_26as="",
        output_path=str(tmp_path / "out.xlsx"),
    )
    # The required legs still hard-fail in this build (Stage 2 parsers are
    # placeholders), but the optional-leg status notes must appear
    # regardless, and every one of them must say "not available" -- never
    # a zero, a blank, or a fabricated figure.
    lowered = result.lower()
    assert "llp statement of account: not available" in lowered
    assert "gnucash books tie-out: not available" in lowered
    assert "26as tds-credit tie-out: not available" in lowered
    assert "not available (no document supplied)" in lowered  # llp_statement
    assert "not available (no book supplied)" in lowered  # gnucash_path
    assert "not available (no workbook supplied)" in lowered  # xlsx_26as


# ---------------------------------------------------------------------------
# Stage 1b -- jv_emitter.py: the GnuCash multi-split journal CSV.
# ---------------------------------------------------------------------------

def _read_journal_csv(path: Path) -> list[dict]:
    import csv

    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _build_fixture_journal_rows(tmp_path: Path) -> list[dict]:
    data = _load_fixture()
    report = build_report(data)
    journals = build_journals(report, data["accounts"])
    out_path = tmp_path / "journal.csv"
    write_journal_csv(journals, str(out_path))
    return _read_journal_csv(out_path)


# 3.1 -- header is exactly the 7 columns, in order.
def test_journal_csv_header_exact(tmp_path):
    rows = _build_fixture_journal_rows(tmp_path)
    assert rows  # sanity: fixture must produce at least one row
    with (tmp_path / "journal.csv").open(encoding="utf-8") as f:
        header_line = f.readline().strip()
    assert header_line.split(",") == JOURNAL_HEADERS
    assert JOURNAL_HEADERS == [
        "Date", "Transaction ID", "Number", "Description", "Account",
        "Amount", "Currency",
    ]


# 3.2 -- no cell in Date/Transaction ID/Number/Description is ever blank
# (regression guard for the blank-continuation-row parse-error trap).
def test_journal_csv_never_has_blank_transaction_fields(tmp_path):
    rows = _build_fixture_journal_rows(tmp_path)
    for row in rows:
        assert row["Date"].strip(), row
        assert row["Transaction ID"].strip(), row
        assert row["Number"].strip(), row
        assert row["Description"].strip(), row


# 3.3 -- rows sharing a Transaction ID share Date/Number/Description, and
# their Amounts sum to 0.00 within half a paisa.
def test_journal_csv_transactions_are_internally_consistent_and_balanced(tmp_path):
    rows = _build_fixture_journal_rows(tmp_path)
    by_txn: dict[str, list[dict]] = {}
    for row in rows:
        by_txn.setdefault(row["Transaction ID"], []).append(row)

    for txn_id, txn_rows in by_txn.items():
        dates = {r["Date"] for r in txn_rows}
        numbers = {r["Number"] for r in txn_rows}
        descriptions = {r["Description"] for r in txn_rows}
        assert len(dates) == 1, f"{txn_id}: mixed Date values {dates}"
        assert len(numbers) == 1, f"{txn_id}: mixed Number values {numbers}"
        assert len(descriptions) == 1, f"{txn_id}: mixed Description values {descriptions}"
        assert numbers == {txn_id}

        total = sum(float(r["Amount"]) for r in txn_rows)
        assert total == pytest.approx(0.0, abs=0.005), f"{txn_id}: splits sum to {total}"


# 3.4 -- Transaction IDs are unique per transaction, all carry the FY
# prefix, and no ID is a prefix of another (so GnuCash's grouping can never
# fuse two distinct transactions).
def test_journal_csv_transaction_ids_unique_fy_prefixed_and_not_colliding(tmp_path):
    data = _load_fixture()
    fy_pfx = fy_prefix(data["financial_year"])
    rows = _build_fixture_journal_rows(tmp_path)
    txn_ids = sorted({row["Transaction ID"] for row in rows})
    assert len(txn_ids) >= 2  # at least the opening reclass + one month

    for txn_id in txn_ids:
        assert txn_id.startswith(fy_pfx + "-"), txn_id

    for a in txn_ids:
        for b in txn_ids:
            if a != b:
                assert not b.startswith(a), f"{a!r} is a prefix of {b!r}"


# 3.5 -- Currency is INR on every row; every Date matches YYYY-MM-DD.
def test_journal_csv_currency_and_date_format(tmp_path):
    rows = _build_fixture_journal_rows(tmp_path)
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for row in rows:
        assert row["Currency"] == "INR"
        assert date_re.match(row["Date"]), row["Date"]


# 3.6 -- no Account starts with "Root Account:"; every Account appears in
# the fixture's own accounts: block (no invented/typo'd account leaks in).
def test_journal_csv_accounts_have_no_root_prefix_and_are_all_known(tmp_path):
    data = _load_fixture()
    known_accounts = set(data["accounts"].values())
    rows = _build_fixture_journal_rows(tmp_path)
    for row in rows:
        assert not row["Account"].startswith("Root Account:"), row["Account"]
        assert row["Account"] in known_accounts, row["Account"]


# 3.7 -- no Amount is ever "0.00" (zero-value splits must be omitted, not
# emitted as a no-op row).
def test_journal_csv_never_emits_a_zero_amount_row(tmp_path):
    rows = _build_fixture_journal_rows(tmp_path)
    for row in rows:
        assert row["Amount"] != "0.00", row


# 3.8 -- no Transfer Amount / Transfer Account column is ever emitted.
def test_journal_csv_has_no_transfer_columns(tmp_path):
    rows = _build_fixture_journal_rows(tmp_path)
    assert rows
    for row in rows:
        assert "Transfer Amount" not in row
        assert "Transfer Account" not in row


# 3.9 -- the firm's tax never appears as its own leg, and the share-of-
# profit credit for a month is the NET figure (gross + firms_tax +
# additional), never the gross figure.
def test_journal_csv_share_of_profit_is_net_of_firms_tax(tmp_path):
    data = _load_fixture()
    report = build_report(data)
    journals = build_journals(report, data["accounts"])

    firms_tax_account = data["accounts"]["tds_expense"]  # sanity: distinct key
    share_account = data["accounts"]["share_of_profit_income"]

    # Firm's tax must never be booked to any account under its own name --
    # there is no accounts.firms_tax key at all, so this is really just
    # confirming ACCOUNT_KEYS has no such entry and no journal invents one.
    for j in journals:
        for s in j.splits:
            assert s.account != "firms_tax", s

    by_month = {m.month: m for m in report.monthly}
    april = by_month["2025-04"]
    expected_net = (
        april.share_of_profit_gross + april.firms_tax + april.additional_share_of_profit
    )

    april_journal = next(j for j in journals if j.txn_id.endswith("-M01"))
    share_split = next(s for s in april_journal.splits if s.account == share_account)
    # Credit leg -> stored as .credit, positive.
    assert share_split.credit == pytest.approx(expected_net, abs=0.01)
    assert share_split.debit == 0.0


# 3.9b -- prior_cohort_drawdown is POSITIVE (a prior-year incentive
# instalment RECEIVED this year) and must produce a CREDIT (negative
# Amount) on the current_account split equal to -prior_cohort_drawdown,
# with no income account carrying any split attributable to it. A
# balance-only check is not sufficient to pin this direction -- the
# transaction still balances even with the wrong sign, which is exactly
# how the wrong sign shipped once already.
def test_journal_csv_prior_cohort_drawdown_credits_current_account(tmp_path):
    data = _load_fixture()
    report = build_report(data)
    journals = build_journals(report, data["accounts"])

    by_month = {m.month: m for m in report.monthly}
    april = by_month["2025-04"]
    assert april.prior_cohort_drawdown > 0, "fixture must exercise the positive case"

    current_account = data["accounts"]["current_account"]
    income_accounts = {
        data["accounts"]["remuneration_income"],
        data["accounts"]["share_of_profit_income"],
        data["accounts"]["interest_on_capital"],
    }

    april_journal = next(j for j in journals if j.txn_id.endswith("-M01"))
    drawdown_split = next(s for s in april_journal.splits if s.account == current_account)

    # Credit leg: stored as .credit (positive), .debit is 0 -- and the CSV
    # row's signed Amount must be negative (Cr).
    assert drawdown_split.debit == 0.0
    assert drawdown_split.credit == pytest.approx(april.prior_cohort_drawdown, abs=0.01)

    out_path = tmp_path / "journal_drawdown.csv"
    write_journal_csv(journals, str(out_path))
    csv_rows = _read_journal_csv(out_path)
    april_rows = [r for r in csv_rows if r["Transaction ID"].endswith("-M01")]
    current_account_rows = [r for r in april_rows if r["Account"] == current_account]
    assert len(current_account_rows) == 1
    amount = float(current_account_rows[0]["Amount"])
    assert amount == pytest.approx(-april.prior_cohort_drawdown, abs=0.01)
    assert amount < 0, "prior_cohort_drawdown must book as a CREDIT (negative Amount)"

    # No income account may carry a split whose value traces to the
    # drawdown -- i.e. no income-account split in April's transaction
    # equals the drawdown amount (which would indicate double-counting it
    # as income).
    for row in april_rows:
        if row["Account"] in income_accounts:
            assert abs(float(row["Amount"])) != pytest.approx(
                april.prior_cohort_drawdown, abs=0.01
            ), row


# 3.10 -- the opening reclass entry is present, dated as given, carries the
# "<FY>-RECT" id, and balances.
def test_journal_csv_opening_reclass_present_and_balanced(tmp_path):
    data = _load_fixture()
    fy_pfx = fy_prefix(data["financial_year"])
    report = build_report(data)
    journals = build_journals(report, data["accounts"])

    rect = next(j for j in journals if j.txn_id == f"{fy_pfx}-RECT")
    assert rect.date == data["opening_reclass"]["date"]
    assert rect.balanced
    assert rect.total_debit == pytest.approx(218650, abs=0.01)
    assert rect.total_credit == pytest.approx(218650, abs=0.01)


# 3.11 -- a missing required account key produces an "ERROR: ..." string
# from run(), never a traceback, and names the key.
def test_missing_account_key_returns_error_string_not_exception(tmp_path):
    import yaml

    data = _load_fixture()
    del data["accounts"]["interest_on_capital"]  # April's line uses this key
    bad_input = tmp_path / "bad_input.yaml"
    bad_input.write_text(yaml.safe_dump(data), encoding="utf-8")

    out_path = tmp_path / "out.xlsx"
    journal_path = tmp_path / "journal.csv"
    result = run(
        input_path=str(bad_input), output_path=str(out_path),
        journal_path=str(journal_path),
    )
    assert result.startswith("ERROR")
    assert "interest_on_capital" in result
    assert not journal_path.exists()


# 3.12 -- with journal_path="" the workbook output is byte-identical to a
# run that never mentions Stage 1b at all (no CSV written, no behaviour
# change).
def test_empty_journal_path_leaves_workbook_output_unchanged(tmp_path):
    out_default = tmp_path / "out_default.xlsx"
    out_explicit_empty = tmp_path / "out_explicit_empty.xlsx"

    result_default = run(input_path=str(FIXTURE_PATH), output_path=str(out_default))
    result_explicit = run(
        input_path=str(FIXTURE_PATH), output_path=str(out_explicit_empty),
        journal_path="",
    )

    assert result_default == result_explicit.replace(
        str(out_explicit_empty), str(out_default)
    )
    # Compare workbook content, not raw bytes: openpyxl stamps docProps/core.xml
    # with a wall-clock created/modified timestamp on every save, so two
    # separate runs are never byte-identical even with identical inputs --
    # what "unchanged" actually means here is every sheet's content, which
    # this compares directly, ignoring only that timestamp-bearing part.
    with zipfile.ZipFile(out_default) as zf_a, zipfile.ZipFile(out_explicit_empty) as zf_b:
        names_a = {n for n in zf_a.namelist() if n != "docProps/core.xml"}
        names_b = {n for n in zf_b.namelist() if n != "docProps/core.xml"}
        assert names_a == names_b
        for name in sorted(names_a):
            assert zf_a.read(name) == zf_b.read(name), f"content differs in {name}"
    assert list(tmp_path.glob("*.csv")) == []  # journal_path unset -> no CSV written at all
