"""BNK-12 -- no scratch folder holding statement content outlives the run.

The HSBC reader (_run_ocr_pipeline) built a hsbc_bankskill_* folder holding a
copy of the PDF, page images, OCR text and the built workbook, and never
removed it; the BoB batch run() did the same with pa-skills-bob-batch-* (one
CSV of statement rows per PDF). Both now remove the folder on every exit.

All fixtures are synthetic. tempfile.mkdtemp is pointed at a folder under
pytest's tmp_path, so these tests never look at the real %TEMP%.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agents.skill_bob import agent as bob  # noqa: E402
from agents.skill_hsbc import agent as hsbc  # noqa: E402

HSBC_NO = "111222222333"
_HEAD = ["Date", "Transaction Details", "Value Date", "Transaction Number",
         "Extra Information", "Deposit", "Withdrawals", "Balance"]
ROWS = [(date(2025, 1, 1), "BALANCE BROUGHT FORWARD", None, "", "", None, None, 1000.0),
        (date(2025, 1, 5), "SYN SALARY", date(2025, 1, 5), "T1", "", 500.0, None, 1500.0)]


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    t = tmp_path / "scratch"
    t.mkdir()
    real = tempfile.mkdtemp

    def _mk(suffix=None, prefix=None, dir=None):
        return real(suffix=suffix, prefix=prefix, dir=str(t))

    monkeypatch.setattr(tempfile, "mkdtemp", _mk)
    return t


def _left(scratch):
    return sorted(p.name for p in scratch.iterdir())


def _workbook(path, rows=ROWS, number=HSBC_NO):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "HSBC Savings"
    ws.append(_HEAD)
    for r in rows:
        ws.append(list(r))
    s = wb.create_sheet("Summary")
    s.append(["Metric", "Value"])
    s.append(["Account Number", number])
    wb.save(path)


def _ocr_stub(monkeypatch, rc=0, stderr="", rows=ROWS, raises=None):
    """Replace the OCR subprocess: drops the files the real pipeline leaves in
    its work folder (page image, OCR text) and, on success, the built workbook."""
    seen = {}

    def fake(cmd, **kw):
        if raises:
            raise raises
        work = Path(cmd[cmd.index("--work-dir") + 1])
        out = Path(cmd[cmd.index("--out") + 1])
        work.mkdir(parents=True, exist_ok=True)
        (work / "page1.png").write_bytes(b"png")
        (work / "page1.txt").write_text("SYN OCR TEXT", encoding="utf-8")
        seen["out"] = out
        seen["pdf_copy_existed"] = any(Path(cmd[cmd.index("--pdf-dir") + 1]).glob("*.pdf"))
        if rc == 0:
            _workbook(out, rows)
        return subprocess.CompletedProcess(cmd, rc, stdout="done", stderr=stderr)

    monkeypatch.setattr(hsbc.subprocess, "run", fake)
    return seen


@pytest.fixture
def pdf(tmp_path):
    up = tmp_path / "up"
    up.mkdir()
    p = up / "stmt.pdf"
    p.write_bytes(b"%PDF-1.4 synthetic")
    return p


# ---- HSBC ---------------------------------------------------------------

def test_a_good_run_reads_rows_and_number_then_leaves_nothing(pdf, scratch, monkeypatch):
    seen = _ocr_stub(monkeypatch)
    res = hsbc.HSBCSkill().parse_one(pdf)
    assert [r["Description"] for r in res.rows] == ["BALANCE BROUGHT FORWARD", "SYN SALARY"]
    assert res.meta.account_number == HSBC_NO          # taken before the folder went
    assert seen["pdf_copy_existed"]                    # NEGATIVE: not removed before use
    assert _left(scratch) == []


@pytest.mark.parametrize("name,kw,exc", [
    ("subprocess raises (OCR cannot start)", dict(raises=OSError("cannot start")), OSError),
    ("bad PDF (non-zero exit)", dict(rc=1, stderr="not a pdf"), RuntimeError),
    ("wrong password", dict(rc=1, stderr="PDFPasswordIncorrect"), ValueError),
    ("interrupted", dict(raises=KeyboardInterrupt()), KeyboardInterrupt),
])
def test_a_failing_run_leaves_nothing(pdf, scratch, monkeypatch, name, kw, exc):
    _ocr_stub(monkeypatch, **kw)
    with pytest.raises(exc):
        hsbc.HSBCSkill().parse_one(pdf, "pw" if "password" in name else None)
    assert _left(scratch) == [], name


def test_a_workbook_with_no_rows_still_leaves_nothing(pdf, scratch, monkeypatch):
    _ocr_stub(monkeypatch, rows=[])
    with pytest.raises(ValueError, match="No transaction rows"):
        hsbc.HSBCSkill().parse_one(pdf)
    assert _left(scratch) == []


def test_clean_up_never_touches_the_input_folder_or_other_outputs(pdf, tmp_path, scratch, monkeypatch):
    other_out = tmp_path / "outputs"
    other_out.mkdir()
    (other_out / "keep.csv").write_text("kept", encoding="utf-8")
    (pdf.parent / "sibling.pdf").write_bytes(b"%PDF-1.4 sibling")
    _ocr_stub(monkeypatch)
    hsbc.HSBCSkill().parse_one(pdf)
    _ocr_stub(monkeypatch, rc=1, stderr="x")
    with pytest.raises(RuntimeError):
        hsbc.HSBCSkill().parse_one(pdf)
    assert sorted(p.name for p in pdf.parent.iterdir()) == ["sibling.pdf", "stmt.pdf"]
    assert pdf.read_bytes() == b"%PDF-1.4 synthetic"
    assert (other_out / "keep.csv").read_text(encoding="utf-8") == "kept"


def test_a_user_supplied_workbook_and_its_folder_are_never_removed(tmp_path, scratch):
    up = tmp_path / "wb"
    up.mkdir()
    wb = up / "enriched.xlsx"             # same file name the scratch workbook has
    _workbook(wb)
    res = hsbc.HSBCSkill().parse_one(wb)
    assert res.meta.account_number == HSBC_NO
    assert wb.is_file() and up.is_dir()


def test_discard_scratch_only_removes_a_folder_this_module_made(tmp_path):
    foreign = tmp_path / "somewhere"
    foreign.mkdir()
    (foreign / "enriched.xlsx").write_bytes(b"x")
    hsbc.discard_scratch(foreign / "enriched.xlsx")
    assert (foreign / "enriched.xlsx").is_file()          # wrong folder name: kept
    mine = tmp_path / (hsbc._SCRATCH_PREFIX + "abc")
    mine.mkdir()
    (mine / "other.xlsx").write_bytes(b"x")
    hsbc.discard_scratch(mine / "other.xlsx")
    assert (mine / "other.xlsx").is_file()                # wrong file name: kept
    (mine / "enriched.xlsx").write_bytes(b"x")
    hsbc.discard_scratch(mine / "enriched.xlsx")
    assert not mine.exists()


def test_the_run_pipeline_failure_path_removes_its_own_folder(pdf, scratch, monkeypatch):
    _ocr_stub(monkeypatch, rc=1, stderr="boom")
    with pytest.raises(RuntimeError):
        hsbc._run_ocr_pipeline([pdf])
    assert _left(scratch) == []


# ---- BoB batch ----------------------------------------------------------

def _bob_stub(monkeypatch, fail_on=None, write=True):
    def fake_single(p, csv_out):
        if fail_on and p.name == fail_on:
            raise RuntimeError("boom")
        if write:
            csv_out.write_text("DATE,NARRATION,WITHDRAWALS,DEPOSITS,BALANCE\n"
                               "01-01-25,SYN,0,10,10\n", encoding="utf-8")
        return "ok"
    monkeypatch.setattr(bob, "_run_single", fake_single)


@pytest.fixture
def bob_in(tmp_path):
    d = tmp_path / "bobin"
    d.mkdir()
    for n in ("a.pdf", "b.pdf"):
        (d / n).write_bytes(b"%PDF-1.4 " + n.encode())
    return d


def test_bob_batch_leaves_no_per_pdf_csv_after_a_good_run(bob_in, tmp_path, scratch, monkeypatch):
    _bob_stub(monkeypatch)
    out = tmp_path / "out.csv"
    msg = bob.run(str(bob_in), str(out))
    assert "Batch complete" in msg
    assert out.is_file() and "SYN" in out.read_text(encoding="utf-8")   # NEGATIVE: output kept
    assert _left(scratch) == []
    assert sorted(p.name for p in bob_in.iterdir()) == ["a.pdf", "b.pdf"]  # input untouched


def test_bob_batch_leaves_nothing_when_a_pdf_raises(bob_in, tmp_path, scratch, monkeypatch):
    _bob_stub(monkeypatch, fail_on="b.pdf")
    with pytest.raises(RuntimeError):
        bob.run(str(bob_in), str(tmp_path / "out.csv"))
    assert _left(scratch) == []
    assert sorted(p.name for p in bob_in.iterdir()) == ["a.pdf", "b.pdf"]


def test_bob_batch_leaves_nothing_when_no_csv_was_produced(bob_in, tmp_path, scratch, monkeypatch):
    _bob_stub(monkeypatch, write=False)
    msg = bob.run(str(bob_in), str(tmp_path / "out.csv"))
    assert msg.startswith("ERROR")
    assert _left(scratch) == []
