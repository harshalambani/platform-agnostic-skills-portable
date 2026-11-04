"""BNK-08 -- HSBC run() removes its staged hsbc_run_* folder when the run ends."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from agents.skill_hsbc import agent  # noqa: E402


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    t = tmp_path / "tmp"
    t.mkdir()
    # tests/conftest.py already reroutes the hsbc_run_ prefix under pytest's
    # base temp; override that here so leftovers are counted in a folder of
    # our own.
    real = tempfile.mkdtemp

    def _mk(suffix=None, prefix=None, dir=None):
        return real(suffix=suffix, prefix=prefix, dir=str(t))

    monkeypatch.setattr(tempfile, "mkdtemp", _mk)
    return t


def _leftovers(scratch):
    return sorted(p.name for p in scratch.glob("hsbc_run_*"))


def _pdf(tmp_path):
    p = tmp_path / "stmt.pdf"
    p.write_bytes(b"%PDF-1.4 synthetic")
    return p


def _fake_run(seen, rc=0, write_out=True):
    def fake(cmd, **kw):
        pdf_dir = Path(cmd[cmd.index("--pdf-dir") + 1])
        seen["pdf_dir"] = pdf_dir
        seen["pdf_existed_during_run"] = (pdf_dir / "stmt.pdf").is_file()
        out = Path(cmd[cmd.index("--out") + 1])
        if write_out and rc == 0:
            out.write_text("parsed", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, rc, stdout="done", stderr="boom" if rc else "")
    return fake


def test_staged_folder_is_removed_after_a_good_run_and_the_output_is_kept(tmp_path, scratch, monkeypatch):
    seen = {}
    monkeypatch.setattr(agent.subprocess, "run", _fake_run(seen))
    out = tmp_path / "out.xlsx"
    agent.run(str(_pdf(tmp_path)), str(tmp_path / "work"), str(out))
    assert seen["pdf_existed_during_run"]                 # NEGATIVE: not deleted before it is used
    assert _leftovers(scratch) == []                      # NEGATIVE: nothing left behind
    assert out.read_text(encoding="utf-8") == "parsed"    # the parsed output the import needs survives


def test_staged_folder_is_removed_after_a_failed_run(tmp_path, scratch, monkeypatch):
    seen = {}
    monkeypatch.setattr(agent.subprocess, "run", _fake_run(seen, rc=1))
    with pytest.raises(RuntimeError, match="HSBC pipeline failed"):
        agent.run(str(_pdf(tmp_path)), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))
    assert seen["pdf_existed_during_run"]
    assert _leftovers(scratch) == []                      # NEGATIVE: not left on failure


def test_staged_folder_is_removed_when_the_subprocess_raises(tmp_path, scratch, monkeypatch):
    def boom(cmd, **kw):
        raise OSError("cannot start")
    monkeypatch.setattr(agent.subprocess, "run", boom)
    with pytest.raises(OSError):
        agent.run(str(_pdf(tmp_path)), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))
    assert _leftovers(scratch) == []


def test_a_folder_input_is_never_touched(tmp_path, scratch, monkeypatch):
    d = tmp_path / "pdfs"
    d.mkdir()
    (d / "stmt.pdf").write_bytes(b"%PDF-1.4 synthetic")
    seen = {}
    monkeypatch.setattr(agent.subprocess, "run", _fake_run(seen))
    agent.run(str(d), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))
    assert (d / "stmt.pdf").is_file()                     # NEGATIVE: the user's own folder is not removed
    assert seen["pdf_dir"] == d


def test_the_original_single_pdf_is_never_removed(tmp_path, scratch, monkeypatch):
    pdf = _pdf(tmp_path)
    monkeypatch.setattr(agent.subprocess, "run", _fake_run({}))
    agent.run(str(pdf), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))
    assert pdf.is_file()                                  # NEGATIVE: only the staged copy goes
