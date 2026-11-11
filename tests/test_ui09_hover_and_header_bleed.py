"""
UI-09 / UI-08 follow-ups (shared review engine).

1. HOVER stays distinct: a hovered row keeps its accent tint (it must never read
   as another match type) and adds a non-fill cue (light inset lines). Selected
   remains the reverse highlight; deleted / locked rows are untouched.
2. STICKY-HEADER BLEED: with `border-collapse: collapse` the 3px accent stripe and
   row borders of scrolled rows painted through the sticky header. The table now
   uses separate borders, the row separator lives on `td`.

The static checks are string-level. The bleed is ALSO checked in a real browser
(headless Edge/Chrome driven over the DevTools protocol, as the UI-11 tests do,
screenshot at DPR 1 / 1.25 / 1.5); skipped if no browser, aiohttp or Pillow is
available. The browser profile lives in a folder made for the test run and is
removed afterwards; a test proves nothing is left in the temp folder.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
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


async def _bleed_report(page_url, port, dpr):
    """Open the page in the running browser over the DevTools protocol, wait for
    the page's own report element, and return (report dict, PNG bytes)."""
    import aiohttp
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % port))
    ws_url = [t for t in targets if t.get("type") == "page"][0]["webSocketDebuggerUrl"]
    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(ws_url, max_msg_size=0) as ws:
            n = 0

            async def call(method, **params):
                nonlocal n
                n += 1
                await ws.send_json({"id": n, "method": method, "params": params})
                while True:
                    msg = await ws.receive_json()
                    if msg.get("id") == n:
                        if "error" in msg:
                            raise RuntimeError(msg["error"])
                        return msg.get("result", {})

            await call("Page.enable")
            await call("Emulation.setDeviceMetricsOverride", width=900, height=600,
                       deviceScaleFactor=dpr, mobile=False)
            await call("Page.navigate", url=page_url)
            rep = None
            for _ in range(100):
                await asyncio.sleep(0.1)
                r = await call("Runtime.evaluate", returnByValue=True,
                               expression="(document.getElementById('rep')||{}).textContent||''")
                val = r.get("result", {}).get("value")
                if val:
                    rep = json.loads(val)
                    break
            assert rep, "page did not render"
            await asyncio.sleep(0.2)
            shot = await call("Page.captureScreenshot", format="png")
            return rep, base64.b64decode(shot["data"])


def _bleed_pixels(html, dpr, port, tmp):
    from PIL import Image
    page = ("<!doctype html><meta charset=utf-8><body style='margin:0;background:#0f0f0f'>" + html +
            "<script>setTimeout(function(){var w=document.querySelector('.scroll-wrapper');"
            "w.scrollTop=230;var h=document.querySelector('thead').getBoundingClientRect();"
            "var wr=w.getBoundingClientRect();var p=document.createElement('pre');p.id='rep';"
            "p.textContent=JSON.stringify({top:h.top,bottom:h.bottom,y:w.scrollTop,l:wr.left,r:wr.right});"
            "document.body.appendChild(p);},500)</script>")
    f = Path(tmp) / "page.html"
    f.write_text(page, encoding="utf-8")
    rep, png = asyncio.run(_bleed_report(f.as_uri(), port, dpr))
    assert rep["y"] > 200, "the table did not scroll"
    im = Image.open(io.BytesIO(png)).convert("RGB")
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
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return _browser() is not None


needs_browser = pytest.mark.skipif(not _have_browser_stack(),
                                   reason="no headless Edge/Chrome, aiohttp or Pillow")


def _close_browser(port, timeout=15):
    """Ask the browser itself to quit (CDP Browser.close) and wait until its
    debugging port stops answering. On Windows the process Popen started can be
    a launcher, so terminate() alone leaves the real browser, and the lock on
    its profile folder, behind."""
    import aiohttp

    async def _go():
        info = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=2))
        async with aiohttp.ClientSession() as sess:
            async with sess.ws_connect(info["webSocketDebuggerUrl"]) as ws:
                await ws.send_str(json.dumps({"id": 1, "method": "Browser.close"}))
                try:
                    await asyncio.wait_for(ws.receive(), 5)
                except Exception:
                    pass
    try:
        asyncio.run(_go())
    except Exception:
        pass
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=1)
        except Exception:
            return
        time.sleep(0.2)


def _remove_dir_with_retry(path, attempts=20, delay=0.25):
    for _ in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not Path(path).exists():
            return
        time.sleep(delay)
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="module")
def browser_port(tmp_path_factory):
    """One headless browser for the module, its profile inside a pytest temp
    folder, closed and removed when the module is done."""
    if not _have_browser_stack():
        pytest.skip("no headless Edge/Chrome, aiohttp or Pillow")
    work = tmp_path_factory.mktemp("ui09-browser")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    proc = subprocess.Popen(
        [_browser(), "--headless=new", "--disable-gpu", "--no-first-run",
         "--hide-scrollbars", "--disable-lcd-text", "--font-render-hinting=none",
         "--remote-debugging-port=%d" % port, "--remote-allow-origins=*",
         "--window-size=900,600", "--user-data-dir=%s" % (work / "profile"), "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/json" % port, timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            pytest.skip("browser did not start")
        yield port, work
    finally:
        _close_browser(port)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        _remove_dir_with_retry(work / "profile")


@needs_browser
@pytest.mark.parametrize("dpr", [1, 1.25, 1.5])
def test_no_accent_pixels_in_the_sticky_header_band_in_a_real_browser(dpr, browser_port):
    """60 accent rows, scrolled ~230px: the header band (title + filter row) must
    contain only grey pixels. Up to 4 stray anti-aliased pixels are tolerated at
    the wrapper's rounded corner (the old stylesheet gives well over 1000)."""
    port, work = browser_port
    assert _bleed_pixels(_page_html(), dpr, port, work) <= 4


@needs_browser
def test_the_browser_check_detects_the_old_collapsed_table(browser_port):
    """NEGATIVE control: the same page with the old collapsed-border CSS DOES bleed,
    so a clean result above is not a blind test."""
    port, work = browser_port
    assert _bleed_pixels(_page_html(collapsed=True), 1, port, work) > 200


@needs_browser
def test_a_run_of_the_browser_tests_leaves_nothing_in_the_temp_folder(tmp_path):
    """NEGATIVE: run the real-browser tests above in a child pytest whose temp
    folder is a fresh empty one, then check that no browser profile, and no
    leftover of any size worth the name, remains in it. (Before this was fixed
    every run left about 7 MB of browser profile in the temp folder.)"""
    sandbox = tmp_path / "sandbox-temp"
    sandbox.mkdir()
    env = dict(os.environ, TMP=str(sandbox), TEMP=str(sandbox), TMPDIR=str(sandbox))
    cp = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:warnings", "-v", "-p", "no:cacheprovider",
         "-k", "real_browser or detects_the_old", str(Path(__file__).resolve())],
        env=env, capture_output=True, text=True, timeout=600, cwd=str(ROOT))
    assert cp.returncode == 0, cp.stdout[-2000:] + cp.stderr[-1000:]
    assert "4 passed" in cp.stdout, cp.stdout[-1500:]
    left = []
    total = 0
    for dp, dn, fn in os.walk(sandbox):
        for d in dn:
            if d.lower() == "profile":
                left.append(str(Path(dp) / d))
        for f in fn:
            try:
                total += (Path(dp) / f).stat().st_size
            except OSError:
                pass
    assert not left, "browser profile left behind: %s" % left
    assert total < 1_000_000, "%d bytes left in the temp folder" % total
