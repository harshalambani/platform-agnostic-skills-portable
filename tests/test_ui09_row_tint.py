"""
UI-09 -- the match-type colour tints the WHOLE review row (shared engine).

It used to be a 3px stripe on the first cell only, lost by the time the eye
reached the Account / Transfer columns. Now: stripe kept, and every cell gets an
overlay of the accent at ~18% (a background-image, so the zebra shows through).

Stylesheet checks are string-level (no browser here); the class swap on Assign
runs in node against the harness's fake DOM.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _review_js as js  # noqa: E402
from ui import _review_engine as eng  # noqa: E402
from ui.tabs import gnucash_review as rv  # noqa: E402

needs_node = pytest.mark.skipif(not js.have_node(), reason="node not installed")

CSS = re.sub(r"/\*.*?\*/", "", eng._CSS.replace("%%APP%%", "x"), flags=re.S)
ACCENTS = ("red", "amber", "green", "blue", "orange")


def _rules():
    """[(selector-list, body)] for every rule in the stylesheet, in order."""
    return [(s.strip(), b.strip()) for s, b in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS)]


def _rule_for(selector_part):
    for sel, body in _rules():
        if selector_part in [p.strip() for p in sel.split(",")]:
            yield sel, body


def _index_of(selector_part):
    for i, (sel, _b) in enumerate(_rules()):
        if selector_part in [p.strip() for p in sel.split(",")]:
            return i
    raise AssertionError(selector_part)


@pytest.mark.parametrize("name", ACCENTS)
def test_every_accent_tints_every_cell_not_just_the_first(name):
    hx = eng.ACCENT_COLOURS[name]
    rgb = eng._rgb(hx)
    rules = list(_rule_for(f"#x-app tbody tr.accent-{name} td"))
    assert rules, "no whole-row (td) rule for accent-" + name
    body = rules[0][1]
    assert "linear-gradient(rgba(%s,0.18)" % rgb in body
    # the stripe is still there, on the first cell
    stripe = list(_rule_for(f"#x-app tbody tr.accent-{name} td:first-child"))
    assert stripe and f"3px solid {hx}" in stripe[0][1]


def test_tint_is_an_overlay_so_the_zebra_is_not_wiped():
    """NEGATIVE: the tint must be background-image only; a `background` shorthand
    would reset the odd/even row colour underneath."""
    for name in ACCENTS:
        body = list(_rule_for(f"#x-app tbody tr.accent-{name} td"))[0][1]
        assert "background-image" in body
        assert not re.search(r"background\s*:", body)
        assert "background-color" not in body


@pytest.mark.parametrize("state", ["#x-app tbody tr:hover td", "#x-app tbody tr.selected td"])
def test_hover_and_selected_are_not_overridden_by_the_tint(state):
    """NEGATIVE: hover/selected drop the overlay AND are declared after the accent
    rules at equal specificity, so they win."""
    rules = list(_rule_for(state))
    assert rules and "background-image: none" in rules[0][1]
    assert _index_of(state) > _index_of("#x-app tbody tr.accent-orange td")
    # their own solid colours are untouched
    assert re.search(r"tr\.selected \{ background: #1e3a5f", CSS)
    assert re.search(r"tr:hover \{ background: #1a2744", CSS)


@pytest.mark.parametrize("tone", ["tone-amber", "tone-green"])
def test_contra_tone_rows_keep_one_background(tone):
    """NEGATIVE: a contra row's tone is its single background; the accent overlay
    is switched off for it (the stripe still marks the match type)."""
    sel = f"#x-app tbody tr.{tone} td"
    rules = list(_rule_for(sel))
    assert rules and "background-image: none" in rules[0][1]
    assert _index_of(sel) > _index_of("#x-app tbody tr.accent-orange td")
    assert re.search(r"tr\.%s\s*\{ background: #" % tone, CSS)


def test_deleted_row_is_still_struck_through_and_faded():
    """NEGATIVE: nothing in the tint touches text-decoration / opacity."""
    body = list(_rule_for("#x-app tbody tr.row-deleted td"))[0][1]
    assert "line-through" in body and "opacity: 0.55" in body
    for name in ACCENTS:
        tint = list(_rule_for(f"#x-app tbody tr.accent-{name} td"))[0][1]
        assert "opacity" not in tint and "text-decoration" not in tint
    # the whole-row tint must not reach a locked row's opacity either
    assert "opacity: 0.75" in list(_rule_for("#x-app tbody tr.locked"))[0][1]


def test_a_row_with_no_accent_gets_no_tint():
    """NEGATIVE: no generic `tbody tr td` gradient exists; only accent-* rules."""
    for sel, body in _rules():
        if "linear-gradient" in body:
            assert all(re.search(r"tr\.accent-(%s) td$" % "|".join(ACCENTS), p.strip())
                       for p in sel.split(",")), sel
    assert len([1 for _s, b in _rules() if "linear-gradient" in b]) == len(ACCENTS)


@pytest.mark.parametrize("name", ACCENTS)
def test_legend_swatch_colour_equals_row_accent_colour(name):
    hx = eng.ACCENT_COLOURS[name]
    sw = list(_rule_for(f"#x-app .legend .sw.{name}"))[0][1]
    assert hx in sw
    stripe = list(_rule_for(f"#x-app tbody tr.accent-{name} td:first-child"))[0][1]
    assert hx in stripe


def test_every_status_and_band_class_used_by_the_tabs_has_a_tint():
    text = "".join(p.read_text(encoding="utf-8")
                   for p in (ROOT / "ui" / "tabs").glob("*.py"))
    for cls in set(re.findall(r"accent-([a-z]+)", text)):
        assert cls in eng.ACCENT_COLOURS, f"accent-{cls} used by a tab but has no colour"


# ---- Assign carries the tint with the class ----------------------------------------

_HEADER = "Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n"
_ROWS = (
    "2025-05-01,UNKNOWN PAYEE ONE,Liabilities:Suspense,,100.00,900.00,suspense,no match\n"
    "2025-05-02,UNKNOWN PAYEE TWO,Liabilities:Suspense,,50.00,850.00,suspense,no match\n"
    "2025-05-03,COFFEE,Expense:Food,,20.00,830.00,high,rule\n"
)


def _html(tmp_path):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER + _ROWS, encoding="utf-8")
    return rv._load_review_data(str(p), str(p))


def _accent(row):
    return [c for c in row["cls"] if c.startswith("accent-")]


@needs_node
def test_assign_moves_the_accent_class_and_only_on_the_assigned_row(tmp_path):
    out = js.run_js(_html(tmp_path), "rv", """
        const before = view(APP);
        select(APP, 0); pick(APP, 0); applySel(APP);
        return {before, after: view(APP)};
    """, tmp_path)
    b = {r["idx"]: r for r in out["before"]}
    a = {r["idx"]: r for r in out["after"]}
    assert _accent(b[0]) == ["accent-red"] and _accent(b[1]) == ["accent-red"]
    assert len(_accent(a[0])) == 1 and _accent(a[0]) != ["accent-red"]
    assert _accent(a[0])[0][len("accent-"):] in eng.ACCENT_COLOURS   # a tinted class
    # NEGATIVE: neighbours keep their own accent, no row ends up with two
    assert _accent(a[1]) == ["accent-red"]
    assert _accent(a[2]) == _accent(b[2])
    assert all(len(_accent(r)) <= 1 for r in a.values())
