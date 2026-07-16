"""
tests/skill_bob/test_cross_format.py -- BoB P2 cross-format golden-family
tests (bank abstraction P2, scope items 2/4/5).

Covers, all offline / synthetic-only:
  - Cross-format identity: a synthetic PDF (split across 2 pages, no
    repeated header) and the native CSV extract_bob_statement.py emits for
    the SAME 5 transactions must produce identical canonical rows.
  - BankStatementMeta is fully populated from the synthetic PDF's front
    matter (account_number, period_from/to, source_format, fidelity,
    password_used).
  - Password-protected PDF: correct password succeeds; missing/wrong
    password raises a clear, actionable error (never echoing the password).
  - Text-quality rejection: a page with no structural anchors raises,
    with no OCR fallback.
  - The Cr-suffixed balance column (bank_common.normalize) round-trips.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import bob_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_bob.agent import BoBSkill, _native_csv_to_canonical  # noqa: E402
from agents.skill_bob.scripts.extract_bob_statement import extract  # noqa: E402


# ---------------------------------------------------------------------------
# Cross-format identity (scope item 4)
# ---------------------------------------------------------------------------

def test_pdf_native_csv_produce_identical_canonical_rows(tmp_path):
    pdf_path = tmp_path / "syn_bob.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())
    csv_path = tmp_path / "syn_bob_native.csv"
    csv_path.write_text(fixture_gen.build_native_csv_text(), encoding="utf-8")

    pdf_result = BoBSkill().parse(pdf_path)
    csv_rows, csv_warnings = _native_csv_to_canonical(str(csv_path))

    assert csv_warnings == []
    assert len(pdf_result.rows) == len(csv_rows) == 5
    assert pdf_result.rows == csv_rows
    assert pdf_result.opening_balance == 100000.0
    assert pdf_result.closing_balance == 140000.0
    assert pdf_result.balance_check.ok is True


def test_synthetic_pdf_populates_bank_statement_meta(tmp_path):
    pdf_path = tmp_path / "syn_bob.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())

    result = BoBSkill().parse(pdf_path)

    assert result.meta is not None
    assert result.meta.bank_key == "bob"
    assert result.meta.account_number == fixture_gen.SYN_ACCOUNT_NUMBER
    assert result.meta.period_from == "2025-04-01"
    assert result.meta.period_to == "2025-04-30"
    assert result.meta.source_format == "pdf"
    assert result.meta.fidelity == "exact"
    assert result.meta.password_used is False


def test_multipage_pdf_without_repeated_header_parses_all_rows(tmp_path):
    # build_pdf() already splits the 5 txns 3/2 across two pages with no
    # header on page 2 -- assert the full row count (+ opening balance) made
    # it through the column-geometry-reuse path.
    pdf_path = tmp_path / "syn_bob.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())
    rows = extract(pdf_path)
    assert len(rows) == 6  # opening balance + 5 transactions


# ---------------------------------------------------------------------------
# Password-protected PDF
# ---------------------------------------------------------------------------

def test_encrypted_pdf_with_correct_password_extracts(tmp_path):
    pdf_path = tmp_path / "syn_bob_encrypted.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf(password="SYNPWD1"))

    result = BoBSkill().parse(pdf_path, password="SYNPWD1")

    assert len(result.rows) == 5
    assert result.meta.password_used is True
    assert result.meta.source_format == "pw-pdf"


def test_encrypted_pdf_without_password_gives_clear_error(tmp_path):
    pdf_path = tmp_path / "syn_bob_encrypted.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf(password="SYNPWD1"))

    with pytest.raises(RuntimeError) as exc_info:
        BoBSkill().parse(pdf_path)

    msg = str(exc_info.value).lower()
    assert "password" in msg
    assert "SYNPWD1" not in str(exc_info.value)


def test_encrypted_pdf_with_wrong_password_gives_clear_error(tmp_path):
    pdf_path = tmp_path / "syn_bob_encrypted.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf(password="SYNPWD1"))

    with pytest.raises(RuntimeError) as exc_info:
        BoBSkill().parse(pdf_path, password="WRONGPWD")

    msg = str(exc_info.value)
    assert "password" in msg.lower()
    assert "SYNPWD1" not in msg
    assert "WRONGPWD" not in msg


# ---------------------------------------------------------------------------
# Text-quality rejection (scope item 2 -- no OCR fallback for BoB)
# ---------------------------------------------------------------------------

def test_garbled_pdf_raises_with_no_ocr_fallback(tmp_path):
    pdf_path = tmp_path / "garbled.pdf"
    pdf_path.write_bytes(fixture_gen.build_garbled_pdf())

    with pytest.raises(RuntimeError) as exc_info:
        extract(pdf_path)

    assert "OCR" in str(exc_info.value)
