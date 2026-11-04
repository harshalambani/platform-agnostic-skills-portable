"""
tests/skill_icici/test_icici_golden.py -- ICICI P2 golden-family tests
(bank abstraction P2, scope item 4).

ICICI only ever emits one real input shape (the .xls net-banking download --
xlrd cannot even read .xlsx), so there is no cross-format identity to prove
like BoB's PDF-vs-CSV pair. Instead this locks a synthetic .xls (5 fake
transactions, built by icici_fixture_gen.build_xls()) to its expected
canonical output as a regression test, and separately proves .xlsx is
declared unsupported.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import icici_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_icici.agent import ICICISkill  # noqa: E402

EXPECTED_ROWS = [
    {
        "Date": "2025-04-01", "Transaction ID": "REF0000000001",
        "Description": "SYN SALARY CREDIT", "Account": "",
        "Deposit": "50000", "Withdrawal": "0", "Balance": "150000",
        "Currency": "INR",
    },
    {
        "Date": "2025-04-02", "Transaction ID": "300000000002",
        "Description": "synshop", "Account": "",
        "Deposit": "0", "Withdrawal": "2000", "Balance": "148000",
        "Currency": "INR",
    },
    {
        "Date": "2025-04-03", "Transaction ID": "REF0000000003",
        "Description": "SYN UTILITY BILL", "Account": "",
        "Deposit": "0", "Withdrawal": "5000", "Balance": "143000",
        "Currency": "INR",
    },
    {
        "Date": "2025-04-04", "Transaction ID": "300000000004",
        "Description": "synrefund/SYNdef0001gh", "Account": "",
        "Deposit": "500", "Withdrawal": "0", "Balance": "143500",
        "Currency": "INR",
    },
    {
        "Date": "2025-04-05", "Transaction ID": "654321",
        "Description": "CLG/SYN CHEQUE DEPOSIT", "Account": "",
        "Deposit": "0", "Withdrawal": "5000", "Balance": "138500",
        "Currency": "INR",
    },
]


# ---------------------------------------------------------------------------
# Synthetic .xls identity/regression (scope item 4)
# ---------------------------------------------------------------------------

def test_synthetic_xls_produces_expected_canonical_rows(tmp_path):
    xls_path = tmp_path / "syn_icici.xls"
    xls_path.write_bytes(fixture_gen.build_xls())

    result = ICICISkill().parse(xls_path)

    assert result.rows == EXPECTED_ROWS
    assert result.opening_balance == fixture_gen.SYN_OPENING_BALANCE
    assert result.closing_balance == fixture_gen.SYN_CLOSING_BALANCE
    assert result.balance_check.ok is True
    assert result.warnings == []


def test_synthetic_xls_populates_bank_statement_meta(tmp_path):
    xls_path = tmp_path / "syn_icici.xls"
    xls_path.write_bytes(fixture_gen.build_xls())

    result = ICICISkill().parse(xls_path)

    assert result.meta is not None
    assert result.meta.bank_key == "icici"
    assert result.meta.account_number == fixture_gen.SYN_ACCOUNT_NUMBER
    assert result.meta.period_from == fixture_gen.SYN_PERIOD_FROM
    assert result.meta.period_to == fixture_gen.SYN_PERIOD_TO
    assert result.meta.source_format == "xls"
    assert result.meta.fidelity == "exact"
    assert result.meta.password_used is False


def test_synthetic_xls_detected_with_high_confidence(tmp_path):
    xls_path = tmp_path / "syn_icici.xls"
    xls_path.write_bytes(fixture_gen.build_xls())

    assert ICICISkill().detect(xls_path) == 0.9


# ---------------------------------------------------------------------------
# .xlsx declared unsupported (xlrd 2.x cannot read it; scope item 4/5)
# ---------------------------------------------------------------------------

def test_xlsx_not_in_formats():
    assert ".xlsx" not in ICICISkill().formats()
    assert ICICISkill().formats() == (".xls",)


def test_xlsx_detect_returns_zero(tmp_path):
    xlsx_path = tmp_path / "syn_icici.xlsx"
    xlsx_path.write_bytes(fixture_gen.build_xls())  # content irrelevant -- suffix gates detect()

    assert ICICISkill().detect(xlsx_path) == 0.0
