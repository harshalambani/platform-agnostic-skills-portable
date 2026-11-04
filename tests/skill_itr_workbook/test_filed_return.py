"""
tests/skill_itr_workbook/test_filed_return.py -- tests for
scripts/filed_return.py against the synthetic SYN-IND filed-return JSON
fixture and a synthetic PDF fallback fixture; plus unit tests for the
regime-election cross-check logic (direct flag vs. standard-deduction
fallback vs. NOT-EXTRACTED when neither is available).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

for p in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import filed_return as fr  # noqa: E402
import fixture_gen  # noqa: E402


def test_parse_filed_json_heads_and_schedules():
    parsed = fr.parse_filed_return(FIXTURES / "syn_ind_filed_return.json", "SYN-IND")
    assert parsed.source_format == "json"
    assert parsed.itr_form == "ITR2"
    assert parsed.regime == "new"
    assert parsed.regime_note == "OptOutNewTaxRegime=N"

    assert parsed.heads["Salaries"] == 800000
    assert parsed.heads["GrossTotalIncome"] == 820550
    assert parsed.heads["TotalIncome"] == 670550
    assert parsed.heads["CapitalGainsTotal"] == 17000
    assert parsed.heads["OtherSourcesTotal"] == 3550

    assert parsed.schedule_os["interest_sb"] == 500
    assert parsed.schedule_os["interest_td"] == 2000
    assert parsed.schedule_os["interest_refund"] == 300
    assert parsed.schedule_os["dividend_gross"] == 750

    assert parsed.schedule_cg["short_term_total"] == 5000
    assert parsed.schedule_cg["long_term_total"] == 12000
    assert parsed.schedule_cg["total"] == 17000

    assert parsed.via_claimed["Section80C"] == 150000

    assert parsed.interest_234["TotalIntrstPay"] == 700
    assert parsed.taxes_paid["TDS"] == 50000
    assert parsed.tax_liability["net_tax_liability"] == 46800
    assert parsed.tax_liability["refund_due"] == 3200

    # Schedule AL absent from this filed return (below the income threshold
    # at which it becomes mandatory) -- must be reported as such, not as a
    # gap or an error.
    assert parsed.schedule_al == {"status": "not_present_in_filed_return"}
    assert parsed.not_extracted_fields == []


def test_regime_direct_flag_old():
    itr = {"PartA_GEN1": {"FilingStatus": {"OptOutNewTaxRegime": "Y"}}}
    regime, note = fr._extract_regime(itr)
    assert regime == "old"
    assert "OptOutNewTaxRegime=Y" in note


def test_regime_fallback_via_standard_deduction_new():
    # ITR-3/4 style filing: no direct OptOutNewTaxRegime flag, only Form
    # 10-IEA bookkeeping fields -- must fall back to the salary standard
    # deduction amount (75000 => new regime for AY2025-26).
    itr = {
        "PartA_GEN1": {"FilingStatus": {"OptOutNewTaxRegime_Method": "BY10IEA"}},
        "ScheduleS": {"DeductionUnderSection16ia": 75000},
    }
    regime, note = fr._extract_regime(itr)
    assert regime == "new"
    assert "75000" in note


def test_regime_fallback_via_standard_deduction_old():
    itr = {
        "PartA_GEN1": {"FilingStatus": {"OptOutNewTaxRegime_Method": "BY10IEA"}},
        "ScheduleS": {"DeductionUnderSection16ia": 50000},
    }
    regime, note = fr._extract_regime(itr)
    assert regime == "old"
    assert "50000" in note


def test_regime_not_extracted_when_neither_signal_present():
    regime, note = fr._extract_regime({})
    assert regime == fr.NOT_EXTRACTED
    assert note


def test_missing_fields_are_not_extracted_never_guessed():
    itr = {"PartB-TI": {"Salaries": 500000}}
    heads = fr._extract_heads(itr)
    assert heads["Salaries"] == 500000
    assert heads["GrossTotalIncome"] == fr.NOT_EXTRACTED
    assert heads["TotalIncome"] == fr.NOT_EXTRACTED


def test_parse_filed_pdf_fallback_extracts_labeled_amounts(tmp_path):
    pdf_path = tmp_path / "syn_ind_filed_return.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_filed_return_pdf())

    parsed = fr.parse_filed_return(pdf_path, "SYN-IND")
    assert parsed.source_format == "pdf"
    assert parsed.heads["GrossTotalIncome"] == 900000.0
    assert parsed.heads["TotalIncome"] == 750000.0
    assert parsed.tax_liability["net_tax_liability"] == 52000.0
    assert parsed.tax_liability["refund_due"] == 4100.0
    # PDF path never guesses the regime -- always NOT-EXTRACTED, flagged.
    assert parsed.regime == fr.NOT_EXTRACTED
    assert "regime" in parsed.not_extracted_fields


def test_unsupported_extension_raises():
    import pytest
    with pytest.raises(ValueError):
        fr.parse_filed_return("somefile.txt", "SYN-IND")
