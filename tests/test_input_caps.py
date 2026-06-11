"""
tests/test_input_caps.py — Regression guards for upload safety limits added
to ui/tabs/_generic.py (security tracker finding #7).

Tests verify:
  - _MAX_UPLOAD_SIZE_BYTES and _MAX_FILE_COUNT constants exist and are sane.
  - The count-cap constant is enforced: a file list longer than _MAX_FILE_COUNT
    should be rejected before staging.
  - The size-cap constant is enforced: a single oversized file should be rejected
    before it is copied into the staging directory.
  - Files within both limits stage successfully.

Because _make_run_handler returns a generator, these tests call the handler
directly and collect the yielded (markdown, download_update) pairs.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.tabs._generic import _MAX_FILE_COUNT, _MAX_UPLOAD_SIZE_BYTES


# ---------------------------------------------------------------------------
# Constant sanity checks
# ---------------------------------------------------------------------------

def test_max_upload_size_is_positive():
    assert _MAX_UPLOAD_SIZE_BYTES > 0


def test_max_upload_size_is_at_least_1mb():
    assert _MAX_UPLOAD_SIZE_BYTES >= 1 * 1024 * 1024


def test_max_upload_size_is_at_most_1gb():
    # Sanity upper bound — should not exceed 1 GB for a portable desktop app.
    assert _MAX_UPLOAD_SIZE_BYTES <= 1024 * 1024 * 1024


def test_max_file_count_is_positive():
    assert _MAX_FILE_COUNT > 0


def test_max_file_count_is_reasonable():
    # Should allow at least a few files but not unbounded.
    assert 1 <= _MAX_FILE_COUNT <= 100


# ---------------------------------------------------------------------------
# Helpers — build a minimal SkillInfo-like object and fake file descriptors
# ---------------------------------------------------------------------------

def _fake_file(path: Path) -> SimpleNamespace:
    """Simulate a Gradio file descriptor (has .name attribute)."""
    return SimpleNamespace(name=str(path))


def _make_minimal_skill(input_type: str = "files") -> Any:
    """
    Build a minimal SkillInfo-compatible namespace for a skill with a single
    'files' input.  Enough to exercise the file staging path in _make_run_handler.
    """
    inp = SimpleNamespace(
        name="input_files",
        type=input_type,
        label="Upload files",
        required=True,
        file_types=None,
        options=[],
    )
    output = SimpleNamespace(
        type="file",
        suffix="out",
        extension=".xlsx",
        download_label="Download",
    )
    requires = SimpleNamespace(native_binaries=[], external_tools=[])
    skill = SimpleNamespace(
        name="test_skill",
        display_name="Test Skill",
        description="",
        inputs=[inp],
        output=output,
        requires=requires,
        run_args={},
        mode="direct",
        entry_point="",
    )
    return skill


def _run_handler_to_list(skill, *args):
    """
    Run _make_run_handler(skill)(*args) fully and return all yielded
    (markdown, download_update) pairs.
    """
    from ui.tabs._generic import _make_run_handler
    handler = _make_run_handler(skill)
    return list(handler(*args))


# ---------------------------------------------------------------------------
# File count cap
# ---------------------------------------------------------------------------

def test_count_cap_rejects_too_many_files(tmp_path):
    """A file list longer than _MAX_FILE_COUNT should produce an error yield."""
    # Create _MAX_FILE_COUNT + 1 real tiny files.
    files = []
    for i in range(_MAX_FILE_COUNT + 1):
        f = tmp_path / f"file_{i:03d}.pdf"
        f.write_bytes(b"%PDF-1.4")
        files.append(_fake_file(f))

    skill = _make_minimal_skill("files")

    # Patch away the LLM health check and everything after staging
    # by checking that the generator yields an "Error: too many files" message
    # before it ever reaches the health check step.
    results = _run_handler_to_list(skill, files, "model-x")

    # The first non-"Validating" yield should be the count error.
    error_yields = [
        (md, dl) for md, dl in results
        if "too many files" in md.lower() or "maximum is" in md.lower()
    ]
    assert error_yields, (
        f"Expected an 'too many files' error for {_MAX_FILE_COUNT + 1} files, "
        f"got yields: {[md[:120] for md, _ in results]}"
    )


def test_count_cap_accepts_max_files(tmp_path):
    """Exactly _MAX_FILE_COUNT files should not trigger the count error."""
    files = []
    for i in range(_MAX_FILE_COUNT):
        f = tmp_path / f"file_{i:03d}.pdf"
        f.write_bytes(b"%PDF-1.4")
        files.append(_fake_file(f))

    skill = _make_minimal_skill("files")

    results = _run_handler_to_list(skill, files, "model-x")

    error_yields = [
        (md, dl) for md, dl in results
        if "too many files" in md.lower()
    ]
    assert not error_yields, (
        f"Unexpected count-error for exactly {_MAX_FILE_COUNT} files"
    )


# ---------------------------------------------------------------------------
# Per-file size cap
# ---------------------------------------------------------------------------

def test_size_cap_rejects_oversized_file(tmp_path):
    """A file larger than _MAX_UPLOAD_SIZE_BYTES should produce an error yield."""
    big_file = tmp_path / "huge.pdf"
    # Don't actually write 100 MB — mock stat().st_size instead.
    big_file.write_bytes(b"%PDF-1.4")  # real file so is_file() passes

    skill = _make_minimal_skill("files")

    oversized = _MAX_UPLOAD_SIZE_BYTES + 1

    original_stat = Path.stat

    def fake_stat(self, **kwargs):
        result = original_stat(self, **kwargs)
        if self == big_file:
            # Return a mock with cbData = oversized
            mock = MagicMock()
            mock.st_size = oversized
            # forward other attrs
            for attr in ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid",
                         "st_atime", "st_mtime", "st_ctime"):
                setattr(mock, attr, getattr(result, attr, 0))
            return mock
        return result

    with patch.object(Path, "stat", fake_stat):
        results = _run_handler_to_list(skill, [_fake_file(big_file)], "model-x")

    error_yields = [
        (md, dl) for md, dl in results
        if "too large" in md.lower() or "maximum is" in md.lower()
    ]
    assert error_yields, (
        f"Expected a 'too large' error for a {oversized}-byte file, "
        f"got: {[md[:120] for md, _ in results]}"
    )


def test_size_cap_accepts_normal_file(tmp_path):
    """A file well within the size limit should not trigger the size error."""
    normal = tmp_path / "normal.pdf"
    normal.write_bytes(b"%PDF-1.4 small content")

    skill = _make_minimal_skill("files")

    results = _run_handler_to_list(skill, [_fake_file(normal)], "model-x")

    error_yields = [
        (md, dl) for md, dl in results
        if "too large" in md.lower()
    ]
    assert not error_yields, (
        f"Unexpected size-error for a tiny file: {[md[:120] for md, _ in results]}"
    )


# ---------------------------------------------------------------------------
# Both caps are enforced independently
# ---------------------------------------------------------------------------

def test_count_checked_before_size(tmp_path):
    """
    If both count AND size violations exist, the count error fires first
    (count check runs before the per-file loop).
    """
    files = []
    for i in range(_MAX_FILE_COUNT + 1):
        f = tmp_path / f"f_{i}.pdf"
        f.write_bytes(b"%PDF")
        files.append(_fake_file(f))

    skill = _make_minimal_skill("files")

    # Override stat to report all files as oversized too.
    original_stat = Path.stat

    def always_oversized(self, **kwargs):
        result = original_stat(self, **kwargs)
        mock = MagicMock()
        mock.st_size = _MAX_UPLOAD_SIZE_BYTES + 1
        return mock

    with patch.object(Path, "stat", always_oversized):
        results = _run_handler_to_list(skill, files, "model-x")

    markdowns = [md for md, _ in results]
    # Count error should appear; "too large" should NOT (count fires first and returns)
    assert any("too many files" in md.lower() for md in markdowns), markdowns
    assert not any("too large" in md.lower() for md in markdowns), markdowns
