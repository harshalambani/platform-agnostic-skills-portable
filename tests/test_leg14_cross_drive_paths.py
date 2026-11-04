"""tests/test_leg14_cross_drive_paths.py -- LEG-14: build log lines must not crash when the destination
is on another drive (os.path.relpath raises ValueError across drives). Synthetic trees only."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bundling import build as pask_build  # noqa: E402


class _Cap:
    def __init__(self):
        self.lines = []

    def __getattr__(self, name):
        return lambda msg="", *a, **k: self.lines.append(str(msg))


def _vendor(tmp_path):
    src = tmp_path / "vendor_src"
    (src / "sub").mkdir(parents=True)
    (src / "a.bin").write_bytes(b"1")
    (src / "sub" / "b.bin").write_bytes(b"2")
    return src


def _cross_drive(*a, **k):
    raise ValueError("path is on mount 'C:', start on mount 'D:'")


def test_helper_returns_the_relative_path_on_the_same_drive():
    p = pask_build.PROJECT_ROOT / "staging" / "x"
    assert pask_build._display_path(p) == os.path.relpath(p, pask_build.PROJECT_ROOT)


def test_helper_falls_back_to_the_path_on_another_drive(monkeypatch, tmp_path):
    monkeypatch.setattr(os.path, "relpath", _cross_drive)
    assert pask_build._display_path(tmp_path) == str(tmp_path)


def test_copy_vendor_subtree_does_not_raise_across_drives(tmp_path, monkeypatch):
    src, dest, log = _vendor(tmp_path), tmp_path / "out" / "dest", _Cap()
    monkeypatch.setattr(os.path, "relpath", _cross_drive)
    assert pask_build._copy_vendor_subtree(src, dest, "synthetic", log) == 2
    line = next(x for x in log.lines if "files copied to" in x)
    assert str(dest) in line and "2 files copied" in line                       # the absolute dest path


def test_copy_vendor_subtree_same_drive_still_logs_the_relative_path(tmp_path, monkeypatch):   # NEGATIVE
    src, log = _vendor(tmp_path), _Cap()
    monkeypatch.setattr(pask_build, "PROJECT_ROOT", tmp_path)
    dest = tmp_path / "staging" / "dest"
    assert pask_build._copy_vendor_subtree(src, dest, "synthetic", log) == 2
    line = next(x for x in log.lines if "files copied to" in x)
    assert line.endswith(os.path.join("staging", "dest"))
    assert str(tmp_path) not in line                                            # relative, not absolute


def test_no_log_line_calls_relpath_or_relative_to_project_root_directly():      # NEGATIVE
    text = (ROOT / "bundling" / "build.py").read_text(encoding="utf-8")
    assert "os.path.relpath(dest, PROJECT_ROOT)" not in text
    assert ".relative_to(PROJECT_ROOT)" not in text
