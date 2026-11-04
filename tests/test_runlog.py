"""
tests/test_runlog.py — unit tests for ui/_runlog.py (Defect 3).

Before this module existed, run failures surfaced only as bare exception
fragments in the UI, and the on-disk warnings.log held only Gradio's own
noise — the HDFC .format() crash and the HSBC OCR ZeroDivisionError were
both invisible for exactly this reason. These tests cover the two
building blocks: allocating a collision-free timestamped path per skill,
and writing a log that is never allowed to raise (logging must not be
the thing that breaks a run).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui import _runlog


def test_new_log_path_lands_under_data_logs(tmp_path):
    with patch("ui._config.data_root_dir", return_value=tmp_path):
        path = _runlog.new_log_path("skill_hsbc")
    assert path.parent == tmp_path / "logs"
    assert path.parent.is_dir()  # mkdir'd eagerly
    assert path.name.endswith("-skill_hsbc.log")


def test_new_log_path_sanitizes_unsafe_characters(tmp_path):
    with patch("ui._config.data_root_dir", return_value=tmp_path):
        path = _runlog.new_log_path("Bank / HSBC (agent)")
    # No path separators or parens should survive into the filename.
    assert "/" not in path.name and "\\" not in path.name
    assert "(" not in path.name and ")" not in path.name


def test_write_run_log_includes_log_lines_and_traceback(tmp_path):
    path = tmp_path / "run.log"
    _runlog.write_run_log(
        path,
        skill_name="skill_bob",
        run_log_lines=["step 1: parsed 10 rows", "step 2: reconciled"],
        traceback_text="Traceback (most recent call last):\nValueError: boom",
    )
    text = path.read_text(encoding="utf-8")
    assert "skill_bob" in text
    assert "step 1: parsed 10 rows" in text
    assert "step 2: reconciled" in text
    assert "=== Traceback ===" in text
    assert "ValueError: boom" in text


def test_write_run_log_without_traceback_omits_section(tmp_path):
    path = tmp_path / "run.log"
    _runlog.write_run_log(
        path, skill_name="skill_bob", run_log_lines=["ok"], traceback_text=None,
    )
    text = path.read_text(encoding="utf-8")
    assert "=== Traceback ===" not in text


def test_write_run_log_empty_lines_still_produces_valid_file(tmp_path):
    path = tmp_path / "run.log"
    _runlog.write_run_log(path, skill_name="skill_bob", run_log_lines=[])
    text = path.read_text(encoding="utf-8")
    assert "(empty)" in text


def test_write_run_log_never_raises_on_bad_path():
    # A path under a nonexistent, un-creatable parent should be swallowed,
    # not propagate — logging must never be the thing that breaks a run.
    bad_path = Path("Z:/definitely/not/a/real/drive/run.log")
    _runlog.write_run_log(bad_path, skill_name="skill_bob", run_log_lines=["x"])
