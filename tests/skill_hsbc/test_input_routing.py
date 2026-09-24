"""
tests/skill_hsbc/test_input_routing.py -- HSB-03: standalone tab's run()
input-routing / validation, added ahead of any subprocess call.

Route B defect (see agent.py's run() docstring): the standalone tab passed
whatever path it was given straight to the OCR pipeline, which globs
`*.pdf` on a directory. A single-PDF path (or an enriched .xlsx) always
ended in an unhelpful "No PDFs found in <path>" / "Stage failed", with no
guidance, even though HSBCSkill.parse() already accepts a single PDF.

Covers (all offline, subprocess.run mocked -- OCR itself stays out of scope):
  Positive:
    - A single PDF path is staged into its own temp directory and passed to
      the OCR pipeline as that directory (containing exactly that PDF).
    - A directory of PDFs is passed through to the pipeline unchanged.
  Negative:
    - A single PDF path never sweeps in sibling PDFs from the same folder
      (the parent-directory-substitution bug this task fixes) -- the staged
      directory handed to the pipeline contains exactly the one PDF.
    - An already-enriched .xlsx/.xlsm workbook is rejected with a message
      pointing at Banks > Convert to GnuCash, and the OCR subprocess is
      never invoked.
    - A directory with no PDFs (but an enriched workbook) is rejected the
      same way, without a subprocess call.
    - An empty directory is rejected with "No PDF statements found".
    - A missing path is rejected with "Path not found".
    - An unsupported file type is rejected with "Unsupported file type".
    - Neither of the two file-path rejection messages above regresses to
      the old buggy "No PDFs found in <file path>" phrasing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import hsbc_fixture_gen as fixture_gen  # noqa: E402
import agents.skill_hsbc.agent as hsbc_agent  # noqa: E402
from agents.skill_hsbc.agent import run as hsbc_run  # noqa: E402


def _mock_subprocess(monkeypatch, returncode=0, stdout="Pipeline complete.", stderr=""):
    """Replace subprocess.run with a recorder; returns the list of recorded
    cmd argument lists (one per call)."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(hsbc_agent.subprocess, "run", fake_run)
    return calls


def _pdf_dir_arg(cmd: list[str]) -> str:
    idx = cmd.index("--pdf-dir")
    return cmd[idx + 1]


# --------------------------------------------------------------------------
# Positive
# --------------------------------------------------------------------------

def test_single_pdf_is_staged_and_passed_as_its_own_directory(monkeypatch, tmp_path):
    calls = _mock_subprocess(monkeypatch)
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    hsbc_run(str(pdf), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))

    assert len(calls) == 1
    staged_dir = Path(_pdf_dir_arg(calls[0]))
    assert [p.name for p in staged_dir.iterdir()] == ["statement.pdf"]


def test_directory_of_pdfs_passed_through_unchanged(monkeypatch, tmp_path):
    calls = _mock_subprocess(monkeypatch)
    stmt_dir = tmp_path / "statements"
    stmt_dir.mkdir()
    (stmt_dir / "a.pdf").write_bytes(b"%PDF-1.4 fake a")
    (stmt_dir / "b.pdf").write_bytes(b"%PDF-1.4 fake b")

    hsbc_run(str(stmt_dir), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))

    assert len(calls) == 1
    assert _pdf_dir_arg(calls[0]) == str(stmt_dir)


# --------------------------------------------------------------------------
# Negative
# --------------------------------------------------------------------------

def test_single_pdf_never_sweeps_in_sibling_pdfs(monkeypatch, tmp_path):
    """The exact defect this task fixes: a single-PDF upload must never be
    swapped for its parent directory, which would sweep in every sibling
    PDF and OCR them too."""
    calls = _mock_subprocess(monkeypatch)
    target = tmp_path / "target.pdf"
    target.write_bytes(b"%PDF-1.4 fake target")
    (tmp_path / "sibling_a.pdf").write_bytes(b"%PDF-1.4 fake a")
    (tmp_path / "sibling_b.pdf").write_bytes(b"%PDF-1.4 fake b")

    hsbc_run(str(target), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))

    assert len(calls) == 1
    staged_dir = Path(_pdf_dir_arg(calls[0]))
    assert sorted(p.name for p in staged_dir.iterdir()) == ["target.pdf"]


def test_enriched_workbook_is_rejected_with_convert_to_gnucash_pointer(monkeypatch, tmp_path):
    calls = _mock_subprocess(monkeypatch)
    wb = tmp_path / "enriched.xlsx"
    wb.write_bytes(fixture_gen.build_xlsx())

    with pytest.raises(ValueError) as exc_info:
        hsbc_run(str(wb), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))

    msg = str(exc_info.value)
    assert "Convert to GnuCash" in msg
    assert "No PDFs found in" not in msg
    assert calls == []  # OCR subprocess never invoked


def test_xlsm_workbook_is_also_rejected(monkeypatch, tmp_path):
    calls = _mock_subprocess(monkeypatch)
    wb = tmp_path / "enriched.xlsm"
    wb.write_bytes(fixture_gen.build_xlsx())

    with pytest.raises(ValueError) as exc_info:
        hsbc_run(str(wb), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))

    assert "Convert to GnuCash" in str(exc_info.value)
    assert calls == []


def test_directory_with_only_workbook_is_rejected_with_pointer(monkeypatch, tmp_path):
    calls = _mock_subprocess(monkeypatch)
    stmt_dir = tmp_path / "statements"
    stmt_dir.mkdir()
    (stmt_dir / "enriched.xlsx").write_bytes(fixture_gen.build_xlsx())

    with pytest.raises(ValueError) as exc_info:
        hsbc_run(str(stmt_dir), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))

    assert "Convert to GnuCash" in str(exc_info.value)
    assert calls == []


def test_empty_directory_is_rejected(monkeypatch, tmp_path):
    calls = _mock_subprocess(monkeypatch)
    stmt_dir = tmp_path / "statements"
    stmt_dir.mkdir()

    with pytest.raises(ValueError) as exc_info:
        hsbc_run(str(stmt_dir), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))

    assert "No PDF statements found" in str(exc_info.value)
    assert calls == []


def test_missing_path_is_rejected(monkeypatch, tmp_path):
    calls = _mock_subprocess(monkeypatch)
    missing = tmp_path / "does_not_exist.pdf"

    with pytest.raises(ValueError) as exc_info:
        hsbc_run(str(missing), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))

    assert "Path not found" in str(exc_info.value)
    assert calls == []


def test_unsupported_file_type_is_rejected(monkeypatch, tmp_path):
    calls = _mock_subprocess(monkeypatch)
    bogus = tmp_path / "statement.docx"
    bogus.write_bytes(b"not a statement")

    with pytest.raises(ValueError) as exc_info:
        hsbc_run(str(bogus), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))

    msg = str(exc_info.value)
    assert "Unsupported file type" in msg
    assert "No PDFs found in" not in msg
    assert calls == []
