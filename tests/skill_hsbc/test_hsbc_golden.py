"""
tests/skill_hsbc/test_hsbc_golden.py -- HSBC P2 golden-family tests
(bank abstraction P2, scope items 1/2/4).

HSBC's OCR stage is not byte-deterministic across environments, so unlike
HDFC/BoB/ICICI this does NOT golden-test PDF-in -> canonical byte-for-byte.
Instead it locks the DETERMINISTIC stage -- an already-enriched workbook
(hsbc_fixture_gen.build_xlsx(), shaped exactly like scripts/build_xlsx.py's
real output) -> canonical rows -- to its expected output as a regression
test. The OCR stage itself stays covered by tests/skill_hsbc/test_parse_tsv.py
(Session A's float-confidence + continuity-detection tests, untouched here).
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

import hsbc_fixture_gen as fixture_gen  # noqa: E402
from agents.bank_contract import BankSkill  # noqa: E402
from agents.skill_hsbc.agent import HSBCSkill  # noqa: E402

EXPECTED_ROWS = [
    {
        "Date": "2025-04-01", "Transaction ID": "", "Description": "BALANCE BROUGHT FORWARD",
        "Account": "", "Deposit": "0", "Withdrawal": "0", "Balance": "100000.0",
        "Currency": "INR",
    },
    {
        "Date": "2025-04-01", "Transaction ID": "UPI3000000001", "Description": "SYN SALARY CREDIT",
        "Account": "", "Deposit": "50000.0", "Withdrawal": "0", "Balance": "150000.0",
        "Currency": "INR",
    },
    {
        "Date": "2025-04-02", "Transaction ID": "", "Description": "synshop purchase",
        "Account": "", "Deposit": "0", "Withdrawal": "2000.0", "Balance": "148000.0",
        "Currency": "INR",
    },
    {
        "Date": "2025-04-03", "Transaction ID": "NEFTSYN00000003", "Description": "SYN UTILITY BILL",
        "Account": "", "Deposit": "0", "Withdrawal": "5000.0", "Balance": "143000.0",
        "Currency": "INR",
    },
    {
        "Date": "2025-04-04", "Transaction ID": "IMPS300000004",
        "Description": "SYN REFUND | ELECTRO 12:30:00",
        "Account": "", "Deposit": "500.0", "Withdrawal": "0", "Balance": "143500.0",
        "Currency": "INR",
    },
    {
        "Date": "2025-04-05", "Transaction ID": "654321", "Description": "SYN CHEQUE DEPOSIT",
        "Account": "", "Deposit": "0", "Withdrawal": "5000.0", "Balance": "138500.0",
        "Currency": "INR",
    },
]


def _write_fixture(tmp_path) -> Path:
    xlsx_path = tmp_path / "syn_hsbc_enriched.xlsx"
    xlsx_path.write_bytes(fixture_gen.build_xlsx())
    return xlsx_path


# ---------------------------------------------------------------------------
# Deterministic-stage identity/regression (enriched workbook -> canonical)
# ---------------------------------------------------------------------------

def test_conforms_to_bank_skill_protocol():
    assert isinstance(HSBCSkill(), BankSkill)


def test_enriched_xlsx_produces_expected_canonical_rows(tmp_path):
    xlsx_path = _write_fixture(tmp_path)

    result = HSBCSkill().parse(xlsx_path)

    assert result.rows == EXPECTED_ROWS
    assert result.opening_balance == fixture_gen.SYN_OPENING_BALANCE
    assert result.closing_balance == fixture_gen.SYN_CLOSING_BALANCE
    assert result.balance_check.ok is True
    assert result.warnings == []


def test_enriched_xlsx_populates_bank_statement_meta(tmp_path):
    xlsx_path = _write_fixture(tmp_path)

    result = HSBCSkill().parse(xlsx_path)

    assert result.meta is not None
    assert result.meta.bank_key == "hsbc"
    assert result.meta.source_format == "pdf"
    assert result.meta.fidelity == "ocr-approx"
    assert result.meta.password_used is False
    assert result.meta.period_from == "2025-04-01"
    assert result.meta.period_to == "2025-04-05"


def test_enriched_xlsx_detected_with_high_confidence(tmp_path):
    xlsx_path = _write_fixture(tmp_path)

    assert HSBCSkill().detect(xlsx_path) == 0.9


def test_extra_information_folded_into_description_no_loss(tmp_path):
    """Scope item 4: Extra Information content must not be silently dropped
    -- the canonical schema has no dedicated column for it, so it's folded
    into Description."""
    xlsx_path = _write_fixture(tmp_path)

    result = HSBCSkill().parse(xlsx_path)

    row = next(r for r in result.rows if r["Transaction ID"] == "IMPS300000004")
    assert "ELECTRO 12:30:00" in row["Description"]


def test_output_path_writes_canonical_csv_and_sidecar(tmp_path):
    """The current skill_gnucash_pipeline call site does
    ``HSBCSkill().parse(xlsx, output_path=canonical_path)`` -- this must keep
    working unmodified even though ``parse()`` also grew a ``password`` arg."""
    xlsx_path = _write_fixture(tmp_path)
    out_csv = tmp_path / "canonical.csv"

    result = HSBCSkill().parse(xlsx_path, output_path=out_csv)

    assert out_csv.is_file()
    assert result.sidecar_path is not None
    assert Path(result.sidecar_path).is_file()


# ---------------------------------------------------------------------------
# formats() / detect() -- PDF is the declared uniform format
# ---------------------------------------------------------------------------

def test_formats_declares_pdf():
    assert HSBCSkill().formats() == (".pdf",)


def test_detect_rejects_unrelated_suffix(tmp_path):
    txt_path = tmp_path / "not_a_statement.txt"
    txt_path.write_text("hello")
    assert HSBCSkill().detect(txt_path) == 0.0
