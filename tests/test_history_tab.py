"""
tests/test_history_tab.py — unit tests for the History tab (C5).

Tests cover:
    - Filename parsing (timestamp extraction, skill inference)
    - Sorting order (newest first, no-timestamp at end)
    - Empty directory handling
    - Delete safety (refuse to delete outside outputs dir)
    - Human-readable size formatting
    - Table data building
    - Edge cases (no timestamp, unknown suffix, dotfiles skipped)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Mock gradio before importing history — gradio is not required for the
# pure-logic functions under test and may not be installed in CI / bare
# Python environments.
# ---------------------------------------------------------------------------
try:
    import gradio  # noqa: F401 — real import; leave sys.modules alone
except ImportError:
    _fake_gr = ModuleType("gradio")
    sys.modules["gradio"] = _fake_gr


# ---------------------------------------------------------------------------
# Helpers — import under test.
# ---------------------------------------------------------------------------

# We need to mock _config before importing history, because history.py
# imports _config at module level for output_dir().  Instead we import
# the individual functions directly and mock at call sites.

from ui.tabs.history import (
    OutputEntry,
    _human_size,
    _infer_skill,
    _TS_FMT,
    _TS_RE,
    delete_output,
    parse_output_entry,
    scan_outputs,
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_outputs(tmp_path):
    """Create a temporary outputs directory with sample files."""
    out = tmp_path / "outputs"
    out.mkdir()
    return out


def _touch(directory: Path, name: str, content: bytes = b"x" * 100) -> Path:
    """Create a file with some content in the given directory."""
    p = directory / name
    p.write_bytes(content)
    return p


def _mkdir(directory: Path, name: str) -> Path:
    """Create a subdirectory with a dummy file inside."""
    d = directory / name
    d.mkdir()
    (d / "output.xlsx").write_bytes(b"x" * 200)
    return d


# ---------------------------------------------------------------------------
# Tests: _human_size.
# ---------------------------------------------------------------------------

class TestHumanSize:
    def test_bytes(self):
        assert _human_size(512) == "512 B"

    def test_zero(self):
        assert _human_size(0) == "0 B"

    def test_kilobytes(self):
        result = _human_size(2048)
        assert "KB" in result

    def test_megabytes(self):
        result = _human_size(5 * 1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self):
        result = _human_size(3 * 1024 ** 3)
        assert "GB" in result


# ---------------------------------------------------------------------------
# Tests: _infer_skill.
# ---------------------------------------------------------------------------

class TestInferSkill:
    def test_known_suffix_26as(self):
        assert _infer_skill("myfile-26AS.xlsx") == "Form 26AS Extractor"

    def test_known_suffix_bob(self):
        assert _infer_skill("statement-BoB.csv") == "Bank of Baroda"

    def test_known_suffix_analysis(self):
        assert _infer_skill("sales-analysis.md") == "CSV Analyzer"

    def test_known_suffix_summary(self):
        assert _infer_skill("report-summary.md") == "Summarizer"

    def test_known_suffix_translation(self):
        assert _infer_skill("text-translation.txt") == "Translator"

    def test_known_suffix_hsbc(self):
        assert _infer_skill("data-HSBC.xlsx") == "HSBC Cleanup"

    def test_known_suffix_cc_sort_dir(self):
        # Directory outputs have just the suffix as the name.
        assert _infer_skill("CC-Sort") == "CC Sort"

    def test_known_suffix_cc_transactions(self):
        assert _infer_skill("merged-CC-Transactions.xlsx") == "CC Transactions"

    def test_unknown_suffix(self):
        assert _infer_skill("random-file.pdf") == "Unknown"

    def test_bare_suffix_no_ext(self):
        assert _infer_skill("analysis") == "CSV Analyzer"


# ---------------------------------------------------------------------------
# Tests: _TS_RE regex.
# ---------------------------------------------------------------------------

class TestTimestampRegex:
    def test_valid_timestamp(self):
        m = _TS_RE.match("2026-06-03-143025-sales-analysis.md")
        assert m is not None
        assert m.group(1) == "2026-06-03-143025"
        assert m.group(2) == "sales-analysis.md"

    def test_no_timestamp(self):
        assert _TS_RE.match("sales-analysis.md") is None

    def test_directory_name(self):
        m = _TS_RE.match("2026-06-03-143025-CC-Sort")
        assert m is not None
        assert m.group(2) == "CC-Sort"


# ---------------------------------------------------------------------------
# Tests: parse_output_entry.
# ---------------------------------------------------------------------------

class TestParseOutputEntry:
    def test_file_with_timestamp(self, tmp_outputs):
        f = _touch(tmp_outputs, "2026-06-03-143025-sales-analysis.md")
        entry = parse_output_entry(f)
        assert entry.timestamp == datetime(2026, 6, 3, 14, 30, 25)
        assert entry.skill_label == "CSV Analyzer"
        assert entry.size_bytes == 100
        assert entry.is_dir is False
        assert entry.filename == "2026-06-03-143025-sales-analysis.md"

    def test_directory_with_timestamp(self, tmp_outputs):
        d = _mkdir(tmp_outputs, "2026-06-03-143025-CC-Sort")
        entry = parse_output_entry(d)
        assert entry.timestamp == datetime(2026, 6, 3, 14, 30, 25)
        assert entry.skill_label == "CC Sort"
        assert entry.is_dir is True
        assert entry.size_bytes == 200  # dummy file inside

    def test_file_without_timestamp(self, tmp_outputs):
        f = _touch(tmp_outputs, "random-output.txt")
        entry = parse_output_entry(f)
        assert entry.timestamp is None
        assert entry.skill_label == "Unknown"

    def test_file_with_bad_timestamp(self, tmp_outputs):
        # Valid regex format but invalid date.
        f = _touch(tmp_outputs, "9999-99-99-999999-foo-summary.md")
        entry = parse_output_entry(f)
        assert entry.timestamp is None
        assert entry.skill_label == "Summarizer"


# ---------------------------------------------------------------------------
# Tests: scan_outputs.
# ---------------------------------------------------------------------------

class TestScanOutputs:
    def test_empty_directory(self, tmp_outputs):
        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            entries = scan_outputs()
        assert entries == []

    def test_sorted_newest_first(self, tmp_outputs):
        _touch(tmp_outputs, "2026-06-01-100000-a-summary.md")
        _touch(tmp_outputs, "2026-06-03-100000-b-summary.md")
        _touch(tmp_outputs, "2026-06-02-100000-c-summary.md")

        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            entries = scan_outputs()

        assert len(entries) == 3
        assert entries[0].filename.startswith("2026-06-03")
        assert entries[1].filename.startswith("2026-06-02")
        assert entries[2].filename.startswith("2026-06-01")

    def test_no_timestamp_entries_at_end(self, tmp_outputs):
        _touch(tmp_outputs, "2026-06-03-100000-b-summary.md")
        _touch(tmp_outputs, "orphan-file.txt")

        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            entries = scan_outputs()

        assert len(entries) == 2
        assert entries[0].timestamp is not None
        assert entries[1].timestamp is None

    def test_dotfiles_skipped(self, tmp_outputs):
        _touch(tmp_outputs, ".hidden-file")
        _touch(tmp_outputs, "2026-06-03-100000-visible-summary.md")

        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            entries = scan_outputs()

        assert len(entries) == 1
        assert entries[0].filename.startswith("2026-06-03")

    def test_nonexistent_directory(self, tmp_path):
        missing = tmp_path / "nope"
        with patch("ui.tabs.history._config.output_dir", return_value=missing):
            entries = scan_outputs()
        assert entries == []


# ---------------------------------------------------------------------------
# Tests: delete_output.
# ---------------------------------------------------------------------------

class TestDeleteOutput:
    def test_delete_file(self, tmp_outputs):
        f = _touch(tmp_outputs, "2026-06-03-100000-test-summary.md")
        assert f.is_file()

        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            result = delete_output(f)

        assert result is True
        assert not f.exists()

    def test_delete_directory(self, tmp_outputs):
        d = _mkdir(tmp_outputs, "2026-06-03-100000-CC-Sort")
        assert d.is_dir()

        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            result = delete_output(d)

        assert result is True
        assert not d.exists()

    def test_refuse_outside_outputs(self, tmp_outputs, tmp_path):
        # Create a file outside the outputs directory.
        outside = _touch(tmp_path, "important-file.txt")

        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            result = delete_output(outside)

        assert result is False
        assert outside.is_file()  # still there

    def test_delete_nonexistent(self, tmp_outputs):
        missing = tmp_outputs / "ghost.txt"
        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            result = delete_output(missing)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: _build_table_data (integration).
# ---------------------------------------------------------------------------

class TestBuildTableData:
    def test_returns_correct_columns(self, tmp_outputs):
        _touch(tmp_outputs, "2026-06-03-143025-report-summary.md")

        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            from ui.tabs.history import _build_table_data
            rows = _build_table_data()

        assert len(rows) == 1
        row = rows[0]
        assert len(row) == 4  # Date, Skill, Filename, Size
        assert row[0] == "2026-06-03 14:30:25"
        assert row[1] == "Summarizer"
        assert row[2] == "2026-06-03-143025-report-summary.md"
        assert "B" in row[3]  # has a size unit

    def test_empty_returns_empty_list(self, tmp_outputs):
        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            from ui.tabs.history import _build_table_data
            rows = _build_table_data()
        assert rows == []


# ---------------------------------------------------------------------------
# Tests: _build_summary_markdown.
# ---------------------------------------------------------------------------

class TestBuildSummaryMarkdown:
    def test_no_files(self, tmp_outputs):
        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            from ui.tabs.history import _build_summary_markdown
            md = _build_summary_markdown()
        assert "No output files" in md

    def test_with_files(self, tmp_outputs):
        _touch(tmp_outputs, "2026-06-03-143025-report-summary.md")
        _touch(tmp_outputs, "2026-06-02-100000-data-analysis.md")

        with patch("ui.tabs.history._config.output_dir", return_value=tmp_outputs):
            from ui.tabs.history import _build_summary_markdown
            md = _build_summary_markdown()

        assert "2" in md
        assert "output" in md.lower()
