"""AUD-11: only the app's own page can call its local server.

The app listens on 127.0.0.1 with no login (accepted as designed). A web page
open in the same browser, from another site, must not be able to read anything
the app returns. The browser enforces that through the CORS answer headers, so
these tests start the app the way ui/webui.py does (same networking arguments,
synthetic config, a free port) and check what a foreign Origin gets back:

* a cross-origin "simple" POST is not readable by the foreign page;
* a CORS preflight is not granted to the foreign page;
* the app's own origin, and a call with no Origin, do work, so the checks above
  are not blind.
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

gr = pytest.importorskip("gradio")
httpx = pytest.importorskip("httpx")

FOREIGN = "http://evil.example"
ACAO = "access-control-allow-origin"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("aud11")
    cfg = tmp / "config.yaml"
    cfg.write_text("endpoints: {}\n", encoding="utf-8")
    mp = pytest.MonkeyPatch()
    mp.setenv("PA_SKILLS_CONFIG", str(cfg))
    from ui import webui

    app = webui.build_app(launch=False)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # The networking arguments of the launch call in ui/webui.py.
    app.launch(server_name="127.0.0.1", server_port=port, share=False,
               inbrowser=False, prevent_thread_lock=True, quiet=True,
               allowed_paths=[])
    base = f"http://127.0.0.1:{port}"
    try:
        yield base
    finally:
        try:
            app.close()
        finally:
            mp.undo()


def _readable_by(origin: str, resp) -> bool:
    """True when a browser would let a page from `origin` read this response."""
    allow = resp.headers.get(ACAO)
    return allow == "*" or allow == origin


def _call(method, url, origin=None, **kw):
    headers = dict(kw.pop("headers", {}))
    if origin:
        headers["Origin"] = origin
    return httpx.request(method, url, headers=headers, timeout=30, **kw)


# ---- the foreign page --------------------------------------------------------

def test_a_foreign_origin_cannot_read_a_simple_post(server):
    # a "simple" request: text/plain, no preflight. It may reach the server, but
    # its answer must not be readable by the foreign page.
    body = json.dumps({"data": [], "fn_index": 0, "session_hash": "x"})
    r = _call("POST", server + "/gradio_api/queue/join", FOREIGN, content=body,
              headers={"Content-Type": "text/plain"})
    assert r.status_code >= 400          # text/plain is not accepted as the JSON call
    assert not _readable_by(FOREIGN, r)
    assert ACAO not in r.headers


def test_a_foreign_origin_cannot_read_a_form_upload_either(server):
    # multipart is also a simple content type; the upload route answers it, but
    # the answer (a file path) is not readable by the foreign page.
    r = _call("POST", server + "/gradio_api/upload", FOREIGN,
              files={"files": ("a.txt", b"synthetic")})
    assert not _readable_by(FOREIGN, r)
    assert ACAO not in r.headers


def test_a_foreign_origin_gets_no_grant_on_a_preflight(server):
    r = _call("OPTIONS", server + "/gradio_api/queue/join", FOREIGN, headers={
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert not _readable_by(FOREIGN, r)
    assert ACAO not in r.headers         # without it the browser drops the real call


def test_a_foreign_origin_cannot_read_the_page_config(server):
    r = _call("GET", server + "/config", FOREIGN)
    assert not _readable_by(FOREIGN, r)


def test_the_null_origin_is_not_granted_either(server):
    r = _call("GET", server + "/config", "null")
    assert not _readable_by("null", r)
    r = _call("OPTIONS", server + "/gradio_api/queue/join", "null", headers={
        "Access-Control-Request-Method": "POST"})
    assert ACAO not in r.headers


# ---- positive controls: the checks above are not blind ----------------------

def test_the_apps_own_origin_is_granted(server):
    r = _call("GET", server + "/config", server)
    assert r.status_code == 200 and r.json().get("mode") == "blocks"
    assert _readable_by(server, r)
    pre = _call("OPTIONS", server + "/gradio_api/queue/join", server, headers={
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 200 and _readable_by(server, pre)
    up = _call("POST", server + "/gradio_api/upload", server,
               files={"files": ("a.txt", b"synthetic")})
    assert up.status_code == 200 and _readable_by(server, up)


def test_a_call_with_no_origin_works(server):
    r = _call("GET", server + "/config")
    assert r.status_code == 200 and r.json().get("mode") == "blocks"


def test_the_readability_check_itself_tells_the_two_apart(server):
    """The same URL, two origins: only one is readable."""
    own = _call("GET", server + "/config", server)
    foreign = _call("GET", server + "/config", FOREIGN)
    assert _readable_by(server, own) is True
    assert _readable_by(FOREIGN, foreign) is False
