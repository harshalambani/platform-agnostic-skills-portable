"""tests/test_ui41_result_report.py -- UI-41: the result panel keeps line breaks, a status box still
renders as a box, and a reply with a RED FLAG / FAIL never opens with a success line. Synthetic text."""
from __future__ import annotations

import pytest

from ui.tabs import _generic as G


def _find_markdowns(blocks):
    import gradio as gr
    return [b for b in blocks.blocks.values() if isinstance(b, gr.Markdown)]


def test_the_result_panel_keeps_single_newlines():
    import gradio as gr
    from ui.webui import build_app as build_ui
    demo = build_ui() if callable(build_ui) else build_ui
    panels = [b for b in _find_markdowns(demo) if getattr(b, "value", "") == "_Awaiting input._"]
    assert panels, "no result panel found"
    assert all(p.line_breaks is True for p in panels)


def test_other_markdown_blocks_are_not_changed_by_this_fix():                 # NEGATIVE
    import gradio as gr
    from ui.webui import build_app as build_ui
    demo = build_ui() if callable(build_ui) else build_ui
    others = [b for b in _find_markdowns(demo) if getattr(b, "value", "") != "_Awaiting input._"]
    assert others and not all(b.line_breaks for b in others)


def test_a_status_box_still_renders_as_a_box():
    out = G._colorize_status("Error: something broke\n\nWarning: check this\n\n### Done\n\nplain line")
    assert 'class="rag-error"' in out and 'class="rag-warn"' in out and 'class="rag-ok"' in out
    assert "plain line" in out and 'rag-ok">✅ plain' not in out               # NEGATIVE: ordinary text stays text


def test_a_reply_with_a_red_flag_never_opens_with_a_success_line():           # NEGATIVE
    reply = "RED FLAG: 2 bank payments match no statement\n\n12 rows written"
    head = G._done_heading(reply)
    assert head.startswith("### Needs your attention") and "Done" not in head
    boxed = G._colorize_status(head + "\n\n" + reply)
    assert 'class="rag-ok"' not in boxed.split("\n\n")[0]
    assert 'class="rag-warn"' in boxed


def test_a_reply_with_a_fail_never_starts_with_success():                     # NEGATIVE
    head = G._done_heading("Tie-out FAIL for card 1234\nTie-out PASS for card 5678")
    assert head.startswith("### Needs your attention") and "1 item" in head


def test_a_missing_statement_counts():
    assert G._attention_count("Missing statement: Apr 2026 for card 1234") == 1


def test_a_clean_reply_is_plain_done():
    assert G._done_heading("12 rows written. All balanced.") == "### Done"
    assert G._done_heading("") == "### Done" and G._done_heading(None) == "### Done"


def test_the_same_line_is_counted_once():                                     # NEGATIVE
    assert G._attention_count("RED FLAG: x\nRED FLAG: x\n- RED FLAG: x") == 1


def test_words_that_only_contain_fail_do_not_trigger():                       # NEGATIVE
    assert G._attention_count("failsafe mode off; unfailing") == 0
