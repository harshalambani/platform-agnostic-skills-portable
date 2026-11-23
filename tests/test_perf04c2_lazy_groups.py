"""PERF-04 C2: the heavy GnuCash groups are built after the page is drawn.

Synthetic only. The real launched app is driven the way the page drives it:
POST the page-load events to /queue/join, read the render results off the
/queue/data stream. Nothing here reads user data.
"""
from __future__ import annotations

import json
import socket
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import gradio as gr  # noqa: E402

from ui import _lazy_tabs, webui  # noqa: E402
from ui.tabs import _generic  # noqa: E402
from ui.tabs import home as tab_home  # noqa: E402

GROUPS = ["Banks", "Partner", "Inter-entity", "26AS", "KRChoksey", "ITR", "Entities"]
LOADING = "Loading models"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _render_deps(cfg):
    return [d for d in cfg["dependencies"]
            if d.get("render_id") is not None and d.get("rendered_in") is None]


def fire_page_load(base: str, cfg: dict, session: str | None = None):
    """Fire every render dependency the way a page load does; returns
    ({render_id: process_completed output}, session_hash)."""
    sh = session or uuid.uuid4().hex
    deps = _render_deps(cfg)
    for d in deps:
        r = httpx.post(f"{base}/gradio_api/queue/join", timeout=30, json={
            "data": [], "fn_index": d["id"], "session_hash": sh, "trigger_id": None})
        assert r.status_code == 200
    return _collect(base, sh, len(deps)), sh


def _collect(base, sh, n, timeout=120):
    out, got = {}, 0
    deadline = time.time() + timeout
    with httpx.stream("GET", f"{base}/gradio_api/queue/data?session_hash={sh}", timeout=timeout) as r:
        for line in r.iter_lines():
            if time.time() > deadline:
                break
            if not line.startswith("data:"):
                continue
            m = json.loads(line[5:])
            if m.get("msg") == "process_completed":
                assert m.get("success"), m
                rc = m["output"].get("render_config")
                if rc is not None:
                    out[rc["render_id"]] = m["output"]
                got += 1
                if got >= n:
                    break
    return out


def fire_dep(base, sh, dep_id):
    httpx.post(f"{base}/gradio_api/queue/join", timeout=30, json={
        "data": [], "fn_index": dep_id, "session_hash": sh, "trigger_id": None})


def _tab_labels(components) -> set:
    return {c["props"].get("label") for c in components if c["type"] == "tabitem"}


def _descendant_tab_labels(app) -> dict:
    """GnuCash sub-tab label -> labels of every tab nested under it (eager build)."""
    from gradio.layouts import Tab

    def walk(block):
        yield block
        for c in getattr(block, "children", []) or []:
            yield from walk(c)

    gc = next(b for b in app.blocks.values() if isinstance(b, Tab) and b.label == "GnuCash")
    out = {}
    for sub in (b for b in walk(gc) if isinstance(b, Tab) and b is not gc):
        if sub.label in GROUPS and sub.label not in out:
            out[sub.label] = {t.label for t in walk(sub) if isinstance(t, Tab) and t is not sub}
    return out


# ---------------------------------------------------------------- real app ---

@pytest.fixture(scope="module")
def real():
    mp = pytest.MonkeyPatch()
    mp.setattr(_generic, "_refresh_models", lambda **kw: [("m1 (tools)", "m1")])
    mp.setattr(_generic, "_default_model_value", lambda ch: "m1")
    eager = webui.build_app(launch=False)
    eager_cfg = eager.get_config_file()
    eager_tabs = _descendant_tab_labels(eager)
    lazy = webui.build_app(launch=False, lazy=True)
    port = _free_port()
    lazy.launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True, quiet=True)
    try:
        yield {"app": lazy, "cfg": lazy.get_config_file(), "base": f"http://127.0.0.1:{port}",
               "eager": eager, "eager_cfg": eager_cfg, "eager_tabs": eager_tabs}
    finally:
        lazy.close()
        mp.undo()


@pytest.fixture(scope="module")
def loaded(real):
    _lazy_tabs.reset_build_log()
    outs, sh = fire_page_load(real["base"], real["cfg"])
    return {"outs": outs, "sh": sh, "log": _lazy_tabs.build_log()}


def test_unlaunched_build_stays_eager(real):  # requirement h
    cfg = real["eager_cfg"]
    assert _render_deps(cfg) == []
    labels = _tab_labels(cfg["components"])
    assert set(GROUPS) <= labels
    assert {"Review", "Review Mapping", "Advance Tax"} <= labels
    assert real["app"].renderables and not real["eager"].renderables


def test_lazy_page_starts_with_the_groups_empty(real):
    cfg = real["cfg"]
    labels = _tab_labels(cfg["components"])
    assert set(GROUPS) <= labels                      # every tab button is on the page
    assert "Review" not in labels and "Review Mapping" not in labels   # their contents are not
    assert len(cfg["components"]) < len(real["eager_cfg"]["components"]) // 2
    assert len(_render_deps(cfg)) == 7


def test_each_group_builds_exactly_once_per_page_load_in_order(real, loaded):  # a
    assert loaded["log"] == GROUPS
    fire_page_load(real["base"], real["cfg"])         # a second page load: once more each
    assert _lazy_tabs.build_log() == GROUPS * 2


def test_renders_are_triggered_by_page_load_only(real):  # NEGATIVE a
    for d in _render_deps(real["cfg"]):
        assert [t[1] for t in d["targets"]] == ["load"], d["targets"]


def test_groups_run_one_at_a_time_through_one_queue_slot(real):  # b
    fns = real["app"].fns
    ids = {d["id"] for d in _render_deps(real["cfg"])}
    assert {fns[i].concurrency_id for i in ids} == {_lazy_tabs.CONCURRENCY_ID}
    assert {fns[i].concurrency_limit for i in ids} == {1}


def test_every_group_lands_with_content(loaded):  # NEGATIVE b: never left empty
    assert len(loaded["outs"]) == 7
    for rid, out in loaded["outs"].items():
        assert len(out["render_config"]["components"]) > 5, rid


def test_each_group_has_only_its_own_tabs(real, loaded):  # NEGATIVE d
    got = [frozenset(_tab_labels(o["render_config"]["components"])) for o in loaded["outs"].values()]
    want = [frozenset(v) for v in real["eager_tabs"].values()]
    assert sorted(map(sorted, got)) == sorted(map(sorted, want))
    # the tabs only Banks has are never in another group's build, and so on
    for g, mine in real["eager_tabs"].items():
        others = set().union(*[v for k, v in real["eager_tabs"].items() if k != g])
        own_only = mine - others
        for labels in got:
            if labels != frozenset(mine):
                assert not (own_only & labels), (g, own_only & labels)


def test_lazy_model_dropdowns_are_filled_not_left_loading(loaded):  # NEGATIVE c
    seen = 0
    for out in loaded["outs"].values():
        for c in out["render_config"]["components"]:
            if c["type"] == "dropdown" and c["props"].get("label") == "Model":
                seen += 1
                assert c["props"]["choices"] == [["m1 (tools)", "m1"]]
                assert c["props"]["value"] == "m1"
                assert LOADING not in json.dumps(c["props"]["choices"])
    assert seen >= 1


def test_lazy_dropdowns_do_not_leak_into_the_page_wide_registry(real, loaded):
    before = len(_generic._deferred_model_dropdowns)
    fire_page_load(real["base"], real["cfg"])
    assert len(_generic._deferred_model_dropdowns) == before


def test_model_focus_refresh_is_wired_once_per_lazy_dropdown(loaded):  # NEGATIVE f
    n_dd = n_focus = 0
    for out in loaded["outs"].values():
        rc = out["render_config"]
        dds = {c["id"] for c in rc["components"]
               if c["type"] == "dropdown" and c["props"].get("label") == "Model"
               and c["props"].get("visible", True)}      # only LLM skills show it
        n_dd += len(dds)
        n_focus += sum(1 for d in rc["dependencies"]
                       if any(t[1] == "focus" and t[0] in dds for t in d["targets"]))
    assert n_dd and n_focus == n_dd                   # none missing, none doubled


def test_focus_refresh_total_matches_the_eager_page(real, loaded):  # NEGATIVE f
    def focus_deps(cfg):
        return sum(1 for d in cfg["dependencies"] if any(t[1] == "focus" for t in d["targets"]))
    lazy_total = focus_deps(real["cfg"]) + sum(
        focus_deps(o["render_config"]) for o in loaded["outs"].values())
    assert lazy_total == focus_deps(real["eager_cfg"])


def test_page_wide_loads_only_target_components_that_exist_at_load(real):  # NEGATIVE c
    cfg = real["cfg"]
    page_ids = {c["id"] for c in cfg["components"]}
    for d in cfg["dependencies"]:
        if d.get("render_id") is None and any(t[1] == "load" for t in d["targets"]):
            assert set(d["outputs"]) <= page_ids
    assert len(tab_home._deferred_status_panels) <= 1


def test_no_duplicated_or_missing_handlers_after_the_build(real, loaded):  # NEGATIVE f
    eager_n = len(real["eager_cfg"]["dependencies"])
    base_n = len(real["cfg"]["dependencies"])
    built = sum(len(o["render_config"]["dependencies"]) for o in loaded["outs"].values())
    # 7 render triggers are the only additions; the page-wide model fill is
    # the one event that may disappear when no eager dropdown is left
    assert base_n + built - 7 in (eager_n, eager_n - 1), (base_n, built, eager_n)
    ids = [d["id"] for o in loaded["outs"].values() for d in o["render_config"]["dependencies"]]
    assert len(ids) == len(set(ids))


def test_review_hooks_exist_only_once_built_and_nothing_looks_early(real, loaded):  # NEGATIVE e
    page = json.dumps(real["cfg"]["components"], default=str)
    built = [c for o in loaded["outs"].values() for c in o["render_config"]["components"]]
    hooks = {c["props"].get("elem_id") for c in built
             if (c["props"].get("elem_id") or "").endswith("-payload-box")}
    assert len(hooks) >= 4                            # gnucash, tds, krc, itr review boxes
    for h in hooks:
        assert h not in page                          # not on the page before the build
        assert h not in webui._ZOOM_HEAD + webui.APP_CSS
    for o in loaded["outs"].values():
        for d in o["render_config"]["dependencies"]:
            if d.get("js") and "getElementById" in d["js"]:
                assert not any(t[1] == "load" for t in d["targets"])


def test_learned_rules_are_untouched_by_the_lazy_build(real, monkeypatch):  # NEGATIVE g
    from agents.skill_gnucash_account_mapper import persistent_rules as pr
    fixture = [
        {"patterns": ["^ACME LTD", r"acme\s+pay"], "account": "Expenses:Acme", "added": "2026-01-01"},
        {"pattern": "rent", "account": "Expenses:Rent", "added": "2026-02-02"},
        {"patterns": ["(bad"], "account": "Expenses:Never"},
    ]
    probes = ["ACME LTD invoice", "monthly Rent", "unrelated", "", "acme   pay x"]
    expect = [("Expenses:Acme", "Override (added 2026-01-01)"),
              ("Expenses:Rent", "Override (added 2026-02-02)"),
              (None, ""), (None, ""), ("Expenses:Acme", "Override (added 2026-01-01)")]
    assert [pr.match_overrides(p, fixture) for p in probes] == expect

    def boom(*a, **k):
        raise AssertionError("learned rules were written during the lazy build")
    for name in ("save_rules", "save_overrides_batch", "merge_auto_rules",
                 "prune_one_off_rules", "_write_rules_yaml", "migrate_legacy_overrides"):
        monkeypatch.setattr(pr, name, boom)
    fire_page_load(real["base"], real["cfg"])
    assert [pr.match_overrides(p, fixture) for p in probes] == expect


# ---------------------------------------------------------------- tiny app ---

def _tiny(n_sleep=0.0):
    log = []
    with gr.Blocks() as app:
        with gr.Tabs():
            for label in ("Alpha", "Beta", "Gamma"):
                with gr.Tab(label) as t:
                    def body(tab, label=label):
                        gr.Markdown(f"I am {label}")
                        box = gr.Textbox(value="init")

                        def _sel():
                            log.append(("select", label, time.time()))
                            return "selected"
                        tab.select(fn=_sel, inputs=[], outputs=[box])
                        log.append(("start", label, time.time()))
                        time.sleep(n_sleep)
                        log.append(("end", label, time.time()))
                    _lazy_tabs.register_group(app, t, label, body)
    return app, log


def _launch(app):
    port = _free_port()
    app.launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True, quiet=True)
    return f"http://127.0.0.1:{port}"


def test_tiny_groups_are_staggered_never_overlap_and_never_built_twice():  # NEGATIVE b
    _lazy_tabs.reset_build_log()
    app, log = _tiny(n_sleep=0.4)
    base = _launch(app)
    try:
        outs, _ = fire_page_load(base, app.get_config_file())
        starts = {e[1]: e[2] for e in log if e[0] == "start"}
        ends = {e[1]: e[2] for e in log if e[0] == "end"}
        assert list(starts) == ["Alpha", "Beta", "Gamma"]
        assert ends["Alpha"] <= starts["Beta"] + 0.02 and ends["Beta"] <= starts["Gamma"] + 0.02
        assert _lazy_tabs.build_log() == ["Alpha", "Beta", "Gamma"]
        assert len(outs) == 3
        assert all(len(o["render_config"]["components"]) >= 3 for o in outs.values())
    finally:
        app.close()


def test_tiny_each_group_is_bound_to_its_own_value():  # NEGATIVE d
    app, _ = _tiny()
    base = _launch(app)
    try:
        outs, _ = fire_page_load(base, app.get_config_file())
        said = [c["props"].get("value") for o in outs.values()
                for c in o["render_config"]["components"] if c["type"] == "markdown"]
        assert sorted(said) == ["I am Alpha", "I am Beta", "I am Gamma"]   # not Gamma x3
    finally:
        app.close()


def test_tiny_tab_selects_run_each_time_but_never_rebuild_or_reset():  # NEGATIVE a
    _lazy_tabs.reset_build_log()
    app, log = _tiny()
    base = _launch(app)
    try:
        outs, sh = fire_page_load(base, app.get_config_file())
        sel = [d["id"] for o in outs.values() for d in o["render_config"]["dependencies"]
               if any(t[1] == "select" for t in d["targets"])]
        assert len(sel) == 3
        for _ in range(5):
            for i in sel:
                fire_dep(base, sh, i)
        deadline = time.time() + 30
        while time.time() < deadline and sum(1 for e in log if e[0] == "select") < 15:
            time.sleep(0.1)
        assert sum(1 for e in log if e[0] == "select") == 15      # every switch handled
        assert _lazy_tabs.build_log() == ["Alpha", "Beta", "Gamma"]   # never rebuilt
        assert sum(1 for e in log if e[0] == "start") == 3
    finally:
        app.close()
