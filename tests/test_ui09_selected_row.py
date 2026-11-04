"""UI-09 addendum -- a selected row must stay unmistakable over every tint.

Treatment: reverse highlight. Selected cells go light (#dbe7ff) with dark text, a
white top/bottom edge and a dark-blue left marker, so it does not rely on hue and
reads over the blue override tint, green, amber, orange and red alike.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui import _review_engine as eng  # noqa: E402

APP = "x9"


def _css() -> str:
    spec = eng.ReviewSpec(app_id=APP, columns=[eng.Column("A", "A")], target_col="A",
                          payload_var="__x9")
    html = eng.build_html(spec, [{"A": "1"}])
    return html.split("<style>", 1)[1].split("</style>", 1)[0]


def _rules(css):
    """(selector, body, position) for each simple rule."""
    return [(m.group(1).strip(), m.group(2), m.start())
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css)]


def _sel_rule(css):
    for sel, body, pos in _rules(css):
        if sel.endswith("tbody tr.selected td"):
            return sel, body, pos
    raise AssertionError("no tr.selected td rule")


def test_selected_rule_overrides_background_colour_image_and_text():
    _, body, _ = _sel_rule(_css())
    assert re.search(r"background-color:\s*#dbe7ff", body)
    assert re.search(r"background-image:\s*none", body)        # kills the tint gradient
    assert re.search(r"(?<!-)color:\s*#0b1220", body)          # dark text on the light cell
    assert "box-shadow" in body                                # not colour alone


def test_selected_rule_comes_after_every_tint_rule_and_is_not_less_specific():
    css = _css()
    sel, _, pos = _sel_rule(css)
    tints = [(s, p) for s, b, p in _rules(css)
             if "background-image" in b and s != sel and "selected" not in s]
    assert tints, "expected tint rules to compare against"
    for s, p in tints:
        assert p < pos, f"tint rule after the selected rule: {s}"
        # same or lower class count than the selected rule (tr.selected td = 1 class + tag)
        assert s.count(".") <= sel.count("."), s


def test_no_later_rule_repaints_a_selected_cell():
    css = _css()
    _, _, pos = _sel_rule(css)
    for s, b, p in _rules(css):
        if p > pos and "tbody tr" in s and "selected" not in s:
            assert "background-image" not in b and "background-color" not in b \
                and not re.search(r"background:", b), s


def test_selected_hover_stays_distinguishable_from_selected():
    css = _css()
    hov = [b for s, b, _ in _rules(css) if s.endswith("tr.selected:hover td")]
    assert hov and "#c3d6ff" in hov[0]
    assert "#c3d6ff" != "#dbe7ff"


def test_plain_hover_and_deleted_are_not_swallowed_by_the_selected_rule():
    css = _css()
    assert re.search(r"tbody tr:hover td", css)
    assert "row-deleted" in css
    # deleted rows are struck through, a cue that survives the selected colours
    assert re.search(r"row-deleted[^{]*\{[^}]*line-through", css)


def test_first_cell_carries_a_dark_left_marker_over_any_tint():
    css = _css()
    marker = [b for s, b, _ in _rules(css) if s.endswith("tr.selected td:first-child")]
    assert marker and "inset 5px 0 0 #1e3a8a" in marker[0]


def test_every_accent_colour_is_covered_by_the_same_override():
    """The rule is colour-agnostic: it is one selector, not one per tint, so adding
    a tint cannot leave a selected row unreadable."""
    css = _css()
    sel_rules = [s for s, b, _ in _rules(css) if s.endswith("tbody tr.selected td")]
    assert len(sel_rules) == 1
    for name in eng.ACCENT_COLOURS:
        assert f"accent-{name}" in css or name in css
