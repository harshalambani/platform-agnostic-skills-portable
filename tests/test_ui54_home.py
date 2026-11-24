"""UI-54 (3/3): the new Home. Synthetic entities, runs and coverage only, all under pytest's tmp."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ui import (  # noqa: E402
    _book_registry, _config, _coverage_last as CL, _health, _home_data as H, _runhistory as RH,
)

NAMES = {"bank_skill": "Bank -> GnuCash", "cc_skill": "Credit Card - Book Spends"}


def _rec(skill="bank_skill", entity="Entity 1", year="2025-26", result="stopped", why="", ts=None):
    return RH.make_record(skill=skill, entity=entity, year=year, result=result, why=why,
                          ts=ts or "2026-11-21T14:50:00")


def _cov_entry(entity="Entity 1", year="2025-26", gaps=None, label=None, checked="2026-11-18T10:00:00"):
    accounts = {f"Assets:Bank:{n}": {"section": "bank", "gap_months": m, "trailing_months": []}
                for n, m in (gaps or {}).items()}
    return {"entity": entity, "label": label or entity, "year": year, "checked": checked,
            "accounts": accounts}


def _model(history=(), coverage=None, registered=None):
    cov = {f"{e['entity']}|{e['year']}": e for e in (coverage or [])}
    return H.build_needs_you(list(history), cov, registered or {}, NAMES)


def _lines(model):
    return [(m["entity"], y["year"], ln["kind"], ln["text"]) for m in model for y in m["years"] for ln in y["lines"]]


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "dataroot"
    monkeypatch.setattr(_config, "data_root_dir", lambda: r)
    monkeypatch.setitem(RH._state, "warned", False)
    monkeypatch.setitem(CL._state, "warned", False)
    return r


def _tree(r: Path) -> dict:
    return {str(p.relative_to(r)): p.read_bytes() for p in r.rglob("*") if p.is_file()} if r.exists() else {}


# ------------------------------------------------------------------ years ---

def test_ay_becomes_fy_and_fy_stays():
    assert H.normalize_year("AY 2026-27") == "2025-26"
    assert H.normalize_year("AY2026-27") == "2025-26"
    assert H.normalize_year("FY 2025-26") == "2025-26"
    assert H.normalize_year("2025-26") == "2025-26"
    assert H.normalize_year("") == ""
    assert H.normalize_year(None) == ""
    assert H.normalize_year("whatever") == "whatever"


def test_an_ay_run_and_an_fy_coverage_gap_share_one_year_group():
    m = _model([_rec(year="AY 2026-27", result="failed")],
               [_cov_entry(year="2025-26", gaps={"HSBC": ["2025-08"]})])
    assert {y for _, y, _, _ in _lines(m)} == {"2025-26"}


# ------------------------------------------------------------------ problem lines ---

@pytest.mark.parametrize("result", ["stopped", "failed", "attention", "not_ready", "interrupted"])
def test_problem_results_raise_a_line(result):
    got = _lines(_model([_rec(result=result, why="account not found")]))
    assert len(got) == 1 and got[0][2] == "problem" and "Bank -> GnuCash" in got[0][3]
    assert "account not found" in got[0][3]


@pytest.mark.parametrize("result", ["ok", "refused", "cancelled"])
def test_ok_refused_and_cancelled_raise_nothing(result):  # NEGATIVE
    assert [ln for ln in _lines(_model([_rec(result=result)])) if ln[2] == "problem"] == []


@pytest.mark.parametrize("later", ["refused", "cancelled"])
def test_a_later_refused_or_cancelled_run_does_not_hide_a_failure(later):  # NEGATIVE
    h = [_rec(result="failed", why="boom", ts="2026-11-20T10:00:00"), _rec(result=later, ts="2026-11-21T10:00:00")]
    got = [ln for ln in _lines(_model(h)) if ln[2] == "problem"]
    assert len(got) == 1 and "boom" in got[0][3]


def test_failed_then_refused_then_ok_is_cleared():
    h = [_rec(result="failed", ts="2026-11-20T10:00:00"), _rec(result="refused", ts="2026-11-21T10:00:00"),
         _rec(result="ok", ts="2026-11-22T10:00:00")]
    assert [ln for ln in _lines(_model(h)) if ln[2] == "problem"] == []


def test_a_later_problem_still_replaces_an_earlier_one_past_a_refused_run():
    h = [_rec(result="failed", why="first", ts="2026-11-20T10:00:00"), _rec(result="refused", ts="2026-11-21T10:00:00"),
         _rec(result="stopped", why="second", ts="2026-11-22T10:00:00")]
    got = [ln for ln in _lines(_model(h)) if ln[2] == "problem"]
    assert len(got) == 1 and "second" in got[0][3] and "stopped" in got[0][3]


@pytest.mark.parametrize("only", ["refused", "cancelled"])
def test_a_lone_refused_or_cancelled_run_raises_no_line(only):  # NEGATIVE
    assert _lines(_model([_rec(result=only)])) == []


def test_recent_runs_still_lists_the_refused_run_after_a_failure():
    h = [_rec(result="failed", ts="2026-11-20T10:00:00"), _rec(result="refused", ts="2026-11-21T10:00:00")]
    assert [r["result"] for r in H.build_recent(h, NAMES)] == ["refused", "failed"]


def test_a_later_ok_run_clears_a_stopped_line():
    h = [_rec(result="stopped", ts="2026-11-20T10:00:00"), _rec(result="ok", ts="2026-11-21T10:00:00")]
    assert [ln for ln in _lines(_model(h)) if ln[2] == "problem"] == []


def test_an_ok_run_of_another_skill_does_not_clear_it():  # NEGATIVE
    h = [_rec(result="stopped", ts="2026-11-20T10:00:00"), _rec(skill="cc_skill", result="ok", ts="2026-11-21T10:00:00")]
    assert len([ln for ln in _lines(_model(h)) if ln[2] == "problem"]) == 1


def test_an_ok_run_for_another_year_does_not_clear_it():  # NEGATIVE
    h = [_rec(result="stopped", ts="2026-11-20T10:00:00"), _rec(year="2024-25", result="ok", ts="2026-11-21T10:00:00")]
    assert len([ln for ln in _lines(_model(h)) if ln[2] == "problem"]) == 1


def test_a_why_with_markup_is_escaped_in_the_html():  # NEGATIVE
    nasty = "<script>alert(1)</script> **bold** [x](http://e.test) <img src=x onerror=1>"
    out = H.render_needs_you(_model([_rec(result="failed", why=nasty)]))
    assert "<script>" not in out and "<img" not in out
    assert "&lt;script&gt;" in out and "**bold**" in out


def test_entity_and_skill_text_is_escaped_too():  # NEGATIVE
    out = H.render_needs_you(_model([_rec(skill="<b>x</b>", entity="<i>E</i>", result="failed")]))
    rec = H.render_recent(H.build_recent([_rec(skill="<b>x</b>", entity="<i>E</i>")], NAMES))
    for text in (out, rec):
        assert "<b>x" not in text and "<i>E" not in text


def test_an_unknown_skill_is_shown_as_stored():
    got = _lines(_model([_rec(skill="old_renamed_skill", result="failed")]))
    assert "old_renamed_skill" in got[0][3]


# ------------------------------------------------------------------ coverage lines ---

def test_months_with_no_statement_become_a_line_per_account():
    m = _model(coverage=[_cov_entry(gaps={"HSBC": ["2025-08", "2025-09"]})])
    got = _lines(m)
    assert got == [("Entity 1", "2025-26", "gap", "2 months with no HSBC statement (Aug, Sep)")]


def test_all_clear_only_for_the_latest_year_with_no_problem():
    m = _model(coverage=[_cov_entry(year="2025-26"), _cov_entry(year="2024-25")])
    assert [(y, k) for _, y, k, _ in _lines(m)] == [("2025-26", "clear")]


def test_a_problem_in_the_latest_year_suppresses_all_clear():  # NEGATIVE
    m = _model([_rec(result="stopped")], [_cov_entry()])
    assert "clear" not in {k for _, _, k, _ in _lines(m)}


def test_a_gap_in_the_latest_year_suppresses_all_clear():  # NEGATIVE
    m = _model(coverage=[_cov_entry(gaps={"HSBC": ["2025-08"]})])
    assert "clear" not in {k for _, _, k, _ in _lines(m)}


def test_all_clear_carries_the_check_date():
    out = H.render_needs_you(_model(coverage=[_cov_entry(checked="2026-11-18T10:00:00")]))
    assert "all clear" in out and "coverage checked 18 Nov" in out


def test_a_registered_book_never_checked_says_so():
    m = _model(registered={"Entity 2": ["2025-26"]})
    assert _lines(m) == [("Entity 2", "2025-26", "unchecked", "coverage not checked yet")]


def test_a_checked_book_is_not_called_unchecked():  # NEGATIVE
    m = _model(coverage=[_cov_entry(entity="Entity 2")], registered={"Entity 2": ["2025-26"]})
    assert "unchecked" not in {k for _, _, k, _ in _lines(m)}


def test_history_entity_joins_coverage_by_key_then_label():
    cov = [_cov_entry(entity="entity_one", label="Entity 1", gaps={"HSBC": ["2025-08"]})]
    m = _model([_rec(entity="Entity 1", result="failed")], cov)
    assert [e["entity"] for e in m] == ["entity_one"]


def test_entities_are_grouped_then_years_newest_first():
    m = _model([_rec(entity="Entity 2", year="2024-25", result="failed"),
                _rec(entity="Entity 2", year="2025-26", result="failed"),
                _rec(entity="Entity 1", year="2025-26", result="failed")])
    assert [e["entity"] for e in m] == ["Entity 1", "Entity 2"]
    assert [y["year"] for y in m[1]["years"]] == ["2025-26", "2024-25"]


def test_junk_input_never_raises():
    junk = [None, 5, "x", {"skill": None, "ts": 7, "result": 3}, {}, {"entity": ["a"], "year": {}}]
    H.build_needs_you(junk, {"k": 1, "j": {"accounts": 5, "entity": None}}, {"a": None, "b": [None]}, {"x": 1})
    H.build_needs_you(None, None, None, None)
    H.build_recent(junk, NAMES)
    H.build_recent(None, None)


# ------------------------------------------------------------------ recent runs ---

def test_recent_runs_are_the_last_20_newest_first_with_display_names():
    hist = [_rec(result="ok", ts=f"2026-11-{d:02d}T10:00:00") for d in range(1, 26)]
    rows = H.build_recent(hist, NAMES, 20)
    assert len(rows) == 20 and rows[0]["when"].startswith("25 Nov") and rows[-1]["when"].startswith("6 Nov")
    assert rows[0]["skill"] == "Bank -> GnuCash"


def test_recent_runs_show_every_result_including_refused_and_cancelled():
    rows = H.build_recent([_rec(result="refused"), _rec(result="cancelled")], NAMES)
    assert {r["result"] for r in rows} == {"refused", "cancelled"}
    assert "Bank ->" not in H.render_recent(rows).replace("Bank -&gt;", "")  # escaped


# ------------------------------------------------------------------ loading from the stores ---

def test_missing_empty_and_corrupt_stores_render_without_raising(root):
    ai, needs, recent = H.load_home(NAMES)
    assert "Nothing needs you" in needs and "No runs recorded yet" in recent
    (root / "logs").mkdir(parents=True)
    (root / "logs" / "run-history.jsonl").write_bytes(b"")
    (root / "logs" / "coverage-last.json").write_bytes(b"")
    H.load_home(NAMES)
    (root / "logs" / "run-history.jsonl").write_bytes(b"{not json\n\xff\xfe\x00\n[1,2]\n")
    (root / "logs" / "coverage-last.json").write_bytes(b"\x00\x01 {{{ ")
    ai, needs, recent = H.load_home(NAMES)
    assert isinstance(needs, str) and isinstance(recent, str)


def test_loading_reads_stores_and_writes_nothing(root):
    RH.append(_rec(result="failed", why="boom"))
    (root / "logs" / "coverage-last.json").write_text(
        json.dumps({"v": 1, "entries": {"Entity 1|2025-26": _cov_entry(gaps={"HSBC": ["2025-08"]})}}), encoding="utf-8")
    before = _tree(root)
    _, needs, recent = H.load_home(NAMES)
    assert "boom" in needs and "HSBC" in needs and "Bank -&gt; GnuCash" in recent
    assert _tree(root) == before


def test_loading_never_scans_a_book(root, monkeypatch):  # NEGATIVE
    from agents.skill_gnucash_coverage import agent as cov
    calls = []
    monkeypatch.setattr(cov, "scan_books", lambda *a, **k: calls.append(a))
    H.load_home(NAMES)
    assert calls == []


# ------------------------------------------------------------------ the AI line ---

def _ep():
    return {"provider": "ollama", "base_url": "http://x.invalid"}


def _res(status):
    return SimpleNamespace(ok=status == "ok", status=status)


def test_ai_line_names_the_active_endpoint_and_counts():
    cfg = {"active_endpoint": "local", "endpoints": {"local": _ep(), "lan": _ep(), "c1": _ep(), "c2": _ep()}}
    state = {id(cfg["endpoints"]["local"]): "ok", id(cfg["endpoints"]["lan"]): "ok"}
    line = H.ai_line(cfg, check=lambda ep: _res(state.get(id(ep), "unreachable")))
    assert line == "AI: local OK (2 of 4 up)"


def test_ai_line_shows_the_failing_state():
    cfg = {"active_endpoint": "local", "endpoints": {"local": _ep()}}
    assert H.ai_line(cfg, check=lambda ep: _res("unreachable")) == "AI: local DOWN (0 of 1 up)"
    assert H.ai_line({"endpoints": {}}) == "AI: no endpoint set up"


def test_ai_line_survives_a_probe_that_raises():
    cfg = {"active_endpoint": "a", "endpoints": {"a": _ep()}}

    def boom(ep):
        raise RuntimeError("x")
    assert "DOWN" in H.ai_line(cfg, check=boom)


def test_ai_placeholder_does_not_probe(monkeypatch):  # NEGATIVE
    monkeypatch.setattr(_health, "check", lambda ep: pytest.fail("probed"))
    monkeypatch.setattr(_health, "check_cached", lambda ep: pytest.fail("probed"))
    assert "AI:" in H.ai_placeholder()


# ------------------------------------------------------------------ Check coverage now ---

def test_check_now_scans_registered_books_once_and_only_writes_coverage_last(root, tmp_path):
    book = tmp_path / "books" / "e1.gnucash"
    book.parent.mkdir()
    book.write_bytes(b"synthetic-not-a-real-book")
    seen = []

    def fake_check(paths, entities_path=None):
        seen.append(list(paths))
        CL.path().parent.mkdir(parents=True, exist_ok=True)
        CL.path().write_text(json.dumps({"v": 1, "entries": {}}), encoding="utf-8")
        return SimpleNamespace(books_scanned=len(paths))

    before_book = book.read_bytes()
    msg = H.run_check_now(check=fake_check, paths_fn=lambda: {str(book)})
    assert seen == [[str(book)]] and "1 book" in msg
    assert book.read_bytes() == before_book
    assert set(_tree(root)) == {str(Path("logs") / "coverage-last.json")}


def test_check_now_with_no_registered_books_does_nothing(root):  # NEGATIVE
    called = []
    msg = H.run_check_now(check=lambda *a, **k: called.append(1), paths_fn=lambda: set())
    assert called == [] and "No books" in msg and _tree(root) == {}


def test_a_second_click_while_running_does_nothing():  # NEGATIVE
    started, release, runs = threading.Event(), threading.Event(), []

    def slow(paths, entities_path=None):
        runs.append(1)
        started.set()
        release.wait(5)
        return SimpleNamespace(books_scanned=1)

    t = threading.Thread(target=H.run_check_now, kwargs={"check": slow, "paths_fn": lambda: {"p"}})
    t.start()
    assert started.wait(5)
    try:
        assert "already running" in H.run_check_now(check=slow, paths_fn=lambda: {"p"})
    finally:
        release.set()
        t.join(5)
    assert runs == [1]


def test_a_failing_check_reports_and_releases_the_lock():
    def boom(paths, entities_path=None):
        raise RuntimeError("secret detail")
    msg = H.run_check_now(check=boom, paths_fn=lambda: {"p"})
    assert "stopped" in msg and "secret detail" not in msg
    assert "Checked" in H.run_check_now(check=lambda p, e=None: SimpleNamespace(books_scanned=1), paths_fn=lambda: {"p"})


# ------------------------------------------------------------------ registry helper ---

def test_registered_entity_years_reads_only_the_yaml(tmp_path):
    ep = tmp_path / "entities.yaml"
    assert _book_registry.registered_entity_years(ep) == {}
    ep.write_text("{{ not yaml", encoding="utf-8")
    assert _book_registry.registered_entity_years(ep) == {}


# ------------------------------------------------------------------ launch pad ---

def _sk(name, display, cat, hidden=False):
    return SimpleNamespace(name=name, display_name=display, category=cat, hidden=hidden)


def test_launch_pad_groups_in_tab_order_with_sub_tabs_and_developer_group():
    from ui import webui
    skills = [
        _sk("b2", "Zeta Bank", "banks"), _sk("b1", "Alpha Bank", "banks"),
        _sk("c1", "Credit Card - Book Spends", "credit_card"),
        _sk("i1", "ITR Workbook", "itr"), _sk("t1", "26AS Extract", "26as"),
        _sk("d1", "Dev One", "dev"), _sk("d2", "Dev Two", "dev"),
        _sk("u1", "Misc", "utilities"),
    ]
    groups = webui.launch_pad_groups(skills)
    assert [g for g, _ in groups] == ["Bank Skills", "Credit Card", "GnuCash", "Other", "Developer"]
    d = dict(groups)
    # app order (discovery order within a plain group), NOT A-Z: Zeta stays first
    assert [n for _, n in d["Bank Skills"]] == ["Zeta Bank", "Alpha Bank"]
    assert d["GnuCash"] == [("26AS", "26AS Extract"), ("ITR", "ITR Workbook")]
    assert [n for _, n in d["Developer"]] == ["Dev One", "Dev Two"]
    assert all(tab == "" for g in ("Bank Skills", "Credit Card", "Other", "Developer") for tab, _ in d[g])


def test_each_gnucash_sub_tab_is_named_once_on_its_first_line():
    from ui import webui
    skills = [_sk("k1", "Convert", "krc"), _sk("k2", "Reconcile", "krc"), _sk("k3", "Convert to GnuCash", "krc"),
              _sk("t1", "Convert", "26as"), _sk("t2", "Convert to GnuCash", "26as")]
    rows = dict(webui.launch_pad_groups(skills))["GnuCash"]
    assert rows == [("26AS", "Convert"), ("", "Convert to GnuCash"),
                    ("KRChoksey", "Convert"), ("", "Reconcile"), ("", "Convert to GnuCash")]


def test_launch_pad_rows_draw_a_sub_tab_column_only_where_there_is_one():
    from ui.tabs import home as tab_home
    a, b = tab_home.launch_pad_rows([("GnuCash", [("26AS", "Convert"), ("", "Other")]), ("Other", [("", "Misc")])])
    assert "26AS" in a[2] and a[1] == 2
    assert b[2].count("<span") == 1


def test_credit_card_and_gnucash_credit_card_read_differently():
    from ui import webui
    skills = [_sk("c1", "Credit Card", "credit_card"), _sk("c2", "Credit Card", "gnucash"),
              _sk("c3", "Other Card", "credit_card")]
    d = dict(webui.launch_pad_groups(skills))
    assert [n for _, n in d["Credit Card"]] == ["Credit Card", "Other Card"]
    assert d["GnuCash"] == [("Banks", "Credit Card")]
    paths = {s.name: p for s, p in webui.app_tab_order(skills)}
    assert paths["c1"] != paths["c2"]


def test_every_visible_real_skill_appears_once_in_the_launch_pad():
    from ui import webui
    skills_all = webui._discover_skills()
    visible = [s for s in skills_all if not s.hidden]
    flat = [n for _, items in webui.launch_pad_groups(visible) for _, n in items]
    assert sorted(flat) == sorted(s.display_name for s in visible)
    only_hidden = {s.display_name for s in skills_all if s.hidden} - {s.display_name for s in visible}
    assert not (only_hidden & set(flat))


# ------------------------------------------------------------------ labels for shared names ---

def test_two_skills_sharing_a_display_name_get_their_sub_tab_added():
    from ui import webui
    skills = [_sk("t1", "Convert", "26as"), _sk("k1", "Convert", "krc"), _sk("k2", "Reconcile", "krc")]
    labels = webui.skill_labels(skills)
    assert labels["t1"] == "Convert (26AS)" and labels["k1"] == "Convert (KRChoksey)"


def test_a_unique_display_name_gets_no_suffix():  # NEGATIVE
    from ui import webui
    skills = [_sk("t1", "Convert", "26as"), _sk("k1", "Convert", "krc"), _sk("k2", "Reconcile", "krc"),
              _sk("b1", "Solo Bank", "banks")]
    labels = webui.skill_labels(skills)
    assert labels["k2"] == "Reconcile" and labels["b1"] == "Solo Bank"


def test_labels_flow_into_the_two_panels_and_an_unknown_skill_falls_back():
    from ui import webui
    names = webui.skill_labels([_sk("t1", "Convert", "26as"), _sk("k1", "Convert", "krc")])
    h = [_rec(skill="t1", result="failed"), _rec(skill="gone_skill", result="failed", entity="Entity 2")]
    out = H.render_needs_you(H.build_needs_you(h, {}, {}, names))
    assert "Convert (26AS)" in out and "gone_skill" in out
    rows = H.build_recent(h, names)
    assert {r["skill"] for r in rows} == {"Convert (26AS)", "gone_skill"}


# ------------------------------------------------------------------ one source of order ---

def _tab_paths_in_app_order(app):
    """Every tab of a built app as its path of labels, depth first, in drawing order."""
    cfg = app.get_config_file()
    label = {c["id"]: (c.get("props") or {}).get("label") for c in cfg["components"] if c.get("type") == "tabitem"}
    out = []

    def walk(node, path):
        here = path
        if node["id"] in label:
            here = path + (label[node["id"]],)
            out.append(here)
        for child in node.get("children", []) or []:
            walk(child, here)
    walk(cfg["layout"], ())
    return out


def order_matches(tab_paths, expected):
    """True when every expected path is a tab of the app and they appear in this order."""
    pos = -1
    for path in expected:
        try:
            nxt = tab_paths.index(tuple(path), pos + 1)
        except ValueError:
            return False
        pos = nxt
    return True


def test_the_launch_pad_order_is_the_order_of_the_built_app():
    from ui import webui
    visible = [s for s in webui._discover_skills() if not s.hidden]
    expected = [p for _, p in webui.app_tab_order(visible)]
    assert expected and order_matches(_tab_paths_in_app_order(webui.build_app(launch=False)), expected)


def test_the_guard_catches_a_wrong_order():  # NEGATIVE
    from ui import webui
    visible = [s for s in webui._discover_skills() if not s.hidden]
    expected = [p for _, p in webui.app_tab_order(visible)]
    app_paths = _tab_paths_in_app_order(webui.build_app(launch=False))
    swapped = list(expected)
    swapped[3], swapped[4] = swapped[4], swapped[3]
    assert not order_matches(app_paths, swapped)
    assert not order_matches(app_paths, expected + [("No", "Such", "Tab")])


def test_app_tab_order_returns_every_visible_skill_once():
    from ui import webui
    visible = [s for s in webui._discover_skills() if not s.hidden]
    names = [s.name for s, _ in webui.app_tab_order(visible)]
    assert sorted(names) == sorted(s.name for s in visible)


# ------------------------------------------------------------------ the status row ---

def test_the_status_row_is_hidden_when_empty_and_shown_when_filled():
    from ui.tabs import home as tab_home
    assert tab_home.status_update("").visible is False
    filled = tab_home.status_update("Checked 2 book(s).")
    assert filled.visible is True and filled.value == "Checked 2 book(s)."


def test_the_built_home_starts_with_the_status_row_hidden():  # NEGATIVE
    from ui import webui
    from ui.tabs import home as tab_home
    app = webui.build_app(launch=False)
    sid = tab_home._deferred_home["status"]._id
    row = next(c for c in app.get_config_file()["components"] if c["id"] == sid)
    assert row["props"]["visible"] is False and row["props"]["value"] == ""


# ------------------------------------------------------------------ the built app ---

def test_building_the_app_reads_no_store_and_scans_no_book(root, monkeypatch):  # NEGATIVE
    from agents.skill_gnucash_coverage import agent as cov
    from ui import webui
    hits = []
    monkeypatch.setattr(cov, "scan_books", lambda *a, **k: hits.append("scan"))
    monkeypatch.setattr(RH, "tail", lambda *a, **k: hits.append("history") or [])
    monkeypatch.setattr(CL, "load", lambda *a, **k: hits.append("coverage") or {})
    monkeypatch.setattr(_book_registry, "registered_entity_years", lambda *a, **k: hits.append("registry") or {})
    monkeypatch.setattr(_book_registry, "registered_book_paths", lambda *a, **k: hits.append("paths") or set())
    webui.build_app(launch=False)
    assert hits == []
    assert _tree(root) == {}


def test_home_load_and_select_fill_without_scan_or_write(root, monkeypatch):  # NEGATIVE
    from agents.skill_gnucash_coverage import agent as cov
    from ui import webui
    from ui.tabs import home as tab_home
    hits = []
    monkeypatch.setattr(cov, "scan_books", lambda *a, **k: hits.append("scan"))
    webui.build_app(launch=False)
    fill = tab_home._deferred_home["fill"]
    fill()
    fill()
    assert hits == [] and _tree(root) == {}


def test_home_load_event_is_wired_once_and_resets():
    from ui.tabs import home as tab_home

    class App:
        def __init__(self):
            self.loads = []

        def load(self, fn=None, inputs=None, outputs=None):
            self.loads.append((fn, outputs))
    tab_home.reset_deferred_home_panels()
    app = App()
    tab_home.wire_deferred_home_loads(app)
    assert app.loads == []


def test_settings_now_holds_the_endpoint_status_and_home_does_not():
    from ui import webui
    from ui.tabs import home as tab_home, settings as tab_settings
    webui.build_app(launch=False)
    assert len(tab_settings._deferred_status_panels) == 1
    assert not hasattr(tab_home, "_deferred_status_panels")
    assert not hasattr(tab_home, "_build_skills_markdown")


def test_top_level_tabs_have_ids_for_the_buttons():
    from ui import webui
    from ui.tabs import home as tab_home
    app = webui.build_app(launch=False)
    ids = {c.get("props", {}).get("id") for c in app.get_config_file()["components"] if c.get("type") == "tabitem"}
    assert {tab_home.SETTINGS_TAB_ID, tab_home.HISTORY_TAB_ID} <= ids


def test_datetime_formatting_has_no_leading_zero_day():
    assert H._fmt_ts("2026-11-05T09:03:00", True) == "5 Nov 09:03"
    assert H._fmt_ts("garbage", False) == "garbage"
