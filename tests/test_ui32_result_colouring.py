"""UI-32: a result line is green only when it really reports success."""
from __future__ import annotations

import sys
from pathlib import Path

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from ui.tabs._generic import _colorize_status  # noqa: E402


def _green(line: str) -> bool:
    return "rag-ok" in _colorize_status(line)


def test_genuine_completed_line_is_green():
    assert _green("Completed.")
    assert _green("Run complete: 12 rows written")
    assert _green("### Done")
    assert _green("All balanced")


def test_failure_wordings_are_never_green():
    for line in (
        "RED FLAG: the run is incomplete",
        "could not complete the s.194T reconciliation",
        "Sort completed with 10 issue(s)",
        "Run did not complete",
        "completed with 1 issue",
        "Reconciliation failed to complete",
        "Completed, but 3 errors were found",
        "complete - unable to read 2 files",
    ):
        assert not _green(line), line


def test_incomplete_is_not_matched_as_complete():
    assert not _green("The statement set is incomplete")


def test_list_after_status_line_still_renders_as_list():
    md = "Completed.\n- first\n- second\n- third"
    html = MarkdownIt("commonmark", {"html": True}).render(_colorize_status(md))
    assert html.count("<li>") == 3
    assert "<ul>" in html
    # the old output swallowed the bullets into the HTML block
    assert "- first" not in html


def test_error_and_warning_boxes_also_release_following_lines():
    for head in ("Error: boom", "Warning: careful"):
        md = head + "\n- a\n- b"
        html = MarkdownIt("commonmark", {"html": True}).render(_colorize_status(md))
        assert html.count("<li>") == 2


def test_plain_lines_pass_through_unchanged():
    assert _colorize_status("Some ordinary line\n\n- x") == "Some ordinary line\n\n- x"
