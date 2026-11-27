"""UI-55: the GnuCash second row. Tab placement only; synthetic, no user data."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from agents import registry  # noqa: E402
from ui import webui  # noqa: E402
from ui.tabs import _generic  # noqa: E402

EXPECTED_ROW = ["Banks", "Credit Card", "Partner", "Inter-entity", "26AS",
                "KRChoksey", "ITR", "Coverage", "Year-End Close", "Entities"]
MOVED = ["Credit Card - Book Spends", "Coverage Gap Detector", "Year-End Close"]


class _S:
    def __init__(self, name):
        self.display_name = name


def _gnucash_row(app):
    """Labels of the tabs directly inside the top-level GnuCash tab."""
    def find(block):
        for c in getattr(block, "children", None) or []:
            if type(c).__name__ == "Tab" and c.label == "GnuCash":
                return c
            r = find(c)
            if r is not None:
                return r
        return None
    gc = find(app)
    assert gc is not None
    tabs = [c for c in gc.children if type(c).__name__ == "Tabs"]
    assert len(tabs) == 1
    return [t.label for t in tabs[0].children if type(t).__name__ == "Tab"]


@pytest.fixture(scope="module")
def recorded():
    """Build the eager and the lazy app once, recording every skill rendered."""
    seen = {}
    orig = _generic.render
    for mode in ("eager", "lazy"):
        calls = []

        def spy(skill, *a, _calls=calls, **k):
            _calls.append(skill.name)
            return orig(skill, *a, **k)
        _generic.render = spy
        try:
            app = webui.build_app(launch=False, lazy=(mode == "lazy"))
            if mode == "lazy":
                # lazy bodies only run once the page loads: run them directly
                from ui import _lazy_tabs
                assert _lazy_tabs.build_log() == []
        finally:
            _generic.render = orig
        seen[mode] = (app, calls)
    return seen


def test_gnucash_row_order_is_exact(recorded):
    assert list(webui.GNUCASH_ROW) == EXPECTED_ROW
    assert _gnucash_row(recorded["eager"][0]) == EXPECTED_ROW


def test_lazy_and_eager_builds_give_the_same_tab_set(recorded):  # NEGATIVE: no lazy-only/eager-only tab
    assert _gnucash_row(recorded["lazy"][0]) == _gnucash_row(recorded["eager"][0])


def test_banks_no_longer_lists_the_moved_skills():  # NEGATIVE
    skills = [_S(n) for n in ["Bank - Convert to GnuCash", *MOVED]]
    parts = webui._split_gnucash_skills(skills)
    banks = [s.display_name for s in parts["Banks"]]
    assert banks == ["Bank - Convert to GnuCash"]
    for n in MOVED:
        assert n not in banks


def test_each_moved_skill_lands_in_its_own_tab_exactly_once():
    parts = webui._split_gnucash_skills([_S(n) for n in MOVED])
    assert [s.display_name for s in parts["Credit Card"]] == ["Credit Card - Book Spends"]
    assert [s.display_name for s in parts["Coverage"]] == ["Coverage Gap Detector"]
    assert [s.display_name for s in parts["Year-End Close"]] == ["Year-End Close"]
    assert parts["Banks"] == []


def test_unknown_future_skill_still_defaults_to_banks():
    parts = webui._split_gnucash_skills([_S("Some Future Skill"), _S(MOVED[0])])
    assert [s.display_name for s in parts["Banks"]] == ["Some Future Skill"]


def test_remaining_banks_skills_unchanged_and_in_order():
    """Whatever Banks held before, minus the three moved ones, in the same order."""
    skills = [s for s in registry.discover(refresh=True) if s.category == "gnucash"]
    before_all = [s.display_name for s in skills]
    expect = [n for n in before_all if n not in MOVED]
    got = [s.display_name for s in webui._split_gnucash_skills(skills)["Banks"]]
    assert got == expect
    b, a = webui._split_banks_tabs(got)
    assert (b + a) == sorted(got, key=webui._banks_tab_rank)   # same ordering rule as before
    assert "Bank - Convert to GnuCash" in b
    assert a == []                                    # nothing renders after Review in Banks
    assert "Coverage Gap Detector" not in got         # NEGATIVE: it is on its own tab, not in Banks
    assert "Coverage Gap Detector" not in webui._BANKS_TAB_ORDER


def _name_of(display):
    return [s.name for s in registry.discover(refresh=True) if s.display_name == display]


def test_every_skill_appears_exactly_once_in_the_ui(recorded):  # NEGATIVE: none duplicated or lost
    calls = recorded["eager"][1]
    for n in MOVED:
        ids = _name_of(n)
        assert len(ids) == 1
        assert calls.count(ids[0]) == 1, (n, calls)
    for s in registry.discover(refresh=True):
        assert calls.count(s.name) <= 1, s.name


def test_lazy_build_registers_one_group_per_tab(recorded):
    from ui import _lazy_tabs  # noqa: F401
    # the GnuCash groups are all lazy-registered: tabs exist, bodies not yet built
    row = _gnucash_row(recorded["lazy"][0])
    assert row == EXPECTED_ROW
    for n in MOVED:
        assert recorded["lazy"][1].count(_name_of(n)[0]) == 0


def test_literal_blocks_text_kept_for_zoom_head_test():
    assert "with gr.Blocks(" in (ROOT / "ui" / "webui.py").read_text(encoding="utf-8")
