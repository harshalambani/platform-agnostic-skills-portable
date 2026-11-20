"""AUD-07 P4: the agent skills and the direct runner send through the single client.

A local fake AI service (both providers), or urlopen replaced. Synthetic data only.
"""
from __future__ import annotations

import io
import json
import re
import socket
import subprocess
import sys
import urllib.error as ue
from pathlib import Path
from urllib import request

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from langchain_core.tools import tool  # noqa: E402

from agents import base_agent as ba  # noqa: E402
from agents import llm_chat_model, llm_client, llm_model_params as P, llm_pacing  # noqa: E402
from tests.test_aud07_llm_client import _Fake  # noqa: E402

KEY = "sk-synthetic-AGENTKEY123456"
HTTPS = "https://llm.example.invalid/v1"
PROVIDERS = ["ollama", "openai_compatible"]


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    llm_pacing.reset_registry()
    monkeypatch.setattr(llm_pacing, "default_state_path", lambda: tmp_path / "pace.json")
    f = _Fake()
    yield f
    f.stop()
    llm_pacing.reset_registry()


def _cfg(tmp_path, provider, url, model="synthetic-model", **extra):
    blk = {"base_url": url, "temperature": 0.0, **extra}
    if model:
        blk["default_model"] = model
    if provider == "openai_compatible":
        blk.setdefault("api_key", "not-needed")
    p = tmp_path / f"{provider}.yaml"
    p.write_text(yaml.safe_dump({"provider": provider, provider: blk}), encoding="utf-8")
    return str(p)


def _url(fake, provider):
    return fake.url if provider == "ollama" else fake.url + "/v1"


def _plain(provider, text="hello", reason="stop"):
    if provider == "ollama":
        return {"model": "m", "message": {"role": "assistant", "content": text}, "done": True, "done_reason": reason}
    return {"choices": [{"index": 0, "finish_reason": reason, "message": {"role": "assistant", "content": text}}]}


def _calls(provider, calls, text=""):
    """calls: [(id, name, args dict)]"""
    if provider == "ollama":
        tc = [{"function": {"name": n, "arguments": a}} for _i, n, a in calls]
        return {"model": "m", "message": {"role": "assistant", "content": text, "tool_calls": tc},
                "done": True, "done_reason": "stop"}
    tc = [{"id": i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, n, a in calls]
    return {"choices": [{"index": 0, "finish_reason": "tool_calls",
                         "message": {"role": "assistant", "content": text, "tool_calls": tc}}]}


def _ok(body):
    return (200, body, {}, 0)


@tool
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


@tool
def shout(text: str) -> str:
    """Upper-case the text."""
    return text.upper()


# ------------------------------------------------------------ a plain reply

@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_plain_reply_goes_through_the_client(fake, tmp_path, provider):
    fake.plan = [_ok(_plain(provider, "fine"))]
    out = ba.run_direct("hi", "be brief", _cfg(tmp_path, provider, _url(fake, provider)))
    assert out == "fine"
    req = fake.requests[0]
    assert req["path"] == ("/api/chat" if provider == "ollama" else "/v1/chat/completions")
    roles = [m["role"] for m in req["body"]["messages"]]
    assert roles == ["system", "user"]
    assert "tools" not in req["body"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_the_reply_cap_and_stream_flag_are_sent(fake, tmp_path, provider):
    fake.plan = [_ok(_plain(provider))]
    ba.load_model(_cfg(tmp_path, provider, _url(fake, provider))).invoke("hi")
    body = fake.requests[0]["body"]
    assert body["stream"] is False
    cap = body["options"]["num_predict"] if provider == "ollama" else body["max_tokens"]
    assert cap == P.LANGCHAIN_REPLY_MAX_TOKENS == 4096


@pytest.mark.parametrize("provider,url,model,secs", [
    ("ollama", "http://localhost:11434", "gemma4:12b", 600),
    ("ollama", "http://localhost:11434", "x:cloud", 120),
    ("openai_compatible", HTTPS, "gpt-x", 120),
    ("openai_compatible", "http://192.168.1.50:1234/v1", "local", 600),
])
def test_the_timeout_is_actually_applied_to_the_request(tmp_path, monkeypatch, provider, url, model, secs):
    seen = []

    def fake_open(req, timeout=None):
        seen.append(timeout)

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read1(self, n): return b"" if getattr(self, "done", False) else self._once()
            def _once(self):
                self.done = True
                return json.dumps(_plain(provider)).encode()
        return R()
    monkeypatch.setattr(request, "urlopen", fake_open)
    ba.load_model(_cfg(tmp_path, provider, url, model)).invoke("hi")
    assert seen == [secs]


# ------------------------------------------------------------ tool calls

@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_tool_call_then_tool_result_then_final_answer(fake, tmp_path, provider):
    fake.plan = [_ok(_calls(provider, [("call_a1", "echo", {"text": "x"})])), _ok(_plain(provider, "all done"))]
    agent = ba.build_agent([echo], "SYS PROMPT", _cfg(tmp_path, provider, _url(fake, provider)))
    res = agent.invoke({"messages": [("user", "go")]})
    assert res["messages"][-1].content == "all done"
    first, second = (r["body"] for r in fake.requests)
    assert first["messages"][0] == {"role": "system", "content": "SYS PROMPT"}
    sent_tools = first["tools"]
    assert sent_tools[0]["type"] == "function" and sent_tools[0]["function"]["name"] == "echo"
    assert sent_tools[0]["function"]["parameters"]["properties"]["text"]["type"] == "string"
    asst = [m for m in second["messages"] if m["role"] == "assistant"][0]
    tool_msg = [m for m in second["messages"] if m["role"] == "tool"][0]
    assert tool_msg["content"] == "echo:x"
    if provider == "ollama":
        assert asst["tool_calls"][0]["function"] == {"name": "echo", "arguments": {"text": "x"}}
        assert tool_msg["tool_name"] == "echo"
    else:
        fn = asst["tool_calls"][0]
        assert fn["id"] == "call_a1" and json.loads(fn["function"]["arguments"]) == {"text": "x"}
        assert tool_msg["tool_call_id"] == "call_a1"


@pytest.mark.parametrize("provider", PROVIDERS)
def test_two_tool_calls_in_one_reply_both_run_and_both_results_go_back(fake, tmp_path, provider):
    fake.plan = [_ok(_calls(provider, [("call_1", "echo", {"text": "a"}), ("call_2", "shout", {"text": "b"})])),
                 _ok(_plain(provider, "both done"))]
    agent = ba.build_agent([echo, shout], "SYS", _cfg(tmp_path, provider, _url(fake, provider)))
    res = agent.invoke({"messages": [("user", "go")]})
    assert res["messages"][-1].content == "both done"
    assert len(fake.requests) == 2
    second = fake.requests[1]["body"]["messages"]
    assert [m["content"] for m in second if m["role"] == "tool"] == ["echo:a", "B"]
    asst = [m for m in second if m["role"] == "assistant"][0]
    assert len(asst["tool_calls"]) == 2
    if provider == "openai_compatible":
        assert [m["tool_call_id"] for m in second if m["role"] == "tool"] == ["call_1", "call_2"]


@pytest.mark.parametrize("provider", PROVIDERS)
def test_tool_calls_come_back_as_langchain_tool_calls(fake, tmp_path, provider):
    fake.plan = [_ok(_calls(provider, [("call_9", "echo", {"text": "q"})], text="thinking aloud"))]
    msg = ba.load_model(_cfg(tmp_path, provider, _url(fake, provider))).bind_tools([echo]).invoke("hi")
    assert msg.content == "thinking aloud"
    assert len(msg.tool_calls) == 1
    tc = msg.tool_calls[0]
    assert tc["name"] == "echo" and tc["args"] == {"text": "q"} and tc["id"]
    if provider == "openai_compatible":
        assert tc["id"] == "call_9"


# ------------------------------------------------------------ a malformed tool call is a clear error

def _bad_ollama(tc):
    return {"model": "m", "message": {"role": "assistant", "content": "", "tool_calls": tc}, "done": True}


def _bad_openai(tc):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tc}}]}


BAD = [
    ("arguments not json", lambda f: f([{"id": "c", "type": "function", "function": {"name": "echo", "arguments": "{oops"}}])),
    ("arguments not an object", lambda f: f([{"id": "c", "type": "function", "function": {"name": "echo", "arguments": "[1]"}}])),
    ("no name", lambda f: f([{"id": "c", "type": "function", "function": {"arguments": "{}"}}])),
    ("no function", lambda f: f([{"id": "c"}])),
    ("not a list", lambda f: f("echo")),
]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("label,make", BAD, ids=[b[0] for b in BAD])
def test_a_malformed_tool_call_is_a_clear_error_not_a_crash_or_an_empty_answer(fake, tmp_path, provider, label, make):  # NEGATIVE
    fake.plan = [_ok(make(_bad_ollama if provider == "ollama" else _bad_openai))]
    model = ba.load_model(_cfg(tmp_path, provider, _url(fake, provider))).bind_tools([echo])
    with pytest.raises(llm_client.LlmError) as ei:
        model.invoke("hi")
    assert "tool call that could not be read" in str(ei.value)
    assert len(fake.requests) == 1


@pytest.mark.parametrize("provider,body", [("ollama", {"done": True}), ("openai_compatible", {"choices": []}),
                                           ("openai_compatible", {"choices": [{"finish_reason": "stop"}]})])
def test_a_reply_with_no_message_is_a_clear_error(fake, tmp_path, provider, body):                      # NEGATIVE
    fake.plan = [_ok(body)]
    with pytest.raises(llm_client.LlmError, match="no message"):
        ba.load_model(_cfg(tmp_path, provider, _url(fake, provider))).invoke("hi")


# ------------------------------------------------------------ failures carry the client's own words

@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("status,words", [(401, "refused the key"), (404, "does not have the model"),
                                          (429, "rate-limiting")])
def test_http_failures_carry_the_same_messages_as_the_client(fake, tmp_path, provider, status, words):
    fake.plan = [(status, {"error": {"message": "synthetic provider text"}}, {}, 0)]
    cfg = _cfg(tmp_path, provider, _url(fake, provider))
    with pytest.raises(llm_client.LlmError) as ei:
        ba.load_model(cfg).invoke("hi")
    assert words in str(ei.value) and f"HTTP {status}" in str(ei.value)
    assert "synthetic provider text" in str(ei.value)
    assert len(fake.requests) == 1                                   # never retried
    # the very same failure through the plain client call
    ep = llm_client.resolve_endpoint(cfg)
    try:
        llm_client.chat(ep, "s", "u")
    except llm_client.LlmError as e:
        assert str(e) == str(ei.value)
    except llm_client.RateLimited as e:
        assert str(llm_client.rate_limit_error(ep, e)) == str(ei.value)


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_timeout_is_the_plain_limit_message_and_is_not_retried(fake, tmp_path, monkeypatch, provider):    # NEGATIVE
    monkeypatch.setattr(P, "LANGCHAIN_TIMEOUT_ONPREM_SECONDS", 1)
    fake.plan = [(200, _plain(provider), {}, 4)]
    slept = []
    monkeypatch.setattr(llm_chat_model, "_retry_pause", slept.append)
    with pytest.raises(P.LlmLimitError) as ei:
        ba.load_model(_cfg(tmp_path, provider, _url(fake, provider))).invoke("hi")
    assert str(ei.value).startswith("The model did not answer within 1 s")
    assert slept == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_server_error_that_mentions_a_timeout_is_not_called_our_time_limit(fake, tmp_path, provider):   # NEGATIVE
    fake.plan = [(504, {"error": {"message": "upstream timeout"}}, {}, 0)]
    with pytest.raises(llm_client.LlmError) as ei:
        ba.load_model(_cfg(tmp_path, provider, _url(fake, provider))).invoke("hi")
    assert "HTTP 504" in str(ei.value) and not isinstance(ei.value, P.LlmLimitError)


# ------------------------------------------------------------ no endpoint, no model: the plain message

@pytest.mark.parametrize("provider", PROVIDERS)
def test_no_model_fails_plainly_and_sends_nothing(fake, tmp_path, provider):                          # NEGATIVE
    cfg = _cfg(tmp_path, provider, _url(fake, provider), model=None)
    for call in (lambda: ba.load_model(cfg), lambda: ba.run_direct("hi", "s", cfg),
                 lambda: ba.build_agent([echo], "s", cfg)):
        with pytest.raises(ValueError, match="No model configured"):
            call()
    assert fake.requests == []


@pytest.mark.parametrize("provider", PROVIDERS)
def test_no_address_fails_plainly_and_guesses_none(tmp_path, monkeypatch, provider):                  # NEGATIVE
    monkeypatch.setattr(request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wire")))
    cfg = _cfg(tmp_path, provider, "")
    with pytest.raises(ValueError, match="No base_url"):
        ba.run_direct("hi", "s", cfg)


def test_no_provider_fails_plainly(tmp_path):                                                          # NEGATIVE
    p = tmp_path / "c.yaml"
    p.write_text("ollama:\n  base_url: http://localhost:11434\n  default_model: m\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provider"):
        ba.load_model(str(p))


def test_no_config_at_all_points_to_settings(tmp_path):
    with pytest.raises(FileNotFoundError, match="Settings tab"):
        ba.load_model(str(tmp_path / "missing.yaml"))


def test_no_fallback_model_names_are_left_in_the_agent_code():                                           # NEGATIVE
    src = (ROOT / "src/agents/base_agent.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert 'or "gpt-4o"' not in code and 'or "gemma4:12b"' not in code
    assert not re.search(r"get\(.base_url., .http", code)


# ------------------------------------------------------------ the key

def _http(code, body):
    return ue.HTTPError(HTTPS + "/chat/completions", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


def test_the_key_is_in_no_raised_message_log_line_or_repr(tmp_path, monkeypatch, caplog, capsys):        # NEGATIVE
    caplog.set_level("DEBUG")
    cfg = _cfg(tmp_path, "openai_compatible", HTTPS, "m", api_key=KEY)
    model = ba.load_model(cfg)
    excs = [_http(401, {"error": {"message": f"bad {KEY}"}}), _http(500, {"error": {"message": f"Bearer {KEY}"}}),
            _http(404, {"error": {"message": f"model gone {KEY}"}}), _http(429, {"error": {"message": f"slow {KEY}"}}),
            OSError(f"reset {KEY}"), ue.URLError(f"nope {KEY}")]
    monkeypatch.setattr(llm_chat_model, "_retry_pause", lambda s: None)
    for exc in excs:
        monkeypatch.setattr(request, "urlopen", lambda *a, _e=exc, **k: (_ for _ in ()).throw(_e))
        with pytest.raises(Exception) as ei:
            model.invoke("hi")
        assert KEY not in str(ei.value) and KEY not in repr(ei.value) and KEY not in repr(ei.value.__cause__)
    assert KEY not in repr(model) and KEY not in str(model)
    out = capsys.readouterr()
    assert KEY not in out.out and KEY not in out.err and KEY not in caplog.text


def test_a_key_over_plain_http_is_refused_before_anything_is_sent(fake, tmp_path):                       # NEGATIVE
    from agents import endpoint_policy
    cfg = _cfg(tmp_path, "openai_compatible", fake.url + "/v1", "m", api_key=KEY)
    with pytest.raises(endpoint_policy.EndpointPolicyError) as ei:
        ba.load_model(cfg)
    assert KEY not in str(ei.value) and fake.requests == []


def test_the_key_is_sent_as_a_bearer_token_over_https_only(tmp_path, monkeypatch):
    seen = {}

    def fake_open(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read1(self, n):
                if getattr(self, "d", False):
                    return b""
                self.d = True
                return json.dumps(_plain("openai_compatible")).encode()
        return R()
    monkeypatch.setattr(request, "urlopen", fake_open)
    ba.load_model(_cfg(tmp_path, "openai_compatible", HTTPS, "m", api_key=KEY)).invoke("hi")
    assert seen["auth"] == f"Bearer {KEY}"


# ------------------------------------------------------------ the step limit and the retries

@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_tool_loop_that_never_ends_stops_at_the_step_limit(fake, tmp_path, provider):                  # NEGATIVE
    fake.plan = [_ok(_calls(provider, [(f"call_{i}", "echo", {"text": str(i)})])) for i in range(60)]
    agent = ba.build_agent([echo], "SYS", _cfg(tmp_path, provider, _url(fake, provider)))
    with pytest.raises(P.LlmLimitError) as ei:
        agent.invoke({"messages": [("user", "go")]})
    assert str(ei.value).startswith("The run stopped after 15 steps without finishing")
    assert 1 < len(fake.requests) <= 16


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_dead_connection_is_retried_twice_and_paced_once(tmp_path, monkeypatch, provider):
    url = f"http://127.0.0.1:{_closed_port()}"
    cfg = _cfg(tmp_path, provider, url if provider == "ollama" else url + "/v1")
    llm_pacing.reset_registry()
    monkeypatch.setattr(llm_pacing, "default_state_path", lambda: tmp_path / "pace.json")
    model = ba.load_model(cfg)
    pacer = llm_pacing.pacer_for(model.endpoint.provider, model.endpoint.base_url)
    n = {"acquire": 0}
    real = pacer.acquire
    monkeypatch.setattr(pacer, "acquire", lambda: (n.__setitem__("acquire", n["acquire"] + 1), real())[1])
    sleeps = []
    monkeypatch.setattr(llm_chat_model, "_retry_pause", sleeps.append)
    with pytest.raises(llm_client.LlmError, match="could not be reached"):
        model.invoke("hi")
    assert sleeps == [1, 1]
    assert n["acquire"] == 1                                # three attempts, one slot on the pacer
    llm_pacing.reset_registry()


def test_a_paced_out_call_sends_nothing(fake, tmp_path):                                                 # NEGATIVE
    fake.plan = [_ok(_plain("ollama"))]
    cfg = _cfg(tmp_path, "ollama", fake.url, daily_cap=1)
    model = ba.load_model(cfg)
    model.invoke("one")
    with pytest.raises(llm_pacing.PacingStopped):
        model.invoke("two")
    assert len(fake.requests) == 1


def test_a_slow_trickle_is_cut_at_the_total_limit(tmp_path, monkeypatch):                                # NEGATIVE
    import time
    monkeypatch.setattr(P, "LANGCHAIN_TIMEOUT_ONPREM_SECONDS", 1)

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read1(self, n):
            time.sleep(0.2)
            return b"x"
    monkeypatch.setattr(request, "urlopen", lambda *a, **k: R())
    t0 = time.time()
    with pytest.raises(P.LlmLimitError):
        ba.load_model(_cfg(tmp_path, "ollama", "http://localhost:11434")).invoke("hi")
    assert time.time() - t0 < 3


def test_a_reply_cut_at_the_cap_is_flagged_through_the_agent(fake, tmp_path):
    fake.plan = [_ok(_plain("ollama", "partial", "length"))]
    res = ba.build_agent([], "SYS", _cfg(tmp_path, "ollama", fake.url)).invoke({"messages": [("user", "go")]})
    assert P.reply_cap_note() in res["messages"][-1].content


# ------------------------------------------------------------ one client, one place

def test_the_client_imports_without_langchain():
    code = "import sys; sys.path.insert(0, %r); import agents.llm_client; " \
           "bad=[m for m in sys.modules if m.split('.')[0] in ('langchain','langchain_core','langgraph')]; " \
           "sys.exit(1 if bad else 0)" % str(ROOT / "src")
    assert subprocess.run([sys.executable, "-I", "-c", code]).returncode == 0


def test_build_agent_uses_the_current_langchain_factory():
    src = (ROOT / "src/agents/base_agent.py").read_text(encoding="utf-8")
    assert "from langchain.agents import create_agent" in src and "create_react_agent" not in src


ALLOWED_WIRE = {
    "src/agents/llm_client.py",     # the one AI client
    "src/run_4c_e2e.py",            # readiness poll of the app's own page
    "ui/webui.py",                  # readiness poll of the app's own page
    "ui/_health.py",                # the health check's own plain GET
    "ui/_update.py",                # the update check
}


def test_only_the_client_and_the_known_non_ai_sites_touch_the_wire_or_the_vendor_classes():          # NEGATIVE
    pat = re.compile(r"urlopen|ChatOpenAI|ChatOllama")
    hits = set()
    for base in ("src", "ui"):
        for p in (ROOT / base).rglob("*.py"):
            if pat.search(p.read_text(encoding="utf-8", errors="replace")):
                hits.add(str(p.relative_to(ROOT)).replace("\\", "/"))
    assert hits == ALLOWED_WIRE
