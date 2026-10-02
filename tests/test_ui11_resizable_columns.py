"""
UI-11 -- drag the right edge of a column header to resize the column, on every
review screen built on ui/_review_engine.py.

Three layers:
  * node + fake DOM: width changes, survives a re-render, Reset clears it, and the
    negatives (a drag does not select a row, widths do not leak between screens);
  * one test per screen (gnucash_review, krc_gnucash_review, tds_journal_review,
    itr_mapping_review) proving the rendered page carries the handles + Reset control;
  * REAL mouse: headless Edge/Chrome driven over the DevTools protocol with
    Input.dispatchMouseEvent (drag, double-click, reload, reset, sticky header,
    no sort / select / edit / focus theft). Skipped when no browser or aiohttp.

All data is synthetic.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests", ROOT / "ui", ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _review_js as js  # noqa: E402
from ui import _review_engine as eng  # noqa: E402

needs_node = pytest.mark.skipif(not js.have_node(), reason="node not installed")


def _spec(app="rw1"):
    return eng.ReviewSpec(
        app_id=app,
        columns=[
            eng.Column("Date", "Date"),
            eng.Column("Description", "Description", edit_key="EditedDesc"),
            eng.Column("Account", "Account"),
        ],
        target_col="Account",
        payload_var="__%s_payload" % app,
        allow_delete=True,
    )


def _rows(n=4):
    return [{"Date": "2025-06-%02d" % (i + 1),
             "Description": "ROW %d %s" % (i, "LONG TEXT " * 12),
             "Account": "Expenses:Food"} for i in range(n)]


def _html(app="rw1", n=4):
    return eng.build_html(_spec(app), _rows(n))


def _run(tmp_path, scenario, app="rw1", html=None):
    return js.run_js(html or _html(app), app, scenario, tmp_path)


# Fake-DOM helper: a resize handle that lives inside the (string-built) thead
_HANDLE = """
  const handle = (key) => { const h = document.createElement('span');
    h.classList.add('col-resizer'); h.dataset.col = key; h.parentNode = $id(APP + '-thead'); return h; };
  const dragBy = (key, dx) => { const h = handle(key);
    dispatch(h, 'mousedown', {clientX: 500});
    (document._l.mousemove || []).slice().forEach(f => f({clientX: 500 + dx}));
    (document._l.mouseup || []).slice().forEach(f => f({clientX: 500 + dx}));
    return h; };
  const W = () => JSON.parse(localStorage.getItem('pask.colw.' + APP) || 'null');
"""


# ---- 1. positive ---------------------------------------------------------------------

@needs_node
def test_dragging_a_handle_changes_that_columns_width(tmp_path):
    out = _run(tmp_path, _HANDLE + """
        const before = W();
        dragBy('Description', 80);
        const w = W();
        return {before, w, cols: $id(APP + '-cols')._h, resized: $id(APP + '-table').classList.contains('resized')};
    """)
    assert out["before"] is None
    assert out["w"]["Description"] == 160 + 80      # fallback base 160 in the fake DOM
    assert out["w"]["Date"] == 160 and out["w"]["Account"] == 160   # others frozen, not squeezed
    assert out["resized"] is True
    assert out["cols"].count("<col ") == 3 and "width:240px" in out["cols"]


@needs_node
def test_drag_never_goes_below_the_minimum_width(tmp_path):
    out = _run(tmp_path, _HANDLE + "dragBy('Date', -5000); return W();")
    assert out["Date"] == 40


@needs_node
def test_width_survives_a_rerender_filter_and_a_fresh_page_load(tmp_path):
    out = _run(tmp_path, _HANDLE + """
        dragBy('Description', 100);
        typeIn(APP, 'Description', 'ROW 1');            // filter -> renderTable()
        const afterFilter = $id(APP + '-cols')._h;
        typeIn(APP, 'Description', '');
        return {afterFilter, saved: W()};
    """)
    assert "width:260px" in out["afterFilter"]
    assert out["saved"]["Description"] == 260
    # "app restart": a second page load reads the saved widths back
    out2 = js.run_js(_html(), "rw1", "return {cols: $id(APP + '-cols')._h};", tmp_path,
                     pre="localStorage.setItem('pask.colw.rw1', %s);" % json.dumps(json.dumps(out["saved"])))
    assert "width:260px" in out2["cols"]


@needs_node
def test_reset_widths_clears_saved_widths_and_restores_auto_layout(tmp_path):
    out = _run(tmp_path, _HANDLE + """
        dragBy('Description', 100);
        const had = W();
        $id(APP + '-reset-widths').onclick({preventDefault() {}});
        return {had, after: W(), cols: $id(APP + '-cols')._h,
                resized: $id(APP + '-table').classList.contains('resized'),
                width: $id(APP + '-table').style.width};
    """)
    assert out["had"] is not None
    assert out["after"] is None and out["cols"] == "" and out["resized"] is False and out["width"] == ""


@needs_node
def test_works_with_no_storage_at_all(tmp_path):
    out = _run(tmp_path, """
        Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: {
          getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); },
          removeItem() { throw new Error('blocked'); } } });
    """ + _HANDLE.replace("const W = () => JSON.parse(localStorage.getItem('pask.colw.' + APP) || 'null');", "") + """
        dragBy('Description', 50);
        return {cols: $id(APP + '-cols')._h};
    """)
    assert "width:210px" in out["cols"]


# ---- 2. negatives --------------------------------------------------------------------

@needs_node
def test_a_resize_does_not_select_a_row_or_open_an_edit(tmp_path):
    out = _run(tmp_path, _HANDLE + """
        const before = view(APP).map(r => r.cls.join(' '));
        dragBy('Description', 60);
        const cell = cellAt(APP, 0, 'Description');
        return {before, after: view(APP).map(r => r.cls.join(' ')),
                selected: view(APP).filter(r => r.cls.includes('selected')).length,
                inputs: inputsIn(cell).length};
    """)
    assert out["after"] == out["before"]
    assert out["selected"] == 0 and out["inputs"] == 0


@needs_node
def test_mousedown_on_a_handle_is_fully_swallowed(tmp_path):
    """No default action (no text selection, no focus change) and no bubbling to the th."""
    out = _run(tmp_path, _HANDLE + """
        const ev = dispatch(handle('Date'), 'mousedown', {clientX: 10});
        return {prevented: ev.prevented, stopped: ev.stopped};
    """)
    assert out["prevented"] is True and out["stopped"] is True


@needs_node
def test_widths_saved_on_one_screen_do_not_leak_to_another(tmp_path):
    a, b = _html("rwa"), _html("rwb")
    out = js.run_js(a, "rwa", _HANDLE + """
        dragBy('Description', 90);
        return {a: localStorage.getItem('pask.colw.rwa'), b: localStorage.getItem('pask.colw.rwb')};
    """, tmp_path)
    assert out["a"] and out["b"] is None
    # a fresh "rwb" page that only has rwa's key in storage must come up un-resized
    out2 = js.run_js(b, "rwb", "return {cols: $id(APP + '-cols')._h};", tmp_path,
                     pre="localStorage.setItem('pask.colw.rwa', %s);" % json.dumps(json.dumps(out["a"])))
    assert out2["cols"] == ""


def test_keys_are_namespaced_by_the_app_id_in_the_template():
    a, b = _html("rwa"), _html("rwb")
    assert "'pask.colw.' + APP" in a and "const APP" in a or "APP" in a
    assert 'id="rwa-reset-widths"' in a and 'id="rwb-reset-widths"' in b
    assert 'id="rwa-reset-widths"' not in b


# ---- 3. one test per screen -----------------------------------------------------------

def _assert_screen_has_resizing(html, app_id):
    assert re.findall(r"%%[A-Z_]*%%", html) == []
    assert "col-resizer" in html                       # handle CSS + markup in the init code
    assert 'id="%s-reset-widths"' % app_id in html
    assert "Reset widths" in html
    assert 'id="%s-cols"' % app_id in html             # the colgroup the widths are applied through
    assert 'id="%s-table"' % app_id in html
    assert "overflow-x: auto" in html                  # the wrapper scrolls sideways once resized


def test_gnucash_review_screen_has_resize_handles_and_reset(tmp_path):
    from ui.tabs import gnucash_review as m
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text("Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n"
                 "2025-05-01,COFFEE,Expense:Food,,100.00,900.00,high,rule\n", encoding="utf-8")
    _assert_screen_has_resizing(m._load_review_data(str(p), str(p)), m.APP_ID)


def test_krc_gnucash_review_screen_has_resize_handles_and_reset(tmp_path):
    from ui.tabs import krc_gnucash_review as m
    p = tmp_path / "Review.csv"
    p.write_text("CN No,Type,Security,Net,Reason\n"
                 'CN1,Sale,SYNCORP LTD,1000.00,"no security account match (fuzzy:0.30) for \'SYNCORP LTD\'"\n',
                 encoding="utf-8")
    g = tmp_path / "book.gnucash"
    g.write_text("not a real book", encoding="utf-8")
    _assert_screen_has_resizing(m._load_review_data(str(p), str(g)), m.APP_ID)


def test_tds_journal_review_screen_has_resize_handles_and_reset(tmp_path):
    from ui.tabs import tds_journal_review as m
    from test_tds_journal_review import _review_row
    p = tmp_path / "2026-FY2526-tds-journals-review.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=m._REVIEW_HEADERS)
        w.writeheader()
        w.writerow(_review_row())
    _assert_screen_has_resizing(m._load_review_data(str(p), ""), m.APP_ID)


def test_itr_mapping_review_screen_has_resize_handles_and_reset(tmp_path):
    import yaml
    from ui.tabs import itr_mapping_review as m
    data_root = tmp_path / "Data"
    (data_root / "itr" / "mappings").mkdir(parents=True)
    (data_root / "itr" / "entities.yaml").write_text(yaml.safe_dump({
        "SYN-IND": {"name": "Synthetic Individual", "pan": "AAAAA0000A", "status": "Individual",
                    "default_regime": "new", "workbook_match": "SYN-IND"}}), encoding="utf-8")
    (data_root / "itr" / "mappings" / "SYN-IND.mapping.yaml").write_text(yaml.safe_dump([
        {"guid": "g1", "path": "Income/Bank Interest", "tag": "OS_INTEREST_BANK",
         "suggested_by_llm": None, "note": ""}], sort_keys=False), encoding="utf-8")
    with patch("ui._config.data_root_dir", return_value=data_root):
        html = m._load_review_data("SYN-IND")
    _assert_screen_has_resizing(html, m.APP_ID)
    # the tag glossary is a reference panel, not a review grid: it must NOT be given handles
    gl = re.search(r"<table[^>]*>.*?</table>", html[html.find("glossary"):], re.S)
    assert gl is None or "col-resizer" not in gl.group(0)


# ---- 4. real mouse, real browser -------------------------------------------------------

def _browser():
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              shutil.which("msedge") or "", shutil.which("chrome") or "",
              shutil.which("google-chrome") or ""):
        if p and os.path.exists(p):
            return p
    return None


def _have_stack():
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return False
    return _browser() is not None


needs_browser = pytest.mark.skipif(not _have_stack(), reason="no headless Edge/Chrome or aiohttp")


class _Cdp:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def call(self, method, **params):
        self.n += 1
        await self.ws.send_json({"id": self.n, "method": method, "params": params})
        while True:
            msg = await self.ws.receive_json()
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result", {})

    async def ev(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"])
        return r["result"].get("value")

    async def mouse(self, kind, x, y, count=1, buttons=1):
        await self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left",
                        buttons=buttons if kind != "mouseReleased" else 0, clickCount=count)

    async def drag(self, x, y, dx, steps=6):
        await self.mouse("mouseMoved", x, y, 0, 0)
        await self.mouse("mousePressed", x, y)
        for i in range(1, steps + 1):
            await self.mouse("mouseMoved", x + dx * i / steps, y)
        await self.mouse("mouseReleased", x + dx, y)

    async def dblclick(self, x, y):
        await self.mouse("mouseMoved", x, y, 0, 0)
        for c in (1, 2):
            await self.mouse("mousePressed", x, y, c)
            await self.mouse("mouseReleased", x, y, c)

    async def reload(self, first=False):
        if not first:
            await self.call("Page.reload")
        for _ in range(100):
            await asyncio.sleep(0.1)
            try:
                if await self.ev("!!document.getElementById('rw1-thead') && "
                                 "document.querySelectorAll('#rw1-tbody tr').length > 0"):
                    return
            except RuntimeError:
                pass
        raise AssertionError("page did not come back")


_Q = """
(function(){
  const $ = (s) => document.querySelector(s);
  const th = (k) => $('#rw1-thead th[data-col="' + k + '"]');
  const rect = (e) => { const r = e.getBoundingClientRect(); return {l: r.left, r: r.right, t: r.top, b: r.bottom, w: r.width}; };
  const w = $('.scroll-wrapper');
  const ths = [...document.querySelectorAll('#rw1-thead th')];
  const tds = [...document.querySelectorAll('#rw1-tbody tr:first-child td')];
  return JSON.stringify({
    W: Object.fromEntries(ths.map(t => [t.dataset.col, Math.round(t.getBoundingClientRect().width)])),
    handle: Object.fromEntries(ths.map(t => [t.dataset.col, rect(t.querySelector('.col-resizer'))])),
    sortSig: [...document.querySelectorAll('#rw1-thead .sort-arrow')].map(a => a.parentNode.dataset.col + a.textContent).join(),
    selected: document.querySelectorAll('#rw1-tbody tr.selected').length,
    editing: document.querySelectorAll('#rw1-tbody input, #rw1-tbody textarea').length,
    active: document.activeElement ? (document.activeElement.dataset.col || document.activeElement.tagName) : null,
    store: localStorage.getItem('pask.colw.rw1'),
    other: localStorage.getItem('pask.colw.rw2'),
    wrap: {l: rect(w).l, t: rect(w).t, r: rect(w).r, sl: w.scrollLeft, st: w.scrollTop, sw: w.scrollWidth, cw: w.clientWidth},
    thead: rect($('#rw1-thead')),
    thL: ths.map(t => rect(t).l), tdL: tds.map(t => rect(t).l),
    tableW: rect($('#rw1-table')).w,
    ell: getComputedStyle(tds[1]).textOverflow,
    tdTitle: tds[1].title || '',
  });
})()
"""


async def _scenario(page_url, port, tmp):
    import aiohttp
    out = {}
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % port))
    ws_url = [t for t in targets if t.get("type") == "page"][0]["webSocketDebuggerUrl"]
    async with aiohttp.ClientSession() as sess:
        async with sess.ws_connect(ws_url, max_msg_size=0) as ws:
            c = _Cdp(ws)
            await c.call("Page.enable")
            await c.call("Emulation.setDeviceMetricsOverride", width=1000, height=700,
                         deviceScaleFactor=1, mobile=False)
            await c.call("Page.navigate", url=page_url)
            await c.reload(first=True)
            await c.ev("localStorage.clear()")
            await c.reload()
            q = lambda: c.ev(_Q)
            s0 = json.loads(await q())
            out["s0"] = s0
            hd = s0["handle"]["Description"]
            x, y = (hd["l"] + hd["r"]) / 2, (hd["t"] + hd["b"]) / 2

            # 1 real drag on the Description handle, +120px
            await c.drag(x, y, 120)
            s1 = json.loads(await q())
            out["s1"] = s1

            # 2 reload: the width comes back (app restart)
            await c.reload()
            out["s2"] = json.loads(await q())

            # 3 handle drag of a different column does not move the first one's saved value
            hdate = out["s2"]["handle"]["Date"]
            await c.drag((hdate["l"] + hdate["r"]) / 2, (hdate["t"] + hdate["b"]) / 2, -30)
            out["s3"] = json.loads(await q())

            # 4 focus in a filter box survives a drag of another column's handle
            await c.ev("document.querySelector('#rw1-thead .filter-row input').focus()")
            out["s3b"] = json.loads(await q())
            ha = out["s3b"]["handle"]["Description"]
            await c.drag((ha["l"] + ha["r"]) / 2, (ha["t"] + ha["b"]) / 2, -20)
            out["s4"] = json.loads(await q())

            # 5 sticky header + sideways scroll: scroll both ways, header must still sit on top
            # and its cells stay aligned with the body cells
            await c.ev("(function(){var w=document.querySelector('.scroll-wrapper');"
                       "w.scrollLeft=9999;w.scrollTop=9999;})()")
            await asyncio.sleep(0.2)
            out["s5"] = json.loads(await q())
            await c.ev("(function(){var w=document.querySelector('.scroll-wrapper');w.scrollLeft=0;w.scrollTop=0;})()")

            # 6 double-click the Description handle: fit-to-content
            s6a = json.loads(await q())
            hd = s6a["handle"]["Description"]
            await c.dblclick((hd["l"] + hd["r"]) / 2, (hd["t"] + hd["b"]) / 2)
            out["s6"] = json.loads(await q())
            out["s6a"] = s6a

            # 7 plain click on a header (not the handle) DOES sort: the control that
            # proves "no sort" above is not a blind check
            hh = out["s6"]["handle"]["Date"]
            await c.mouse("mouseMoved", hh["l"] - 20, (hh["t"] + hh["b"]) / 2, 0, 0)
            await c.mouse("mousePressed", hh["l"] - 20, (hh["t"] + hh["b"]) / 2)
            await c.mouse("mouseReleased", hh["l"] - 20, (hh["t"] + hh["b"]) / 2)
            out["s7"] = json.loads(await q())

            # 8 Reset widths
            await c.ev("document.getElementById('rw1-reset-widths').click()")
            out["s8"] = json.loads(await q())
    return out


def _page(tmp):
    spec = _spec("rw1")
    spec_cols = spec.columns
    html = eng.build_html(spec, _rows(40))
    page = ("<!doctype html><meta charset=utf-8><body style='margin:0;background:#0f0f0f'>" + html)
    f = Path(tmp) / "page.html"
    f.write_text(page, encoding="utf-8")
    return f.as_uri()


@pytest.fixture(scope="module")
def browser_run():
    if not _have_stack():
        pytest.skip("no headless Edge/Chrome or aiohttp")
    with tempfile.TemporaryDirectory() as tmp:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        proc = subprocess.Popen(
            [_browser(), "--headless=new", "--disable-gpu", "--no-first-run",
             "--remote-debugging-port=%d" % port, "--remote-allow-origins=*",
             "--window-size=1000,700", "--user-data-dir=%s" % (Path(tmp) / "profile"), "about:blank"],
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
            yield asyncio.run(_scenario(_page(tmp), port, tmp))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass


@needs_browser
def test_real_drag_widens_only_that_column(browser_run):
    s0, s1 = browser_run["s0"], browser_run["s1"]
    assert 100 <= s1["W"]["Description"] - s0["W"]["Description"] <= 140
    assert abs(s1["W"]["Date"] - s0["W"]["Date"]) <= 1
    assert abs(s1["W"]["Account"] - s0["W"]["Account"]) <= 1
    assert json.loads(s1["store"])["Description"] == s1["W"]["Description"]


@needs_browser
def test_real_drag_does_not_sort_select_or_edit(browser_run):
    for k in ("s1", "s3", "s4"):
        s = browser_run[k]
        assert s["sortSig"] == browser_run["s0"]["sortSig"] and s["selected"] == 0 and s["editing"] == 0, k


@needs_browser
def test_real_header_click_still_sorts_so_the_check_above_is_not_blind(browser_run):
    assert browser_run["s7"]["sortSig"] != browser_run["s6"]["sortSig"]


@needs_browser
def test_real_width_survives_a_reload(browser_run):
    assert abs(browser_run["s2"]["W"]["Description"] - browser_run["s1"]["W"]["Description"]) <= 1


@needs_browser
def test_real_drag_keeps_focus_in_a_filter_box(browser_run):
    assert browser_run["s3b"]["active"] == "Date"          # the box really had focus (setup is not blind)
    assert browser_run["s3b"]["W"]["Description"] - browser_run["s4"]["W"]["Description"] >= 15   # the drag really happened
    assert browser_run["s4"]["active"] == "Date"           # ...and a drag of another handle did not steal it


@needs_browser
def test_real_resized_table_scrolls_sideways_and_header_stays_aligned(browser_run):
    s = browser_run["s5"]
    assert s["wrap"]["sw"] > s["wrap"]["cw"]                       # wrapper scrolls horizontally
    assert s["wrap"]["sl"] > 0 and s["wrap"]["st"] > 0              # scrolled both ways
    assert abs(s["thead"]["t"] - s["wrap"]["t"]) <= 3               # sticky header still at the top
    for a, b in zip(s["thL"], s["tdL"]):
        assert abs(a - b) <= 1                                      # header cells line up with body cells


@needs_browser
def test_real_double_click_fits_the_column_to_its_content(browser_run):
    a, b = browser_run["s6a"]["W"]["Description"], browser_run["s6"]["W"]["Description"]
    assert b != a and b >= 40
    assert browser_run["s6"]["sortSig"] == browser_run["s0"]["sortSig"] and browser_run["s6"]["editing"] == 0


@needs_browser
def test_real_clipped_text_has_ellipsis(browser_run):
    assert browser_run["s1"]["ell"] == "ellipsis"


@needs_browser
def test_real_reset_clears_saved_widths_and_restores_auto_layout(browser_run):
    s8 = browser_run["s8"]
    assert s8["store"] is None
    assert s8["tableW"] >= browser_run["s0"]["tableW"] - 2 and s8["tableW"] <= s8["wrap"]["r"] - s8["wrap"]["l"] + 1


@needs_browser
def test_real_widths_do_not_leak_to_another_screen_key(browser_run):
    assert browser_run["s1"]["other"] is None
