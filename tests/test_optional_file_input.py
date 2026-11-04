"""
tests/test_optional_file_input.py — Defect 4 regression guard.

Previously the "file"/"output_file" input branch in
ui/tabs/_generic.py's _make_run_handler validation never checked
inp_def.required before refusing to proceed on a missing value — unlike
the "files" and "directory" branches, which already honored it. This hit
the ITR Workbook skill: leaving its optional "Entity mapping" file blank
produced "please provide: Entity mapping (optional...)" and blocked the
run outright, instead of letting it proceed to BLOCKED-FOR-REVIEW as
designed.

These tests exercise a minimal manifest with a single optional "file"
input and confirm an empty value no longer produces the "please provide"
warning, while a required "file" input still does.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.tabs._generic import _make_run_handler


def _make_minimal_skill(*, input_required: bool, input_type: str = "file") -> Any:
    inp = SimpleNamespace(
        name="mapping_file",
        type=input_type,
        label="Entity mapping (optional — leave blank to run without one)",
        required=input_required,
        file_types=None,
        options=[],
    )
    output = SimpleNamespace(
        type="file",
        suffix="out",
        extension=".xlsx",
        download_label="Download",
    )
    requires = SimpleNamespace(native_binaries=[], external_tools=[], llm=False)
    return SimpleNamespace(
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


def _run_handler_to_list(skill, *args):
    handler = _make_run_handler(skill)
    return list(handler(*args))


def test_optional_file_input_does_not_block_run():
    """Leaving an optional file input blank must not stop the run at
    validation — it should proceed at least to the 'Running' step."""
    skill = _make_minimal_skill(input_required=False)

    results = _run_handler_to_list(skill, None, "model-x")
    markdowns = [md for md, *_ in results]

    assert not any("please provide" in md.lower() for md in markdowns), markdowns
    assert any("running" in md.lower() for md in markdowns), markdowns


def test_required_file_input_still_blocks_run():
    """A required file input left blank must still be rejected — this
    guards against the fix over-correcting into skipping validation
    entirely."""
    skill = _make_minimal_skill(input_required=True)

    results = _run_handler_to_list(skill, None, "model-x")
    markdowns = [md for md, *_ in results]

    assert any("please provide" in md.lower() for md in markdowns), markdowns
