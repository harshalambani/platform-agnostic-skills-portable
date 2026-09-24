"""
tests/test_hsbc_input_routing_pipeline.py -- HSB-03: the "Convert to
GnuCash" pipeline's HSBC input-shaping (Route A).

Route A defect (see agent.py's `_resolve_hsbc_input` docstring): the HSBC
branch used a staged upload directory as-is. A single-file upload (a lone
PDF, or an already-enriched .xlsx) was silently replaced by its PARENT
directory, so every sibling PDF in that folder got swept into OCR. And
because HSBCSkill.parse() globs `*.pdf` on a directory, a staged .xlsx
upload always ended in "No PDFs found in <dir>" -- the .xlsx/.xlsm fast
path in parse() could never be reached from the UI.

Covers, all offline / synthetic-only (openpyxl fixtures via
tests/skill_hsbc/hsbc_fixture_gen.py, no real bank data):
  Positive:
    - A staged directory holding exactly one HSBC-shaped .xlsx resolves to
      that workbook's own path (not the directory), and the real
      HSBCSkill.parse() successfully reads rows from it.
    - A staged directory holding only PDFs resolves unchanged (the
      directory itself), preserving HSBC's existing multi-statement
      consolidation behaviour.
    - A single file (PDF or workbook) passed directly (not as a directory)
      resolves to itself.
    - run() wiring: a staged HSBC workbook directory reaches
      HSBCSkill.parse() as the workbook path, not the directory.
  Negative:
    - A single PDF file passed directly never has its parent directory
      substituted, even when sibling PDFs are present alongside it.
    - A non-HSBC-shaped .xlsx (HSBCSkill.detect() == 0) is rejected with a
      clear message and never reaches parse().
    - A directory mixing PDFs and a workbook is rejected -- nothing is
      silently chosen.
    - A directory with two workbooks is rejected -- nothing is silently
      chosen.
    - An empty directory (no PDFs, no workbook) is rejected.
    - run() wiring for Bank of Baroda and ICICI is unaffected by the HSBC
      change (same dispatch path, same behaviour as before HSB-03).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT / "tests" / "skill_hsbc") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests" / "skill_hsbc"))

import hsbc_fixture_gen as fixture_gen  # noqa: E402
import agents.skill_gnucash_pipeline.agent as pipeline_agent  # noqa: E402
from agents.skill_gnucash_pipeline.agent import _resolve_hsbc_input  # noqa: E402
from agents.banks import BankInfo  # noqa: E402
from agents.skill_hsbc.agent import HSBCSkill  # noqa: E402


class _StubHsbcSkill:
    """A minimal stand-in exposing only what `_resolve_hsbc_input` needs
    (`.detect()`), so the workbook-shape tests don't depend on the real
    detector's implementation."""

    def __init__(self, confidence: float):
        self._confidence = confidence

    def detect(self, path):
        return self._confidence


_REAL_HSBC_SKILL = HSBCSkill()


# --------------------------------------------------------------------------
# _resolve_hsbc_input -- positive
# --------------------------------------------------------------------------

def test_single_pdf_file_resolves_to_itself(tmp_path):
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    resolved, error = _resolve_hsbc_input(str(pdf), _StubHsbcSkill(0.9))

    assert error is None
    assert resolved == str(pdf)


def test_single_workbook_file_resolves_to_itself(tmp_path):
    wb = tmp_path / "enriched.xlsx"
    wb.write_bytes(fixture_gen.build_xlsx())

    resolved, error = _resolve_hsbc_input(str(wb), _StubHsbcSkill(0.9))

    assert error is None
    assert resolved == str(wb)


def test_directory_of_pdfs_only_resolves_unchanged(tmp_path):
    stmt_dir = tmp_path / "statements"
    stmt_dir.mkdir()
    (stmt_dir / "a.pdf").write_bytes(b"%PDF-1.4 fake a")
    (stmt_dir / "b.pdf").write_bytes(b"%PDF-1.4 fake b")

    resolved, error = _resolve_hsbc_input(str(stmt_dir), _StubHsbcSkill(0.9))

    assert error is None
    assert resolved == str(stmt_dir)


def test_directory_with_one_hsbc_shaped_workbook_resolves_to_workbook(tmp_path):
    stmt_dir = tmp_path / "statements"
    stmt_dir.mkdir()
    wb = stmt_dir / "enriched.xlsx"
    wb.write_bytes(fixture_gen.build_xlsx())

    # Real detector: the fixture is genuinely HSBC-shaped (Transaction
    # Details + Withdrawals headers), so detect() must score it > 0.
    resolved, error = _resolve_hsbc_input(str(stmt_dir), _REAL_HSBC_SKILL)

    assert error is None
    assert resolved == str(wb)


def test_resolved_hsbc_workbook_actually_parses_via_real_skill(tmp_path):
    """End-to-end for the fast path: a staged directory holding one
    HSBC-shaped workbook resolves to that workbook, and HSBCSkill.parse()
    reads real rows from it -- no OCR involved."""
    stmt_dir = tmp_path / "statements"
    stmt_dir.mkdir()
    (stmt_dir / "enriched.xlsx").write_bytes(fixture_gen.build_xlsx())

    resolved, error = _resolve_hsbc_input(str(stmt_dir), _REAL_HSBC_SKILL)
    assert error is None

    result = _REAL_HSBC_SKILL.parse(resolved)
    assert result.row_count == len(fixture_gen.SYN_ROWS)
    assert result.rows[0]["Description"] == "BALANCE BROUGHT FORWARD"


# --------------------------------------------------------------------------
# _resolve_hsbc_input -- negative
# --------------------------------------------------------------------------

def test_single_pdf_file_never_substitutes_parent_directory(tmp_path):
    """The exact defect this task fixes: a lone PDF passed as a FILE path
    must resolve to itself, never to its parent directory, even though the
    parent directory also contains sibling PDFs."""
    target = tmp_path / "target.pdf"
    target.write_bytes(b"%PDF-1.4 fake target")
    (tmp_path / "sibling_a.pdf").write_bytes(b"%PDF-1.4 fake a")
    (tmp_path / "sibling_b.pdf").write_bytes(b"%PDF-1.4 fake b")

    resolved, error = _resolve_hsbc_input(str(target), _StubHsbcSkill(0.9))

    assert error is None
    assert resolved == str(target)
    assert resolved != str(tmp_path)


def test_non_hsbc_shaped_workbook_is_rejected_and_never_parsed(tmp_path):
    stmt_dir = tmp_path / "statements"
    stmt_dir.mkdir()
    wb = stmt_dir / "not_hsbc.xlsx"
    wb.write_bytes(fixture_gen.build_xlsx())

    resolved, error = _resolve_hsbc_input(str(stmt_dir), _StubHsbcSkill(0.0))

    assert resolved is None
    assert error is not None
    assert "Date, Transaction Details" in error or "Transaction Details" in error


def test_mixed_pdfs_and_workbook_is_rejected(tmp_path):
    stmt_dir = tmp_path / "statements"
    stmt_dir.mkdir()
    (stmt_dir / "a.pdf").write_bytes(b"%PDF-1.4 fake a")
    (stmt_dir / "enriched.xlsx").write_bytes(fixture_gen.build_xlsx())

    resolved, error = _resolve_hsbc_input(str(stmt_dir), _StubHsbcSkill(0.9))

    assert resolved is None
    assert error is not None
    assert "mixed upload" in error.lower()


def test_two_workbooks_is_rejected(tmp_path):
    stmt_dir = tmp_path / "statements"
    stmt_dir.mkdir()
    (stmt_dir / "one.xlsx").write_bytes(fixture_gen.build_xlsx())
    (stmt_dir / "two.xlsx").write_bytes(fixture_gen.build_xlsx())

    resolved, error = _resolve_hsbc_input(str(stmt_dir), _StubHsbcSkill(0.9))

    assert resolved is None
    assert error is not None
    assert "multiple workbooks" in error.lower()


def test_empty_directory_is_rejected(tmp_path):
    stmt_dir = tmp_path / "statements"
    stmt_dir.mkdir()

    resolved, error = _resolve_hsbc_input(str(stmt_dir), _StubHsbcSkill(0.9))

    assert resolved is None
    assert error is not None
    assert "no .pdf or .xlsx" in error.lower()


# --------------------------------------------------------------------------
# run() wiring -- HSBC gets the fixed routing; BoB / ICICI are unaffected
# --------------------------------------------------------------------------
#
# The pipeline's run() does a lot after Step 1 (account mapping against a
# real .gnucash book), which is out of scope here. To isolate just the
# input-shaping/dispatch behaviour, the fake bank skill's parse() records
# the path it was called with and then raises a sentinel exception, which
# run()'s own Step-1 try/except turns into an early "## {bank} → extraction
# error" return -- so these tests never need a real .gnucash file.

class _RecordingSkill:
    def __init__(self, detect_confidence: float = 0.9):
        self.received_path = None
        self._detect_confidence = detect_confidence

    def detect(self, path):
        return self._detect_confidence

    def parse(self, path, password=None):
        self.received_path = str(path)
        raise RuntimeError("test-sentinel-stop")


def _run_and_capture(monkeypatch, tmp_path, bank: str, statement_files: str):
    skill = _RecordingSkill()
    bank_info = BankInfo(bank_key=bank.lower(), display_name=bank, skill_name=bank, package="agents.skill_stub")

    monkeypatch.setattr(pipeline_agent, "discover_banks", lambda: [bank_info])
    monkeypatch.setattr(pipeline_agent, "load_bank_skill", lambda info: skill)

    result = pipeline_agent.run(
        bank=bank,
        statement_files=statement_files,
        gnucash_file=str(tmp_path / "nonexistent.gnucash"),
        output_path=str(tmp_path / "out.csv"),
    )
    return skill, result


def test_run_routes_hsbc_workbook_directory_to_parse_as_workbook_path(monkeypatch, tmp_path):
    upload_dir = tmp_path / "upload"
    upload_dir.mkdir()
    wb = upload_dir / "enriched.xlsx"
    wb.write_bytes(fixture_gen.build_xlsx())

    skill, result = _run_and_capture(monkeypatch, tmp_path, "HSBC", str(upload_dir))

    assert skill.received_path == str(wb)
    assert "extraction error" in result


def test_run_routes_hsbc_single_pdf_file_unchanged(monkeypatch, tmp_path):
    target = tmp_path / "upload" / "target.pdf"
    target.parent.mkdir()
    target.write_bytes(b"%PDF-1.4 fake")
    (target.parent / "sibling.pdf").write_bytes(b"%PDF-1.4 fake sibling")

    skill, result = _run_and_capture(monkeypatch, tmp_path, "HSBC", str(target))

    assert skill.received_path == str(target)
    assert "extraction error" in result


def test_run_bank_of_baroda_dispatch_is_unaffected(monkeypatch, tmp_path):
    """BoB's own branch (an inline, no-PDFs-found guard just below HSBC's)
    must behave exactly as before HSB-03: an empty upload directory is
    rejected before parse() is ever called."""
    stmt_dir = tmp_path / "upload"
    stmt_dir.mkdir()

    skill, result = _run_and_capture(monkeypatch, tmp_path, "Bank of Baroda", str(stmt_dir))

    assert skill.received_path is None  # parse() never reached
    assert "no PDFs found" in result


def test_run_icici_dispatch_reaches_parse_with_resolved_file(monkeypatch, tmp_path):
    """ICICI's branch (_resolve_single_file, untouched by HSB-03) must
    still resolve a staged directory to the one matching file inside it."""
    stmt_dir = tmp_path / "upload"
    stmt_dir.mkdir()
    xls = stmt_dir / "statement.xls"
    xls.write_bytes(b"fake xls bytes")

    skill, result = _run_and_capture(monkeypatch, tmp_path, "ICICI", str(stmt_dir))

    assert skill.received_path == str(xls)
    assert "extraction error" in result
