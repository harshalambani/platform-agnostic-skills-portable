"""
UI-09 / UI-08 follow-ups (shared review engine).

1. HOVER stays distinct: a hovered row keeps its accent tint (it must never read
   as another match type) and adds a non-fill cue (light inset lines). Selected
   remains the reverse highlight; deleted / locked rows are untouched.
2. STICKY-HEADER BLEED: with `border-collapse: collapse` the 3px accent stripe and
   row borders of scrolled rows painted through the sticky header. The table now
   uses separate borders, the row separator lives on `td`.

The static checks are string-level. The bleed is ALSO checked in a real browser
(headless Edge/Chrome screenshot at DPR 1 / 1.25 / 1.5); skipped if no browser or
Pillow is available.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui import _review_engine as eng  # noqa: E402

CSS = re.sub(r"/\*.*?\*/", "", eng._CSS.replace("%%APP%%", "x"), flags=re.S)
ACCENTS = tuple(eng.ACCENT_COLOURS)


def _rules():
    return [(s.strip(), b.strip(), m) for m, (s, b) in
            ((0, (a, c)) for a, c in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS))]


def _find(selector):
    """[(index, body)] of rules that list `selector` exactly."""
    out = []
    for i, (sel, body, _m) in enumerate(_rules()):
        if selector in [p.strip() for p in sel.split(",")]:
            out.append((i, body))
    return out


def _prop(body, name):
    m = re.search(r"(?<![-\w])%s\s*:\s*([^;]+)" % re.escape(name), body)
    return m.group(1).strip() if m else None


# ---- tiny cascade model of the properties that make up a row's LOOK -----------------

def _look(accent, zebra, hover):
    """(row background, tint image, cell shadow) for a plain (non-selected,
    non-tone) row, read from the real rules."""
    bg = None
    if zebra:
        bg = _prop(_find("#x-app tbody tr:nth-child(even)")[0][1], "background")
    if hover:
        bg = _prop(_find("#x-app tbody tr:hover")[0][1], "background")
    image = None
    if accent:
        image = _prop(_find(f"#x-app tbody tr.accent-{accent} td")[0][1], "background-image")
    shadow = None
    if hover:
        # does any tint-off rule list the hover cell? (it must not)
        for i, body in _find("#x-app tbody tr:hover td"):
            if _prop(body, "background-image") == "none":
                image = None
            if _prop(body, "box-shadow"):
                shadow = _prop(body, "box-shadow")
    return (bg, image, shadow)


@pytest.mark.parametrize("zebra", [False, True])
@pytest.mark.parametrize("accent", ACCENTS)
def test_hover_differs_from_rest_for_every_accent(accent, zebra):
    assert _look(accent, zebra, True) != _look(accent, zebra, False)


@pytest.mark.parametrize("zebra", [False, True])
@pytest.mark.parametrize("accent", ACCENTS)
def test_hover_keeps_its_own_accent_tint(accent, zebra):
    """NEGATIVE: the hovered row does not lose its tint (it would then look like a
    plain, un-matched row), and does not take another accent's."""
    hov = _look(accent, zebra, True)
    assert hov[1] == _look(accent, zebra, False)[1] and hov[1] is not None
    for other in ACCENTS:
        if other != accent:
            assert hov[1] != _look(other, zebra, True)[1]


@pytest.mark.parametrize("zebra", [False, True])
@pytest.mark.parametrize("accent", ACCENTS)
def test_hover_never_looks_like_any_other_accent_at_rest(accent, zebra):
    hov = _look(accent, zebra, True)
    for other in ACCENTS:
        for z in (False, True):
            assert hov != _look(other, z, False), (accent, other)


def test_hover_cue_is_not_a_fill_and_not_an_accent_or_selected_colour():
    rules = _find("#x-app tbody tr:hover td")
    assert len(rules) == 1, "hover cell rule must exist exactly once (and not in tint-off)"
    body = rules[0][1]
    assert "background-image" not in body and "background-color" not in body
    shadow = _prop(body, "box-shadow")
    assert shadow and "inset" in shadow
    for hx in list(eng.ACCENT_COLOURS.values()) + ["#ffffff", "#dbe7ff", "#c3d6ff"]:
        assert hx.lower() not in shadow.lower()


def test_hover_rule_is_before_selected_so_selected_wins():
    i_hover = _find("#x-app tbody tr:hover td")[0][0]
    i_sel = _find("#x-app tbody tr.selected td")[-1][0]   # the reverse-highlight rule, not tint-off
    i_selhov = _find("#x-app tbody tr.selected:hover td")[0][0]
    assert i_hover < i_sel < i_selhov
    sel_body = _find("#x-app tbody tr.selected td")[-1][1]
    assert "box-shadow" in sel_body            # selected overrides the hover cue's shadow
    assert _prop(sel_body, "background-color") == "#dbe7ff"


def test_tint_off_list_no_longer_names_hover():
    """NEGATIVE: the tint-off rule is for selected and contra tones only."""
    for sel, body, _ in _rules():
        if _prop(body, "background-image") == "none":
            assert "hover" not in sel.replace("selected:hover", ""), sel


def test_deleted_and_locked_rows_unchanged():
    assert "line-through" in _find("#x-app tbody tr.row-deleted td")[0][1]
    assert _prop(_find("#x-app tbody tr.row-deleted td")[0][1], "opacity") == "0.55"
    assert _prop(_find("#x-app tbody tr.locked")[0][1], "opacity") == "0.75"
    assert _prop(_find("#x-app tbody tr.locked")[0][1], "cursor") == "not-allowed"


# ---- sticky header bleed: static ------------------------------------------------------

def test_table_uses_separate_borders():
    body = _find("#x-app table")[0][1]
    assert _prop(body, "border-collapse") == "separate"
    assert _prop(body, "border-spacing") == "0"
    assert "border-collapse: collapse" not in CSS      # NEGATIVE: no collapsed table anywhere


def test_row_separator_moved_to_the_cells():
    tr_rules = [b for s, b, _ in _rules() if s.strip() == "#x-app tbody tr"]
    assert tr_rules and all("border" not in b for b in tr_rules)   # NEGATIVE: ignored on a tr when separate
    assert "border-bottom: 1px solid #262626" in _find("#x-app tbody td")[0][1]


def test_header_stays_opaque_and_sticky_with_its_own_borders():
    assert _prop(_find("#x-app thead")[0][1], "position") == "sticky"
    assert _prop(_find("#x-app thead")[0][1], "background") == "#1a1a1a"
    assert "border-bottom: 2px solid #444" in _find("#x-app thead th")[0][1]
    assert "border-bottom: 1px solid #444" in _find("#x-app thead .filter-row td")[0][1]


def test_selected_and_stripe_rules_survive_the_border_change():
    assert "border-left: 3px solid" in _find("#x-app tbody tr.accent-red td:first-child")[0][1]
    assert "inset 5px 0 0 #1e3a8a" in _find("#x-app tbody tr.selected td:first-child")[0][1]


# ---- sticky header bleed: real browser -----------------------------------------------

def _browser():
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              shutil.which("msedge") or "", shutil.which("chrome") or "",
              shutil.which("google-chrome") or ""):
        if p and os.path.exists(p):
            return p
    return None


def _bleed_pixels(html, dpr, browser, tmp):
    from PIL import Image
    page = ("<!doctype html><meta charset=utf-8><body style='margin:0;background:#0f0f0f'>" + html +
            "<script>setTimeout(function(){var w=document.querySelector('.scroll-wrapper');"
            "w.scrollTop=230;var h=document.querySelector('thead').getBoundingClientRect();"
            "var wr=w.getBoundingClientRect();var p=document.createElement('pre');p.id='rep';"
            "p.textContent=JSON.stringify({top:h.top,bottom:h.bottom,y:w.scrollTop,l:wr.left,r:wr.right});"
            "document.body.appendChild(p);},500)</script>")
    f = Path(tmp) / "page.html"
    f.write_text(page, encoding="utf-8")
    url = f.as_uri()
    shot = Path(tmp) / f"s{dpr}.png"
    common = ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--disable-lcd-text",
              "--font-render-hinting=none", f"--force-device-scale-factor={dpr}",
              "--window-size=900,600", "--virtual-time-budget=4000",
              f"--user-data-dir={Path(tmp) / 'profile'}"]
    subprocess.run([browser, *common, f"--screenshot={shot}", url], capture_output=True, timeout=120)
    dump = subprocess.run([browser, *common, "--dump-dom", url], capture_output=True,
                          timeout=120).stdout.decode("utf-8", "replace")
    m = re.search(r'id="rep">(.*?)</pre>', dump)
    assert m, "page did not render"
    rep = json.loads(m.group(1))
    assert rep["y"] > 200, "the table did not scroll"
    im = Image.open(shot).convert("RGB")
    bad = 0
    for y in range(int((rep["top"] + 1) * dpr), int(rep["bottom"] * dpr) - 2):
        for x in range(int((rep["l"] + 2) * dpr), int((rep["r"] - 20) * dpr)):
            r, g, b = im.getpixel((x, y))
            if max(abs(r - g), abs(g - b), abs(r - b)) > 8:
                bad += 1
    return bad


def _page_html(collapsed=False):
    spec = eng.ReviewSpec(app_id="x9", columns=[eng.Column("A", "A"), eng.Column("B", "B")],
                          target_col="B", payload_var="__x9")
    rows = [{"A": f"row {i}", "B": "acct", "_band": "accent-" + ACCENTS[i % 5]} for i in range(60)]
    html = eng.build_html(spec, rows)
    if collapsed:   # the OLD stylesheet, for the control run
        html = html.replace("border-collapse: separate; border-spacing: 0;", "border-collapse: collapse;")
        html = html.replace("</style>", "#x9-app tbody tr { border-bottom: 1px solid #262626; }</style>", 1)
    return html


def _have_browser_stack():
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return _browser() is not None


needs_browser = pytest.mark.skipif(not _have_browser_stack(), reason="no headless Edge/Chrome or Pillow")


@needs_browser
@pytest.mark.parametrize("dpr", [1, 1.25, 1.5])
def test_no_accent_pixels_in_the_sticky_header_band_in_a_real_browser(dpr):
    """60 accent rows, scrolled ~230px: the header band (title + filter row) must
    contain only grey pixels. Up to 4 stray anti-aliased pixels are tolerated at
    the wrapper's rounded corner (the old stylesheet gives well over 1000)."""
    with tempfile.TemporaryDirectory() as tmp:
        assert _bleed_pixels(_page_html(), dpr, _browser(), tmp) <= 4


@needs_browser
def test_the_browser_check_detects_the_old_collapsed_table():
    """NEGATIVE control: the same page with the old collapsed-border CSS DOES bleed,
    so a clean result above is not a blind test."""
    with tempfile.TemporaryDirectory() as tmp:
        assert _bleed_pixels(_page_html(collapsed=True), 1, _browser(), tmp) > 200
