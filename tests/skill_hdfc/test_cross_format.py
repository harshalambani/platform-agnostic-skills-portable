"""
tests/skill_hdfc/test_cross_format.py -- HDFC multi-format rewire tests
(items 6/7 of the spec).

Covers, all offline / synthetic-only:
  - Cross-format identity: PDF, XLS(X), and CSV fixtures encoding the SAME 5
    synthetic transactions must produce identical canonical rows.
  - Password-protected PDF: correct password succeeds; missing/wrong
    password raises a clear, actionable error (never echoing the password).
  - Garbled-text detection unit tests with constructed junk strings.
  - @pytest.mark.local_samples smoke tests over the 4 real Khyati files
    (skipped when Data/Khyati/2026 is absent -- never run in CI, never
    prints the PDF password).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hdfc_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_hdfc import agent  # noqa: E402

REAL_SAMPLES_DIR = ROOT / "Data" / "Khyati" / "2026"
REAL_PDF_PASSWORD = "9017470"


def _read_canonical(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Cross-format identity (item 6)
# ---------------------------------------------------------------------------

def test_pdf_xlsx_csv_produce_identical_canonical_rows(tmp_path):
    pdf_path = tmp_path / "syn.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())
    xlsx_path = tmp_path / "syn.xlsx"
    xlsx_path.write_bytes(fixture_gen.build_xlsx_bytes())
    csv_path = tmp_path / "syn.csv"
    csv_path.write_text(fixture_gen.build_csv_text(), encoding="utf-8")

    pdf_out, xlsx_out, csv_out = tmp_path / "pdf.csv", tmp_path / "xlsx.csv", tmp_path / "csv.csv"

    pdf_result = agent.run(str(pdf_path), str(pdf_out))
    xlsx_result = agent.run(str(xlsx_path), str(xlsx_out))
    csv_result = agent.run(str(csv_path), str(csv_out))

    assert "5 transactions" in pdf_result
    assert "5 transactions" in xlsx_result
    assert "5 transactions" in csv_result
    for result in (pdf_result, xlsx_result, csv_result):
        assert "Closing balance: VERIFIED" in result or "Running balance: OK" in result

    pdf_rows = _read_canonical(pdf_out)
    xlsx_rows = _read_canonical(xlsx_out)
    csv_rows = _read_canonical(csv_out)

    assert len(pdf_rows) == len(xlsx_rows) == len(csv_rows) == 5

    compare_fields = ("Date", "Transaction ID", "Deposit", "Withdrawal", "Balance")
    for pr, xr, cr in zip(pdf_rows, xlsx_rows, csv_rows):
        for field in compare_fields:
            assert pr[field] == xr[field] == cr[field], (
                f"field {field!r} differs across formats: "
                f"PDF={pr[field]!r} XLSX={xr[field]!r} CSV={cr[field]!r}"
            )


# ---------------------------------------------------------------------------
# Password-protected PDF (item 1)
# ---------------------------------------------------------------------------

def test_encrypted_pdf_with_correct_password_extracts(tmp_path):
    pdf_path = tmp_path / "syn_encrypted.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf(password="SYNPWD1"))
    out_path = tmp_path / "out.csv"

    result = agent.run(str(pdf_path), str(out_path), pdf_password="SYNPWD1")

    assert "5 transactions" in result
    rows = _read_canonical(out_path)
    assert len(rows) == 5


def test_encrypted_pdf_without_password_gives_clear_error(tmp_path):
    pdf_path = tmp_path / "syn_encrypted.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf(password="SYNPWD1"))
    out_path = tmp_path / "out.csv"

    result = agent.run(str(pdf_path), str(out_path))  # no password supplied

    assert "password" in result.lower()
    assert "SYNPWD1" not in result  # never echo the password


def test_encrypted_pdf_with_wrong_password_gives_clear_error(tmp_path):
    pdf_path = tmp_path / "syn_encrypted.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf(password="SYNPWD1"))
    out_path = tmp_path / "out.csv"

    result = agent.run(str(pdf_path), str(out_path), pdf_password="WRONGPWD")

    assert "password" in result.lower()
    assert "SYNPWD1" not in result
    assert "WRONGPWD" not in result


# ---------------------------------------------------------------------------
# Garbled-text detection unit tests (item 2/7)
# ---------------------------------------------------------------------------

def test_text_layer_usable_accepts_normal_statement_text():
    text = (
        "Statement of account\n"
        "Date  Narration  Chq./Ref.No.  Value Dt  Withdrawal Amt.  Deposit Amt.  Closing Balance\n"
        "01/04/25 NEFT SALARY CREDIT 0000102716301847 01/04/25 146.00 635,512.16\n"
    )
    assert agent._text_layer_usable(text) is True


def test_text_layer_usable_rejects_dense_cid_junk():
    junk_line = "(cid:34)(cid:56)(cid:78) " * 20
    text = "Date Narration\n" + junk_line
    assert agent._text_layer_usable(text) is False


def test_text_layer_usable_rejects_low_ascii_ratio():
    text = "日付 摘要 残高 " * 30 + "Date Narration"
    assert agent._text_layer_usable(text) is False


def test_text_layer_usable_rejects_missing_structural_anchors():
    text = "Some random extracted text with numbers 123.45 678.90 but no headers."
    assert agent._text_layer_usable(text) is False


def test_text_layer_usable_rejects_empty_text():
    assert agent._text_layer_usable("") is False
    assert agent._text_layer_usable("   \n  ") is False


# ---------------------------------------------------------------------------
# local_samples: the 4 real Khyati files (never run in CI; skip if absent)
# ---------------------------------------------------------------------------

@pytest.mark.local_samples
def test_real_password_pdf_matches_real_xls():
    if not REAL_SAMPLES_DIR.is_dir():
        pytest.skip(f"{REAL_SAMPLES_DIR} not present -- local-only smoke test")
    import tempfile

    pdf_path = REAL_SAMPLES_DIR / "Bank stmnt FY26.pdf"
    xls_path = REAL_SAMPLES_DIR / "Khyati HDFC-FY26.xls"
    with tempfile.TemporaryDirectory() as tmp:
        pdf_out = Path(tmp) / "pdf.csv"
        xls_out = Path(tmp) / "xls.csv"
        pdf_result = agent.run(str(pdf_path), str(pdf_out), pdf_password=REAL_PDF_PASSWORD)
        xls_result = agent.run(str(xls_path), str(xls_out))

        assert "Closing balance: VERIFIED" in pdf_result
        assert "Closing balance: VERIFIED" in xls_result

        pdf_rows = _read_canonical(pdf_out)
        xls_rows = _read_canonical(xls_out)
        assert len(pdf_rows) == len(xls_rows)

        pdf_ids = {r["Transaction ID"] for r in pdf_rows}
        xls_ids = {r["Transaction ID"] for r in xls_rows}
        assert pdf_ids == xls_ids


@pytest.mark.local_samples
def test_real_csv_matches_real_xls_row_count_and_closing_balance():
    if not REAL_SAMPLES_DIR.is_dir():
        pytest.skip(f"{REAL_SAMPLES_DIR} not present -- local-only smoke test")
    import tempfile

    csv_path = REAL_SAMPLES_DIR / "Khyati HDFC-FY26clean.csv"
    xls_path = REAL_SAMPLES_DIR / "Khyati HDFC-FY26.xls"
    with tempfile.TemporaryDirectory() as tmp:
        csv_out = Path(tmp) / "csv.csv"
        xls_out = Path(tmp) / "xls.csv"
        csv_result = agent.run(str(csv_path), str(csv_out))
        xls_result = agent.run(str(xls_path), str(xls_out))

        assert "Closing balance: VERIFIED" in csv_result
        assert "Closing balance: VERIFIED" in xls_result
        assert len(_read_canonical(csv_out)) == len(_read_canonical(xls_out))


@pytest.mark.local_samples
def test_real_garbled_pdf_detected_unusable_and_ocr_extracts_something():
    if not REAL_SAMPLES_DIR.is_dir():
        pytest.skip(f"{REAL_SAMPLES_DIR} not present -- local-only smoke test")
    import tempfile

    garbled_path = REAL_SAMPLES_DIR / "Bank stmnt FY26wopwd2.pdf"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ocr.csv"
        result = agent.run(str(garbled_path), str(out))
        assert "OCR" in result
        assert out.is_file()
