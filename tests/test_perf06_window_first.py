"""PERF-06 -- open the window first, then load the app into it.

Ordering and mocks, not timing. A fake `webview` module stands in for the real
window library; no window is opened. Synthetic only.
"""
from __future__ import annotations

import ast
import sys
import threading
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
for _p in (ROOT.parent / "src", ROOT.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ui import _firstwindow as fw  # noqa: E402

WEBUI = ROOT.parent / "ui" / "webui.py"


class FakeEvents:
    def __init__(self):
        self.handlers = {"closing": [], "shown": []}

    def __getattr__(self, name):
        if name == "handlers":
            raise AttributeError(name)
        return _Hook(self.handlers[name])


class _Hook:
    def __init__(self, lst):
        self.lst = lst

    def __iadd__(self, fn):
        self.lst.append(fn)
        return self


class FakeWindow:
    def __init__(self, log, html=None):
        self.log = log
        self.events = FakeEvents()
        self.pages = [("html", html)] if html else []

    def load_url(self, url):
        self.log.append(("load_url", url))
        self.pages.append(("url", url))

    def load_html(self, h):
        self.log.append(("load_html", h))
        self.pages.append(("html", h))


@pytest.fixture()
def fake_webview(monkeypatch):
    log = []
    mod = types.ModuleType("webview")
    mod.settings = {}
    state = {"window": None, "fire_shown": True}

    def create_window(title, url=None, html=None, **kw):
        w = FakeWindow(log, html=html)
        state["window"] = w
        log.append(("create_window", title, bool(html), url, "js_api" in kw))
        return w

    def start(*a, **kw):
        log.append(("start",))
        if state["fire_shown"]:
            for h in list(state["window"].events.handlers["shown"]):
                h()                    # the GUI is up and the window is shown

    mod.create_window = create_window
    mod.start = start
    monkeypatch.setitem(sys.modules, "webview", mod)
    monkeypatch.setattr(fw, "native_window_available", lambda: True)
    # a launch is only taken over when nothing in the environment says otherwise
    for k in ("PA_SKILLS_NO_BROWSER", "PA_SKILLS_NO_WINDOW"):
        monkeypatch.delenv(k, raising=False)
    # the real exit must never run in a test
    exits = []
    monkeypatch.setattr(fw, "terminate_process", lambda: exits.append(1))
    return types.SimpleNamespace(log=log, state=state, exits=exits)


def _run(argv):
    """fw.run, then wait for the loader thread it started."""
    r = fw.run(argv)
    th = fw._LOADER_THREAD
    if th is not None:
        th.join(10)
        assert not th.is_alive()
    return r


# -- the window opens before gradio is imported (ordering, not timing) --------------------

def test_the_launch_check_runs_before_gradio_is_imported_in_webui():
    tree = ast.parse(WEBUI.read_text(encoding="utf-8"))
    first_gradio = None
    run_call = None
    for node in tree.body:
        if isinstance(node, ast.Import) and any(a.name == "gradio" for a in node.names):
            first_gradio = first_gradio or node.lineno
        if isinstance(node, ast.If) and "__main__" in ast.dump(node.test):
            if "run" in ast.dump(node) and "_first_window" in ast.dump(node):
                run_call = run_call or node.lineno
    assert first_gradio and run_call
    assert run_call < first_gradio


def test_the_window_is_created_and_started_before_the_app_module_is_imported(fake_webview, monkeypatch):
    order = fake_webview.log

    def fake_import(name, *a, **k):
        order.append(("import", name))
        raise RuntimeError("stop here")
    monkeypatch.setattr("importlib.import_module", fake_import)
    _run([])
    kinds = [e[0] if e[0] != "import" else f"import:{e[1]}" for e in order]
    # the window exists before the app module is imported; the loader starts at once,
    # in parallel with the window coming up, not after it
    assert kinds.index("create_window") < kinds.index("import:ui.webui")
    # and the page it opened is the local loading page, not a URL
    cw = next(e for e in order if e[0] == "create_window")
    assert cw[2] is True and cw[3] is None


def test_the_loading_page_is_plain_text_and_says_starting():
    h = fw.loading_html()
    assert "Starting PA Skills" in h and "<script" not in h.lower()


# -- the switch happens only after the server answers ------------------------------------

def test_the_window_switches_only_after_the_server_answers():
    log = []
    w = FakeWindow(log, html="x")
    calls = []

    def waiter(url, timeout):
        calls.append(("wait", url))
        assert not any(e[0] == "load_url" for e in log)      # not switched while waiting
        return True
    assert fw.attach_app(w, "http://127.0.0.1:1", waiter) is True
    assert calls == [("wait", "http://127.0.0.1:1")]
    assert log == [("load_url", "http://127.0.0.1:1")]


def test_no_switch_while_the_server_is_down():                        # NEGATIVE
    log = []
    w = FakeWindow(log, html="x")
    assert fw.attach_app(w, "http://127.0.0.1:1", lambda u, t: False) is False
    assert not any(e[0] == "load_url" for e in log)
    assert [e[0] for e in log] == ["load_html"]               # an error page, not a hang


def test_a_waiter_that_raises_is_shown_as_an_error_not_a_switch():     # NEGATIVE
    log = []
    w = FakeWindow(log, html="x")

    def boom(u, t):
        raise OSError("synthetic")
    assert fw.attach_app(w, "http://127.0.0.1:1", boom) is False
    assert not any(e[0] == "load_url" for e in log)


# -- a start failure is shown, never left hanging ------------------------------------------

def test_a_start_failure_is_shown_in_the_window(fake_webview, monkeypatch):    # NEGATIVE
    def broken(name, *a, **k):
        raise ImportError("synthetic import failure")
    monkeypatch.setattr("importlib.import_module", broken)
    _run([])
    w = fake_webview.state["window"]
    shown = [p for k, p in w.pages if k == "html"][-1]
    assert "could not start" in shown and "synthetic import failure" in shown
    assert "Starting PA Skills" not in shown                    # not left on "Starting..."
    assert "pywebview.api.close()" in shown                     # a way to close it


def test_error_text_is_escaped():                                   # NEGATIVE
    h = fw.error_html("<b>x</b>", "<script>alert(1)</script>")
    assert "<script>alert" not in h and "&lt;script&gt;" in h


def test_the_loader_hands_the_window_to_the_app_module(fake_webview, monkeypatch):
    seen = {}
    fake_mod = types.SimpleNamespace(_PREOPENED_WINDOW=None,
                                     main=lambda argv: seen.setdefault("argv", argv))
    monkeypatch.setattr("importlib.import_module", lambda n: fake_mod)
    _run(["x"])
    assert fake_mod._PREOPENED_WINDOW._window is fake_webview.state["window"]
    assert seen["argv"] == ["x"]


# -- closing ---------------------------------------------------------------------------------

def test_closing_the_window_runs_the_exit_sequence(fake_webview, monkeypatch):
    monkeypatch.setattr("importlib.import_module", lambda n: types.SimpleNamespace(main=lambda a: None))
    _run([])
    w = fake_webview.state["window"]
    handler = w.events.handlers["closing"][0]
    before = len(fake_webview.exits)
    handler()
    assert len(fake_webview.exits) == before + 1


def test_terminate_runs_exit_cleanups_then_exits(monkeypatch):
    calls = []
    cfg = types.SimpleNamespace(run_exit_cleanups=lambda: calls.append("cleanups"))
    monkeypatch.setitem(sys.modules, "ui._config", cfg)
    monkeypatch.setattr(fw.os, "_exit", lambda code: calls.append(("exit", code)))
    fw.terminate_process()
    assert calls == ["cleanups", ("exit", 0)]


def test_terminate_still_exits_when_the_cleanups_fail(monkeypatch):      # NEGATIVE
    calls = []

    def bad():
        raise RuntimeError("synthetic")
    monkeypatch.setitem(sys.modules, "ui._config", types.SimpleNamespace(run_exit_cleanups=bad))
    monkeypatch.setattr(fw.os, "_exit", lambda code: calls.append(("exit", code)))
    fw.terminate_process()
    assert calls == [("exit", 0)]


def test_terminate_before_the_config_is_loaded_does_not_import_it(monkeypatch):   # NEGATIVE
    monkeypatch.delitem(sys.modules, "ui._config", raising=False)
    monkeypatch.setattr(fw.os, "_exit", lambda code: None)
    fw.terminate_process()
    assert "ui._config" not in sys.modules


def test_closing_during_starting_leaves_no_thread_behind(fake_webview, monkeypatch):   # NEGATIVE
    started = threading.Event()
    release = threading.Event()

    def slow_import(name):
        started.set()
        release.wait(5)
        return types.SimpleNamespace(main=lambda a: None)
    monkeypatch.setattr("importlib.import_module", slow_import)
    before = {t.ident for t in threading.enumerate()}
    fake_webview.state["fire_shown"] = False
    th = threading.Thread(target=fw.run, args=([],))
    th.start()
    assert started.wait(5)
    w = fake_webview.state["window"]
    w.events.handlers["closing"][0]()                 # the user closes during "Starting..."
    assert fake_webview.exits                         # the exit sequence was reached
    release.set()
    th.join(5)
    fw._LOADER_THREAD.join(5)
    assert not th.is_alive() and not fw._LOADER_THREAD.is_alive()
    assert {t.ident for t in threading.enumerate()} <= before | {threading.get_ident()}


# -- the splash -----------------------------------------------------------------------------------

def test_the_splash_is_dismissed_when_the_window_is_shown(fake_webview, monkeypatch):
    from ui import _splash
    hits = []
    monkeypatch.setattr(_splash, "dismiss_launcher_splash", lambda: hits.append(1))
    monkeypatch.setattr("importlib.import_module", lambda n: types.SimpleNamespace(main=lambda a: None))
    fake_webview.state["fire_shown"] = False
    _run([])
    w = fake_webview.state["window"]
    assert hits == []                                  # not before it is shown
    for h in w.events.handlers["shown"]:
        h()
    assert hits == [1]


# -- when this is NOT a window launch, nothing changes -------------------------------------------

@pytest.mark.parametrize("argv,env", [
    (["--no-browser"], {}),
    (["--no-window"], {}),
    ([], {"PA_SKILLS_NO_BROWSER": "1"}),
    ([], {"PA_SKILLS_NO_WINDOW": "1"}),
    (["C:/x/script.py", "a"], {}),                       # frozen-mode script dispatch
])
def test_other_launches_are_left_to_the_old_path(argv, env):      # NEGATIVE
    assert fw.wants_window_first(argv, env=env, available=lambda: True) is False


def test_no_native_window_means_the_old_path(fake_webview, monkeypatch):    # NEGATIVE
    monkeypatch.setattr(fw, "native_window_available", lambda: False)
    assert fw.run([]) is False
    assert not any(e[0] == "create_window" for e in fake_webview.log)


def test_a_window_that_never_opens_falls_back_to_the_old_path(fake_webview, monkeypatch):   # NEGATIVE
    def boom(*a, **k):
        raise RuntimeError("no window")
    sys.modules["webview"].create_window = boom
    assert fw.run([]) is False
    assert not fake_webview.exits


def test_webui_attaches_to_the_preopened_window_instead_of_opening_a_second():
    src = WEBUI.read_text(encoding="utf-8")
    assert "_firstwindow.attach_app(_PREOPENED_WINDOW, url, _wait_for_server)" in src
    # the old path is still there for every other launch
    assert "_run_native_window(url)" in src


# -- the loader starts at once; a page change waits for the window to be shown -----------------

def test_a_page_change_waits_until_the_window_is_shown():
    log = []
    shown = threading.Event()
    pw = fw.PreopenedWindow(FakeWindow(log, html="x"), shown, wait_s=5)
    done = threading.Event()

    def go():
        pw.load_url("http://127.0.0.1:1")
        done.set()
    th = threading.Thread(target=go)
    th.start()
    assert not done.wait(0.3) and log == []           # held back while not shown
    shown.set()
    assert done.wait(5)
    th.join(5)
    assert log == [("load_url", "http://127.0.0.1:1")]


def test_a_window_that_is_never_shown_does_not_hang_the_loader():     # NEGATIVE
    log = []
    pw = fw.PreopenedWindow(FakeWindow(log, html="x"), threading.Event(), wait_s=0.2)
    pw.load_html("<p>error</p>")                       # returns after the bounded wait
    assert [e[0] for e in log] == ["load_html"]


def test_the_loader_is_a_daemon_so_it_cannot_keep_the_process_alive(fake_webview, monkeypatch):   # NEGATIVE
    monkeypatch.setattr("importlib.import_module", lambda n: types.SimpleNamespace(main=lambda a: None))
    _run([])
    assert fw._LOADER_THREAD.daemon is True
