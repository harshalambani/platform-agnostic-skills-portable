"""UI-03: the zoom badge. The page script is run under node with a small fake
page, so the real _ZOOM_HEAD code is what is tested."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _script() -> str:
    src = (ROOT / "ui" / "webui.py").read_text(encoding="utf-8")
    start = src.index("_ZOOM_HEAD = ")
    a = src.index('"""', start) + 3
    b = src.index('"""', a)
    m = re.search(r"<script>(.*)</script>", src[a:b], re.S)
    return m.group(1)


HARNESS = r"""
const stored = %(stored)s;
const store = {};
if (stored !== null) store["paskills.zoom"] = stored;
const listeners = {};
const els = [];
function mkEl(tag) {
  const el = {tag, style: {}, textContent: "", id: "", title: "", _l: {},
    addEventListener(t, f) { this._l[t] = f; }};
  return el;
}
global.window = {localStorage: {getItem: k => (k in store ? store[k] : null),
                                setItem: (k, v) => { store[k] = String(v); }}};
global.document = {readyState: "complete", documentElement: {style: {}},
  body: {appendChild(e) { els.push(e); }},
  createElement: mkEl,
  addEventListener(t, f) { listeners[t] = f; }};
global.setTimeout = () => 1; global.clearTimeout = () => {};
%(script)s
function key(k) { listeners.keydown({ctrlKey: true, metaKey: false, key: k, preventDefault() {}}); }
const badge = () => els.find(e => e.id === "pask-zoom-badge");
const out = {};
%(steps)s
console.log(JSON.stringify(out));
"""


def _run(stored, steps):
    code = HARNESS % {"stored": json.dumps(stored), "script": _script(), "steps": steps}
    r = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


STATE = ("out.zoom = document.documentElement.style.zoom; "
         "out.stored = store['paskills.zoom']; "
         "const b = badge(); out.text = b ? b.textContent : null; out.display = b ? b.style.display : null;")


def test_badge_is_hidden_at_100_percent():                          # NEGATIVE
    o = _run(None, STATE)
    assert o["zoom"] == "1" and o["display"] in (None, "none") and not o["text"]


def test_badge_shows_the_percentage_after_a_zoom_change():
    o = _run(None, "key('='); " + STATE)
    assert o["text"] == "110%" and o["display"] == "block" and o["zoom"] == "1.1"


def test_badge_shows_on_load_when_a_zoom_is_stored():
    o = _run("1.2", STATE)
    assert o["text"] == "120%" and o["display"] == "block"


def test_clicking_the_badge_resets_to_exactly_one():
    o = _run(None, "key('='); key('='); badge()._l.click(); " + STATE)
    assert o["zoom"] == "1" and o["stored"] == "1" and o["display"] == "none" and not o["text"]


def test_ctrl_zero_also_hides_the_badge():
    o = _run(None, "key('-'); key('0'); " + STATE)
    assert o["zoom"] == "1" and o["display"] == "none"


def test_reset_never_leaves_the_zoom_anywhere_but_one():            # NEGATIVE
    # Mix of steps then reset: the zoom is 1 and 100% is never displayed as a badge.
    o = _run("2.4", "key('+'); key('+'); key('+'); badge()._l.click(); " + STATE)
    assert o["zoom"] == "1" and o["stored"] == "1" and o["display"] == "none"


@pytest.mark.parametrize("bad", ["abc", "", "NaN", "0.5abc", "9", "-1", "0", "Infinity", "1e999"])
def test_a_bad_stored_value_never_shows_a_wrong_percentage(bad):    # NEGATIVE
    o = _run(bad, STATE)
    assert o["zoom"] == "1" and o["display"] in (None, "none") and not o["text"]


def test_a_stored_value_near_one_is_not_shown_as_a_false_100():     # NEGATIVE
    # 1.004 rounds to 1.0: the page is at 100% and no badge says otherwise.
    o = _run("1.004", STATE)
    assert o["zoom"] == "1" and o["display"] in (None, "none")


def test_zoom_is_kept_between_the_limits():
    o = _run(None, "for (let i = 0; i < 40; i++) key('='); " + STATE)
    assert o["text"] == "250%"
