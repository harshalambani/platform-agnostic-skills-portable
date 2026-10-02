"""
Tiny node-based harness that runs the review engine's client code against a
fake DOM, so tests can assert on what the user would actually SEE after a click
(badges, tags, filter results) rather than on strings in the template.

Skipped by callers when node is not installed (see `have_node`).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

NODE = shutil.which("node")


def have_node() -> bool:
    return NODE is not None


_FAKE_DOM = r"""
function mkEl(tag) {
  const e = {
    tag, tagName: String(tag).toUpperCase(), children: [], dataset: {}, style: {}, value: '', title: '',
    className: '', textContent: '', _h: '', onclick: null,
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(c) { this.children.push(c); return c; },
    querySelectorAll() { return []; },
    // only the one selector the engine uses to find a filter box
    querySelector(sel) {
      const m = /\.filter-row input\[data-col="(.*)"\]/.exec(sel);
      if (!m) return null;
      for (const row of this.children) {
        if (row.className !== 'filter-row') continue;
        for (const td of row.children)
          for (const i of td.children) if (i.dataset.col === m[1]) return i;
      }
      return null;
    },
    contains(x) {
      if (x === this) return true;
      return this.children.some(c => c.contains && c.contains(x));
    },
    addEventListener() {}, closest() { return null; },
    focus() { document.activeElement = this; },
    blur() { if (document.activeElement === this) document.activeElement = null; },
    setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
  };
  Object.defineProperty(e, 'innerHTML', {
    get() { return this._h; },
    set(v) { this._h = v; this.children = []; },
  });
  return e;
}
const _els = {};
globalThis.document = {
  getElementById(id) { return _els[id] || (_els[id] = mkEl('div')); },
  createElement(t) { return mkEl(t); },
  addEventListener() {}, querySelectorAll() { return []; },
  activeElement: null,
};
globalThis.window = globalThis;
globalThis.alert = function () {};
globalThis.confirm = function () { return true; };
const $id = (id) => document.getElementById(id);
function view(app) {
  return $id(app + '-tbody').children.map(tr => ({
    idx: Number(tr.dataset.idx),
    cls: [...tr.classList._s],
    cells: tr.children.map(td => td.innerHTML),
    title: tr.title,
  }));
}
function pick(app, i) {
  const s = $id(app + '-picker-search');
  s.onfocus();
  $id(app + '-picker-dropdown').children[i].onclick();
}
function select(app, idx) {
  const tr = $id(app + '-tbody').children.find(t => Number(t.dataset.idx) === idx);
  tr.onclick({ shiftKey: false, ctrlKey: false, metaKey: false });
}
function applySel(app) { $id(app + '-apply-sel').onclick(); }
function setStatus(app, v) { $id(app + '-status').onchange({ target: { value: v } }); }
function filterBox(app, col) {
  return $id(app + '-thead').querySelector('.filter-row input[data-col="' + col + '"]');
}
// the user types `text` into a filter box: it has focus, value changes, oninput fires
function typeIn(app, col, text) {
  const i = filterBox(app, col);
  i.focus(); i.value = text; i.selectionStart = i.selectionEnd = text.length; i.oninput();
}
// the user clicks somewhere that is not a filter box (focus leaves it)
function clickAway() { document.activeElement = null; }
function focusedCol() {
  const a = document.activeElement;
  return a && a.dataset && a.dataset.col ? a.dataset.col : null;
}
function filterCols(app) {
  const row = $id(app + '-thead').children.find(r => r.className === 'filter-row');
  return row.children.map(td => td.children[0].dataset.col);
}
// the X button inside a filter box's cell, or null (it only exists while the box has text)
function filterX(app, col) {
  const row = $id(app + '-thead').children.find(r => r.className === 'filter-row');
  for (const td of row.children)
    for (const b of td.children) if (b.dataset.clear === col) return b;
  return null;
}
// the stats-line "N filters - Clear all" link
function clearAllLink(app) { return $id(app + '-clear-all'); }
function theadHtml(app) { return $id(app + '-thead')._h; }
function pressEsc(app, col) {
  const ev = { key: 'Escape', stopped: false, prevented: false,
    stopPropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; } };
  filterBox(app, col).onkeydown(ev);
  return ev;
}
function payload(varName) { return JSON.parse(globalThis[varName]); }
"""


def run_js(html: str, app: str, scenario: str, tmp_path: Path):
    """Run the engine's init code against the fake DOM, then `scenario` (JS
    source that may call view/pick/select/applySel/setStatus/payload and must
    `return` a JSON-able value). Returns the parsed result."""
    m = re.search(
        r'<script type="text/plain" id="%s-init-code">(.*?)</script>' % re.escape(app),
        html, re.S)
    assert m, "init code block not found"
    src = (
        _FAKE_DOM
        + "\nconst APP = %s;\n" % json.dumps(app)
        + "eval(%s);\n" % json.dumps(m.group(1))
        + "const __out = (function(){\n" + scenario + "\n})();\n"
        + "console.log(JSON.stringify(__out));\n"
    )
    f = tmp_path / "scenario.js"
    f.write_text(src, encoding="utf-8")
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])
