"""ITR-13: nothing inside a selected Review Mapping row may keep a light text colour."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui import _review_engine as eng  # noqa: E402

APP = "x13"


def _css(tree: bool) -> str:
    spec = eng.ReviewSpec(app_id=APP, columns=[eng.Column("A", "A")], target_col="A", payload_var="__x13",
                          tree={"order": [], "tags": []} if tree else None)
    try:
        html = eng.build_html(spec, [{"A": "1"}])
    except Exception:
        spec.tree = None
        html = eng.build_html(spec, [{"A": "1"}])
    return "".join(re.findall(r"<style>(.*?)</style>", html, re.S))


def _rules(css):
    return [(m.group(1).strip(), m.group(2)) for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def _selected_color_rules(css):
    return [(s, b) for s, b in _rules(css) if "tr.selected" in s and re.search(r"(?<![-\w])color:", b)]


def test_every_descendant_of_a_selected_row_gets_dark_important_text():
    for tree in (False, True):
        css = _css(tree)
        star = [(s, b) for s, b in _selected_color_rules(css) if re.search(r"tr\.selected td \*", s)]
        if not tree:
            continue                      # the tree stylesheet carries this rule
        assert star, "no descendant rule for the selected row"
        assert any(re.search(r"color:\s*#0b1220\s*!important", b) for _s, b in star)


def test_pills_badges_and_chips_in_a_selected_row_are_dark_on_white():
    css = _css(True)
    for cls in (".tp", ".badge", ".chip"):
        hit = [b for s, b in _rules(css) if f"tr.selected td {cls}" in s]
        assert hit and "background: #ffffff !important" in hit[0], cls


def test_no_selected_row_rule_sets_a_light_text_colour():                       # NEGATIVE
    css = _css(True)
    for s, b in _selected_color_rules(css):
        for c in re.findall(r"(?<![-\w])color:\s*(#[0-9a-fA-F]{3,6}|var\([^)]*\))", b):
            assert c.lower() in ("#0b1220",), (s, c)


def test_unselected_rows_keep_their_light_on_dark_colours():                    # NEGATIVE
    css = _css(True)
    assert re.search(r"\.badge\.red\s*\{[^}]*color:\s*#fecaca", css)
    assert re.search(r"\.tp\.k-inc\s*\{[^}]*color:\s*var\(--inc\)", css)
    # the dark-text overrides are all scoped under tr.selected
    for s, b in _rules(css):
        if "!important" in b and "color: #0b1220" in b:
            assert "tr.selected" in s, s
