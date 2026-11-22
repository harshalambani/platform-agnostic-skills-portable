"""PERF-04 A: cheap start-up wins fall back to stock behaviour when unsure."""
from __future__ import annotations

import json
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import gradio as gr  # noqa: E402
import gradio.blocks as gblocks  # noqa: E402
import gradio.routes as groutes  # noqa: E402

from ui import _startup_fast as sf  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _tiny_app():
    with gr.Blocks() as app:
        with gr.Tab("Alpha"):
            gr.Markdown("a")
        with gr.Tab("Beta"):
            btn = gr.Button("go")
            out = gr.Textbox()
        app.load(fn=lambda: "loaded", inputs=None, outputs=out)
    return app


def test_unknown_gradio_version_uses_the_stock_path(monkeypatch):  # NEGATIVE
    class Fake:
        __version__ = "9.9.9"
    before = (gblocks.Blocks.__exit__, gblocks.Blocks.get_config_file, groutes.App.create_app)
    with sf.skip_exit_build(Fake) as active:
        assert active is False
        assert (gblocks.Blocks.__exit__, gblocks.Blocks.get_config_file) == before[:2]
    # and a disabled flag (tests, launch=False) is stock too
    with sf.skip_exit_build(gr, enabled=False) as active:
        assert active is False
        assert gblocks.Blocks.__exit__ is before[0]


def test_skip_is_scoped_and_restored(monkeypatch):
    if not sf.gradio_is_tested(gr):
        pytest.skip("gradio version not in the tested set")
    before = (gblocks.Blocks.__exit__, gblocks.Blocks.get_config_file)
    with sf.skip_exit_build(gr) as active:
        assert active is True
        assert gblocks.Blocks.__exit__ is not before[0]
    assert (gblocks.Blocks.__exit__, gblocks.Blocks.get_config_file) == before
    # restored even when the body raises
    with pytest.raises(RuntimeError):
        with sf.skip_exit_build(gr):
            raise RuntimeError("x")
    assert (gblocks.Blocks.__exit__, gblocks.Blocks.get_config_file) == before


def test_exit_build_is_skipped_but_the_launched_app_is_complete():
    if not sf.gradio_is_tested(gr):
        pytest.skip("gradio version not in the tested set")
    calls = []
    orig = groutes.App.create_app

    def counting(*a, **k):
        calls.append(k.get("mcp_server"))
        return orig(*a, **k)
    groutes.App.create_app = staticmethod(counting)
    try:
        _tiny_app().close()                 # stock: Blocks() builds once, __exit__ once more
        stock = len(calls)
        calls.clear()
        with sf.skip_exit_build(gr):
            app = _tiny_app()
        assert len(calls) == stock - 1      # the build in __exit__ did not run
        port = _free_port()
        app.launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True,
                   quiet=True, _frontend=False)
        try:
            cfg = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/config", timeout=20).read())
            labels = {c["props"].get("label") for c in cfg["components"] if c["type"] == "tabitem"}
            assert {"Alpha", "Beta"} <= labels                       # every tab is still there
            # the load event (deferred loads) is still registered and wired
            assert any(any(t[1] == "load" for t in d["targets"]) for d in cfg["dependencies"])
            assert urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=20).status == 200
        finally:
            app.close()
    finally:
        groutes.App.create_app = staticmethod(orig)


def test_unlaunched_build_keeps_the_stock_config():  # NEGATIVE
    app = _tiny_app()                     # stock path: __exit__ built the config
    assert app.config and app.app is not None
    app.close()


def test_launch_extra_is_empty_unless_window_mode_on_a_tested_version(monkeypatch):  # NEGATIVE
    from ui import webui
    monkeypatch.setattr(webui._startup_fast, "gradio_is_tested", lambda g: True)
    assert webui._launch_extra(True) == {"_frontend": False}
    assert webui._launch_extra(False) == {}          # browser mode needs the frontend probe
    monkeypatch.setattr(webui._startup_fast, "gradio_is_tested", lambda g: False)
    assert webui._launch_extra(True) == {}


def test_readiness_probe_is_not_ready_before_the_server_answers():  # NEGATIVE
    from ui import webui
    port = _free_port()
    assert webui._wait_for_server(f"http://127.0.0.1:{port}/", timeout=0.6) is False
    app = _tiny_app()
    try:
        app.launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True, quiet=True)
        assert webui._wait_for_server(f"http://127.0.0.1:{port}/", timeout=20) is True
    finally:
        app.close()


def test_ssl_cache_only_serves_the_default_verified_context():  # NEGATIVE
    import httpx
    import httpx._config as cfg
    sf.start_ssl_warm()
    if httpx.__version__ not in sf.TESTED_HTTPX:
        assert sf.install_ssl_cache() is False
        return
    assert sf.install_ssl_cache() is True
    import httpx._transports.default as dflt
    ctx = dflt.create_ssl_context(verify=True)
    assert ctx.verify_mode.name == "CERT_REQUIRED"
    unverified = dflt.create_ssl_context(verify=False)       # not the cached one
    assert unverified is not ctx and unverified.verify_mode.name == "CERT_NONE"
    assert httpx.Client() is not None
