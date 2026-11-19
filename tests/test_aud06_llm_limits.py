"""AUD-06: time limit, connection retries, reply cap and step limit on the LangChain path.
Synthetic fake servers and fake graphs only."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypedDict

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import base_agent as ba  # noqa: E402
from agents import llm_model_params as P  # noqa: E402


# ---- selection of the timeout ----------------------------------------------------

@pytest.mark.parametrize("url,model", [
    ("http://localhost:11434", "gemma4:12b"),
    ("http://127.0.0.1:11434", "llama3.2:latest"),
    ("http://192.168.1.50:1234/v1", "local-model"),
    ("http://10.0.0.7:11434", "gemma4:12b"),
    ("http://172.20.1.1:8000/v1", "x"),
])
def test_on_prem_gets_600(url, model):
    assert P.langchain_timeout_seconds(url, model) == 600 == P.LANGCHAIN_TIMEOUT_ONPREM_SECONDS


@pytest.mark.parametrize("url,model", [
    ("https://api.example.com/v1", "gpt-x"),
    ("https://8.8.8.8", "m"),
    ("https://ollama.com", "gemma4:12b"),
    ("", "m"),
    ("not a url", "m"),
])
def test_public_host_never_gets_600(url, model):                      # NEGATIVE
    assert P.langchain_timeout_seconds(url, model) == 120 == P.LANGCHAIN_TIMEOUT_HOSTED_SECONDS


@pytest.mark.parametrize("model", ["minimax-m3:cloud", "gpt-oss:120b-cloud", "Foo:CLOUD"])
@pytest.mark.parametrize("url", ["http://localhost:11434", "http://192.168.1.5:11434"])
def test_cloud_model_on_a_local_daemon_never_gets_600(url, model):   # NEGATIVE
    assert P.is_cloud_model(model)
    assert P.langchain_timeout_seconds(url, model) == 120


@pytest.mark.parametrize("model", ["gemma4:12b", "cloudy:latest", "mycloud:7b", "llama3.2:latest"])
def test_ordinary_models_are_not_cloud(model):
    assert not P.is_cloud_model(model)


def test_the_values_are_the_agreed_ones():
    assert (P.LANGCHAIN_TIMEOUT_ONPREM_SECONDS, P.LANGCHAIN_TIMEOUT_HOSTED_SECONDS) == (600, 120)
    assert (P.LANGCHAIN_MAX_RETRIES, P.LANGCHAIN_REPLY_MAX_TOKENS, P.AGENT_STEP_LIMIT) == (2, 4096, 15)


# ---- load_model passes all the limits on both providers ---------------------------------

def _cfg(tmp_path, provider, base_url, model):
    ep = {"base_url": base_url, "default_model": model, "temperature": 0.0}
    if provider == "openai_compatible":
        ep["api_key"] = "not-needed"
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"provider": provider, provider: ep}), encoding="utf-8")
    return str(p)


@pytest.mark.parametrize("url,model,secs", [
    ("http://localhost:11434", "gemma4:12b", 600),
    ("http://localhost:11434", "minimax-m3:cloud", 120),
    ("https://ollama.example.com", "gemma4:12b", 120),
])
def test_ollama_model_carries_all_three_limits(tmp_path, url, model, secs):
    m = ba.load_model(_cfg(tmp_path, "ollama", url, model))
    assert m.num_predict == 4096
    assert m.limit_seconds == secs
    assert m.client_kwargs["timeout"] == secs
    assert m.connect_retries == 2


@pytest.mark.parametrize("url,model,secs", [
    ("http://192.168.1.50:1234/v1", "local", 600),
    ("https://api.example.com/v1", "gpt-x", 120),
])
def test_openai_model_carries_all_three_limits(tmp_path, url, model, secs):
    m = ba.load_model(_cfg(tmp_path, "openai_compatible", url, model))
    assert m.max_tokens == 4096
    assert m.connect_retries == 2                    # our own retries, connection failures only
    assert m.max_retries == 0                        # the SDK would retry a timeout
    assert m.root_client.max_retries == 0
    assert m.request_timeout == secs


def test_no_langchain_model_is_built_without_the_limits(tmp_path):    # NEGATIVE
    from langchain_ollama import ChatOllama
    m = ba.load_model(_cfg(tmp_path, "ollama", "http://localhost:11434", "gemma4:12b"))
    assert type(m) is not ChatOllama           # the limited subclass, never the plain class
    assert m.num_predict is not None and m.limit_seconds > 0


# ---- a fake Ollama server ---------------------------------------------------------------

class _Fake:
    def __init__(self, mode):
        self.mode, self.hits, self.last_body = mode, 0, {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                outer.hits += 1
                n = int(self.headers.get("Content-Length") or 0)
                outer.last_body = json.loads(self.rfile.read(n) or b"{}")
                if outer.mode == "silent":
                    time.sleep(8)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()

                def line(content, done=False, reason=None):
                    d = {"model": "m", "created_at": "2026-01-01T00:00:00Z",
                         "message": {"role": "assistant", "content": content}, "done": done}
                    if done:
                        d["done_reason"] = reason or "stop"
                    self.wfile.write((json.dumps(d) + "\n").encode())
                    self.wfile.flush()
                if outer.mode == "trickle":
                    for _ in range(40):
                        line("x")
                        time.sleep(0.1)
                    line("", True)
                elif outer.mode == "length":
                    line("partial answer", True, "length")
                else:
                    line("fine", True)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fake():
    made = []

    def make(mode):
        f = _Fake(mode)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def _model(tmp_path, url, seconds, monkeypatch):
    monkeypatch.setattr(P, "LANGCHAIN_TIMEOUT_ONPREM_SECONDS", seconds)
    return ba.load_model(_cfg(tmp_path, "ollama", url, "gemma4:12b"))


def test_a_server_that_never_answers_stops_at_the_limit_and_is_not_retried(tmp_path, fake, monkeypatch):  # NEGATIVE
    f = fake("silent")
    m = _model(tmp_path, f.url, 1, monkeypatch)
    t0 = time.time()
    with pytest.raises(P.LlmLimitError) as ei:
        m.invoke("hello")
    assert time.time() - t0 < 6
    assert str(ei.value).startswith("The model did not answer within 1 s")
    assert f.hits == 1                                           # a timed-out call is not retried


def test_a_slow_trickle_is_capped_in_total(tmp_path, fake, monkeypatch):                                  # NEGATIVE
    f = fake("trickle")                                           # keeps sending for about 4 s
    m = _model(tmp_path, f.url, 1, monkeypatch)
    t0 = time.time()
    with pytest.raises(P.LlmLimitError):
        m.invoke("hello")
    assert time.time() - t0 < 3.5


def test_a_normal_answer_passes_and_asks_for_the_cap(tmp_path, fake, monkeypatch):
    f = fake("ok")
    m = _model(tmp_path, f.url, 30, monkeypatch)
    assert m.invoke("hello").content == "fine"
    assert f.last_body["options"]["num_predict"] == 4096


def test_connection_failures_are_retried_twice_then_raised(tmp_path, monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                     # nothing listens now
    m = _model(tmp_path, f"http://127.0.0.1:{port}", 30, monkeypatch)
    sleeps = []
    monkeypatch.setattr(ba, "_retry_pause", lambda x: sleeps.append(x))
    with pytest.raises((ConnectionError, httpx.ConnectError)):
        m.invoke("hello")
    assert len(sleeps) == 2


def test_a_timeout_is_not_a_connection_failure(tmp_path, fake, monkeypatch):                              # NEGATIVE
    f = fake("silent")
    m = _model(tmp_path, f.url, 1, monkeypatch)
    sleeps = []
    monkeypatch.setattr(ba, "_retry_pause", lambda x: sleeps.append(x))
    with pytest.raises(P.LlmLimitError):
        m.invoke("hello")
    assert sleeps == []


# ---- reply cap is reported ----------------------------------------------------------------

def test_run_direct_says_when_the_reply_hit_the_cap(tmp_path, fake):
    f = fake("length")
    out = ba.run_direct("hi", "be brief", _cfg(tmp_path, "ollama", f.url, "gemma4:12b"))
    assert out.startswith("partial answer")
    assert P.reply_cap_note() in out and "4,096 tokens" in out


def test_run_direct_does_not_add_the_note_to_a_complete_reply(tmp_path, fake):                            # NEGATIVE
    f = fake("ok")
    out = ba.run_direct("hi", "be brief", _cfg(tmp_path, "ollama", f.url, "gemma4:12b"))
    assert out == "fine"


# ---- step limit ---------------------------------------------------------------------------

def _looping_graph():
    from langgraph.graph import START, StateGraph

    class S(TypedDict):
        n: int
    calls = {"n": 0}

    def step(s):
        calls["n"] += 1
        return {"n": s["n"] + 1}

    g = StateGraph(S)
    g.add_node("a", step)
    g.add_edge(START, "a")
    g.add_edge("a", "a")
    return g.compile(), calls


def test_a_looping_agent_stops_at_the_step_limit_with_a_plain_message():                                  # NEGATIVE
    graph, calls = _looping_graph()
    agent = ba._LimitedAgent(graph)
    with pytest.raises(P.LlmLimitError) as ei:
        agent.invoke({"n": 0})
    assert str(ei.value).startswith("The run stopped after 15 steps without finishing")
    assert calls["n"] <= 16


def test_the_same_loop_without_the_wrapper_has_no_plain_message():                                        # NEGATIVE
    graph, _ = _looping_graph()
    from langgraph.errors import GraphRecursionError
    with pytest.raises(GraphRecursionError):
        graph.invoke({"n": 0}, {"recursion_limit": 15})


def test_build_agent_returns_the_limited_agent(tmp_path, fake):
    f = fake("ok")
    a = ba.build_agent([], "be brief", _cfg(tmp_path, "ollama", f.url, "gemma4:12b"))
    assert isinstance(a, ba._LimitedAgent)
    assert a._steps == 15


def test_build_agent_run_answers_through_the_wrapper(tmp_path, fake):
    f = fake("ok")
    a = ba.build_agent([], "be brief", _cfg(tmp_path, "ollama", f.url, "gemma4:12b"))
    res = a.invoke({"messages": [("user", "hi")]})
    assert res["messages"][-1].content == "fine"


def test_a_capped_agent_reply_is_flagged(tmp_path, fake):
    f = fake("length")
    a = ba.build_agent([], "be brief", _cfg(tmp_path, "ollama", f.url, "gemma4:12b"))
    res = a.invoke({"messages": [("user", "hi")]})
    assert P.reply_cap_note() in res["messages"][-1].content


def test_messages_name_the_limit_in_words():
    assert P.timeout_message(600).startswith("The model did not answer within 600 s")
    assert "15 steps" in P.step_limit_message()
    assert "4,096 tokens" in P.reply_cap_note()


def test_the_langchain_ollama_call_streams_so_the_total_cap_is_ours(tmp_path, fake, monkeypatch):
    """The client asks for a streamed reply, so the httpx timeout only limits the wait for the next
    piece; the total limit comes from LimitedChatOllama (see the trickle test above)."""
    f = fake("ok")
    _model(tmp_path, f.url, 30, monkeypatch).invoke("hello")
    assert f.last_body.get("stream") is True


def test_every_llm_skill_help_explains_the_limit_messages():
    import yaml as _y
    n = 0
    for p in sorted((ROOT / "src" / "agents").glob("skill_*/skill.yaml")):
        d = _y.safe_load(p.read_text(encoding="utf-8")) or {}
        if not d.get("display_name"):
            continue
        rows = (d.get("help") or {}).get("troubleshooting") or []
        has = [r for r in rows if "time limit or a step limit" in r.get("problem", "")]
        llm = (d.get("requires") or {}).get("llm", True) is True
        assert bool(has) == llm, p.parent.name        # present on LLM skills only
        n += llm
    assert n == 8


# ---- the OpenAI-compatible path: a timeout is never retried ------------------------------------

def _oa_model(tmp_path, url, seconds, monkeypatch):
    monkeypatch.setattr(P, "LANGCHAIN_TIMEOUT_ONPREM_SECONDS", seconds)
    return ba.load_model(_cfg(tmp_path, "openai_compatible", url + "/v1", "local-model"))


class _OAFake:
    def __init__(self, mode):
        self.mode, self.hits = mode, 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                outer.hits += 1
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                if outer.mode == "silent":
                    time.sleep(8)
                    return
                body = json.dumps({"id": "x", "object": "chat.completion", "created": 1, "model": "m",
                                   "choices": [{"index": 0, "finish_reason": "length",
                                                "message": {"role": "assistant", "content": "partial"}}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def oafake():
    made = []

    def make(mode):
        f = _OAFake(mode)
        made.append(f)
        return f
    yield make
    for f in made:
        f.close()


def test_openai_server_that_never_answers_gets_exactly_one_request(tmp_path, oafake, monkeypatch):        # NEGATIVE
    f = oafake("silent")
    m = _oa_model(tmp_path, f.url, 1, monkeypatch)
    sleeps = []
    monkeypatch.setattr(ba, "_retry_pause", lambda x: sleeps.append(x))
    t0 = time.time()
    with pytest.raises(P.LlmLimitError) as ei:
        m.invoke("hello")
    assert time.time() - t0 < 6
    assert str(ei.value).startswith("The model did not answer within 1 s")
    assert f.hits == 1
    assert sleeps == []


def test_openai_refused_connection_is_retried_twice_then_raised(tmp_path, monkeypatch):
    import openai
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    m = _oa_model(tmp_path, f"http://127.0.0.1:{port}", 30, monkeypatch)
    sleeps = []
    monkeypatch.setattr(ba, "_retry_pause", lambda x: sleeps.append(x))
    with pytest.raises(openai.APIConnectionError):
        m.invoke("hello")
    assert sleeps == [1, 1]


def test_openai_capped_reply_is_flagged_by_run_direct(tmp_path, oafake):
    f = oafake("ok")
    out = ba.run_direct("hi", "be brief", _cfg(tmp_path, "openai_compatible", f.url + "/v1", "local-model"))
    assert out.startswith("partial") and P.reply_cap_note() in out
