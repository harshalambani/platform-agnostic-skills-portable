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
    tag, children: [], dataset: {}, style: {}, value: '', title: '',
    className: '', textContent: '', _h: '', onclick: null,
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    appendChild(c) { this.children.push(c); return c; },
    querySelectorAll() { return []; }, querySelector() { return null; },
    addEventListener() {}, closest() { return null; }, focus() {},
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
