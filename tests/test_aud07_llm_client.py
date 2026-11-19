"""AUD-07 P1: the single AI client, tested against a local fake HTTP server.

Synthetic data only. Nothing in the app calls the client yet.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agents import llm_client as lc  # noqa: E402
from agents import llm_errors, llm_pacing  # noqa: E402

KEY = "sk-synthetic-ABCDEF123456"
SHORT_KEY = "k9z"


class _Fake:
    """A tiny fake AI service. ``plan`` is a list of (status, body, headers,
    delay) answered in order; the last one repeats."""

    def __init__(self):
        self.requests = []
        self.plan = [(200, {}, {}, 0.0)]
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silent
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                outer.requests.append({
                    "method": self.command, "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(raw) if raw else None,
                })
                i = min(len(outer.requests) - 1, len(outer.plan) - 1)
                status, body, headers, delay = outer.plan[i]
                if delay:
                    time.sleep(delay)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            do_POST = _answer
            do_GET = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fake():
    llm_pacing.reset_registry()
    f = _Fake()
    yield f
    f.stop()
    llm_pacing.reset_registry()


def _ollama(f, **kw):
    return lc.Endpoint("ollama", f.url, "tiny-model", None, **kw)


def _openai(f, key=None, **kw):
    # a key is never sent over plain http (even to this computer), so the
    # fake server is used keyless; key-bearing calls go through _https()
    return lc.Endpoint("openai_compatible", f.url + "/v1", "tiny-model", key, **kw)


class _Resp:
    def __init__(self, body):
        self._b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._b).encode() if not isinstance(self._b, bytes) else self._b


def _https(monkeypatch, status=200, body=None, headers=None):
    """Replace the network with one canned answer; returns the list of requests."""
    import io
    import urllib.error as ue
    seen = []

    def fake_open(req, timeout=None):
        seen.append({"url": req.full_url, "auth": req.get_header("Authorization"),
                     "timeout": timeout,
                     "body": json.loads(req.data) if req.data else None})
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        if status == 200:
            return _Resp(body)
        raise ue.HTTPError(req.full_url, status, "x", headers or {}, io.BytesIO(data))
    monkeypatch.setattr(lc._req, "urlopen", fake_open)
    return seen


HTTPS_EP = lc.Endpoint("openai_compatible", "https://api.example.com/v1", "m", KEY)

OLLAMA_OK = {"message": {"content": "Expenses:Food", "thinking": "no"}, "done_reason": "stop"}
OPENAI_OK = {"choices": [{"message": {"content": "Expenses:Food", "reasoning": "no"},
                          "finish_reason": "stop"}]}


def _both(f):
    return [(_ollama(f), OLLAMA_OK), (_openai(f), OPENAI_OK)]


# ---------------------------------------------------------------- success

@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_success_returns_content_and_finish_reason(fake, provider):
    ep, ok = _both(fake)[0 if provider == "ollama" else 1]
    fake.plan = [(200, ok, {}, 0)]
    r = lc.chat(ep, "sys", "usr")
    assert isinstance(r, lc.Reply) and r == "Expenses:Food" and r.finish_reason == "stop"
    sent = fake.requests[0]
    assert sent["body"]["messages"] == [{"role": "system", "content": "sys"},
                                        {"role": "user", "content": "usr"}]
    assert sent["path"] == ("/api/chat" if provider == "ollama" else "/v1/chat/completions")
    assert sent["body"]["stream"] is False


def test_reply_cap_defaults_to_the_models_cap_and_can_be_set(fake):
    from agents.llm_model_params import reply_cap_for
    fake.plan = [(200, OLLAMA_OK, {}, 0)]
    lc.chat(_ollama(fake), "s", "u")
    assert fake.requests[-1]["body"]["options"]["num_predict"] == reply_cap_for("tiny-model")
    lc.chat(_ollama(fake, max_tokens=77), "s", "u")
    assert fake.requests[-1]["body"]["options"]["num_predict"] == 77
    fake.plan = [(200, OPENAI_OK, {}, 0)]
    lc.chat(_openai(fake, max_tokens=55), "s", "u")
    assert fake.requests[-1]["body"]["max_tokens"] == 55


def test_key_goes_only_in_the_header_of_the_openai_route(fake, monkeypatch):
    seen = _https(monkeypatch, 200, OPENAI_OK)
    lc.chat(HTTPS_EP, "s", "u")
    assert seen[0]["auth"] == f"Bearer {KEY}"
    assert KEY not in json.dumps(seen[0]["body"])
    fake.plan = [(200, OLLAMA_OK, {}, 0)]
    monkeypatch.undo()
    lc.chat(lc.Endpoint("ollama", fake.url, "m", KEY), "s", "u")
    assert fake.requests[-1]["auth"] is None            # NEGATIVE: not sent to ollama
    fake.plan = [(200, OPENAI_OK, {}, 0)]
    lc.chat(_openai(fake, key="not-needed"), "s", "u")
    assert fake.requests[-1]["auth"] is None            # NEGATIVE: the placeholder sends nothing


def test_a_key_is_refused_even_on_this_computer_over_http(fake):
    with pytest.raises(lc.LlmError):
        lc.chat(_openai(fake, key=KEY), "s", "u")
    assert fake.requests == []                          # NEGATIVE: nothing sent


# ---------------------------------------------------------------- failures

@pytest.mark.parametrize("status,kind", [(401, "auth"), (404, "model_not_found"),
                                         (402, "out_of_credits")])
@pytest.mark.parametrize("which", [0, 1])
def test_http_failures_are_classified(fake, which, status, kind):
    ep, _ = _both(fake)[which]
    fake.plan = [(status, {"error": {"message": f"bad thing with {KEY}"}}, {}, 0)]
    with pytest.raises(lc.LlmError) as ei:
        lc.chat(ep, "s", "u")
    assert ei.value.failure.kind == kind and ei.value.failure.status == status
    assert len(fake.requests) == 1                      # no retry


@pytest.mark.parametrize("which", [0, 1])
def test_429_raises_rate_limited_with_retry_after_and_never_retries(fake, which):
    ep, _ = _both(fake)[which]
    fake.plan = [(429, {"error": {"message": "slow down"}}, {"Retry-After": "7"}, 0)]
    with pytest.raises(lc.RateLimited) as ei:
        lc.chat(ep, "s", "u")
    assert ei.value.retry_after == 7.0
    assert len(fake.requests) == 1                      # NEGATIVE: no own retry


@pytest.mark.parametrize("which", [0, 1])
def test_429_without_retry_after(fake, which):
    ep, _ = _both(fake)[which]
    fake.plan = [(429, {"error": {"message": "slow down"}}, {}, 0)]
    with pytest.raises(lc.RateLimited) as ei:
        lc.chat(ep, "s", "u")
    assert ei.value.retry_after is None
    assert len(fake.requests) == 1


def test_429_that_says_out_of_credits_is_a_failure_not_a_wait(fake):
    fake.plan = [(429, {"error": {"message": "insufficient_quota: add credit"}}, {}, 0)]
    with pytest.raises(lc.LlmError) as ei:
        lc.chat(_openai(fake), "s", "u")
    assert ei.value.failure.kind == "out_of_credits"


def test_list_shaped_gemini_error_body(fake):
    fake.plan = [(400, [{"error": {"message": "API key not valid", "status": "INVALID_ARGUMENT"}}],
                  {}, 0)]
    with pytest.raises(lc.LlmError) as ei:
        lc.chat(_openai(fake), "s", "u")
    assert "API key not valid" in ei.value.failure.provider_message
    fake.plan = [(429, [{"error": {"message": "quota per minute", "status": "RESOURCE_EXHAUSTED"}}],
                  {}, 0)]
    with pytest.raises(lc.RateLimited):
        lc.chat(_openai(fake), "s", "u")


@pytest.mark.parametrize("which", [0, 1])
def test_timeout(fake, which):
    ep, ok = _both(fake)[which]
    fake.plan = [(200, ok, {}, 1.5)]
    t0 = time.monotonic()
    with pytest.raises(lc.LlmError) as ei:
        lc.chat(ep, "s", "u", timeout=0.3)
    assert ei.value.failure.kind == "timeout"
    assert time.monotonic() - t0 < 1.4


def test_endpoint_timeout_is_used_when_none_is_given(fake):
    fake.plan = [(200, OLLAMA_OK, {}, 1.5)]
    with pytest.raises(lc.LlmError) as ei:
        lc.chat(_ollama(fake, timeout=0.3), "s", "u")
    assert ei.value.failure.kind == "timeout"


def test_connection_refused_is_a_connection_failure():
    ep = lc.Endpoint("ollama", "http://127.0.0.1:9", "m", None)
    with pytest.raises(lc.LlmError) as ei:
        lc.chat(ep, "s", "u", timeout=2)
    assert ei.value.failure.kind in ("connection", "timeout")


def test_non_json_reply_is_a_failure_not_a_crash(fake):
    fake.plan = [(200, b"<html>no</html>", {}, 0)]
    with pytest.raises(lc.LlmError):
        lc.chat(_ollama(fake), "s", "u")


def test_unknown_provider_fails_loud():
    with pytest.raises(ValueError):
        lc.chat(lc.Endpoint("nope", "http://127.0.0.1:1", "m", None), "s", "u")


# ---------------------------------------------------------------- pacing

def test_paced_out_call_raises_pacing_stopped_and_sends_nothing(fake, tmp_path):
    ep = _ollama(fake)
    fake.plan = [(200, OLLAMA_OK, {}, 0)]
    llm_pacing.configure("ollama", fake.url, 0.0, 1, state_path=tmp_path / "p.json")
    lc.chat(ep, "s", "u")
    with pytest.raises(llm_pacing.PacingStopped):
        lc.chat(ep, "s", "u")
    assert len(fake.requests) == 1


# ---------------------------------------------------------------- probe / list

@pytest.mark.parametrize("which", [0, 1])
def test_probe_sends_one_ping_message(fake, which):
    ep, ok = _both(fake)[which]
    fake.plan = [(200, ok, {}, 0)]
    assert lc.probe(ep) is None
    assert fake.requests[0]["body"]["messages"] == [{"role": "user", "content": "ping"}]
    if which == 0:
        assert fake.requests[0]["body"]["options"]["num_predict"] == 1


def test_probe_401_is_an_auth_failure_not_unreachable(fake):
    fake.plan = [(401, {"error": {"message": "nope"}}, {}, 0)]
    with pytest.raises(lc.LlmError) as ei:
        lc.probe(_openai(fake))
    assert ei.value.failure.kind == "auth"


def test_list_models_both_providers(fake):
    fake.plan = [(200, {"models": [{"name": "a:1", "size": 5}, "junk"]}, {}, 0)]
    assert lc.list_models(_ollama(fake)) == [{"name": "a:1", "size": 5}]
    assert fake.requests[-1]["path"] == "/api/tags"
    fake.plan = [(200, {"data": [{"id": "m1"}, {"id": "m2"}]}, {}, 0)]
    assert [m["id"] for m in lc.list_models(_openai(fake))] == ["m1", "m2"]
    assert fake.requests[-1]["path"] == "/v1/models"


def test_list_models_401(fake):
    fake.plan = [(401, {"error": {"message": "no"}}, {}, 0)]
    with pytest.raises(lc.LlmError) as ei:
        lc.list_models(_openai(fake))
    assert ei.value.failure.kind == "auth"


# ---------------------------------------------------------------- resolve

def _cfg(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def test_resolve_endpoint_reads_the_block_named_by_provider(tmp_path):
    p = _cfg(tmp_path, "provider: openai_compatible\nopenai_compatible:\n"
                       "  base_url: http://127.0.0.1:1/v1/\n  default_model: m1\n"
                       "  api_key: sk-synthetic-ZZZZZZ\n  temperature: 0.2\n")
    ep = lc.resolve_endpoint(p)
    assert (ep.provider, ep.base_url, ep.model, ep.api_key, ep.temperature) == \
        ("openai_compatible", "http://127.0.0.1:1/v1", "m1", "sk-synthetic-ZZZZZZ", 0.2)
    assert (ep.timeout, ep.warmup_timeout, ep.max_tokens) == (60.0, 180.0, None)
    assert lc.resolve_endpoint(p, model_override="m2").model == "m2"


def test_resolve_endpoint_fails_plainly_with_no_guess(tmp_path):
    with pytest.raises(ValueError, match="No model"):
        lc.resolve_endpoint(_cfg(tmp_path, "provider: ollama\nollama:\n  base_url: http://127.0.0.1:1\n"))
    with pytest.raises(ValueError, match="No base_url"):
        lc.resolve_endpoint(_cfg(tmp_path, "provider: ollama\nollama:\n  default_model: m\n"))
    with pytest.raises(ValueError, match="provider"):
        lc.resolve_endpoint(_cfg(tmp_path, "ollama:\n  base_url: http://127.0.0.1:1\n  default_model: m\n"))
    with pytest.raises(ValueError, match="provider"):
        lc.resolve_endpoint(_cfg(tmp_path, "provider: mystery\nmystery:\n  base_url: x\n  default_model: m\n"))


def test_resolve_endpoint_registers_pacing(tmp_path):
    llm_pacing.reset_registry()
    p = _cfg(tmp_path, "provider: ollama\nollama:\n  base_url: http://127.0.0.1:5\n"
                       "  default_model: m\n  min_gap_seconds: 3\n  daily_cap: 9\n")
    lc.resolve_endpoint(p)
    pacer = llm_pacing.pacer_for("ollama", "http://127.0.0.1:5")
    assert pacer is not None and pacer.min_gap == 3 and pacer.daily_cap == 9
    llm_pacing.reset_registry()


# ---------------------------------------------------------------- the key never leaks

def test_repr_and_str_hide_the_key():
    ep = lc.Endpoint("openai_compatible", "https://api.example.com/v1", "m", KEY)
    for text in (repr(ep), str(ep), f"{ep}", f"{ep!r}"):
        assert KEY not in text and "***" in text
    assert KEY not in repr([ep]) and KEY not in repr({"ep": ep})


def test_key_never_appears_in_a_raised_message_or_log(monkeypatch, caplog):
    caplog.set_level("DEBUG")
    for key in (KEY, SHORT_KEY):
        ep = lc.Endpoint("openai_compatible", "https://api.example.com/v1", "m", key)
        for status in (400, 401, 404, 500):
            _https(monkeypatch, status, {"error": {"message": f"echo {key} and Bearer {key} here"}})
            with pytest.raises(lc.LlmError) as ei:
                lc.chat(ep, "s", "u")
            f = ei.value.failure
            for text in (str(ei.value), repr(ei.value), f.reason, f.provider_message):
                assert key not in text
    _https(monkeypatch, 500, b"plain text echoing " + KEY.encode())
    with pytest.raises(lc.LlmError) as ei:
        lc.chat(HTTPS_EP, "s", "u")
    assert KEY not in str(ei.value) and KEY not in ei.value.failure.provider_message
    assert KEY not in caplog.text


def test_key_on_public_http_is_refused_before_anything_is_sent(fake, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a request was attempted")
    monkeypatch.setattr(lc._req, "urlopen", boom)
    ep = lc.Endpoint("openai_compatible", "http://api.example.com/v1", "m", KEY)
    with pytest.raises(lc.LlmError) as ei:
        lc.chat(ep, "s", "u")
    assert KEY not in str(ei.value)
    with pytest.raises(lc.LlmError):
        lc.probe(ep)
    with pytest.raises(lc.LlmError):
        lc.list_models(ep)
    assert fake.requests == []


def test_key_on_public_http_never_reaches_a_real_socket(fake):
    ep = lc.Endpoint("openai_compatible", "http://203.0.113.9:1/v1", "m", KEY)
    t0 = time.monotonic()
    with pytest.raises(lc.LlmError):
        lc.chat(ep, "s", "u", timeout=30)
    assert time.monotonic() - t0 < 2          # refused up front, no connect wait


def test_https_with_a_key_is_allowed_by_the_address_rule(monkeypatch):
    seen = _https(monkeypatch, 200, OPENAI_OK)
    assert lc.chat(HTTPS_EP, "s", "u") == "Expenses:Food"
    assert seen[0]["url"] == "https://api.example.com/v1/chat/completions"
    assert seen[0]["auth"] == f"Bearer {KEY}" and seen[0]["timeout"] == 60.0
    seen = _https(monkeypatch, 200, {"data": [{"id": "m1"}]})
    assert lc.list_models(HTTPS_EP) == [{"id": "m1"}]
    assert seen[0]["auth"] == f"Bearer {KEY}"
    seen = _https(monkeypatch, 401, {"error": {"message": "no"}})
    with pytest.raises(lc.LlmError) as ei:
        lc.list_models(HTTPS_EP)
    assert ei.value.failure.kind == "auth"


# ---------------------------------------------------------------- redact widened

def _mapper_rx():
    src = (ROOT / "src/agents/skill_gnucash_account_mapper/agent.py").read_text(encoding="utf-8")
    m = re.search(r'^_SECRET_LIKE_RX = re\.compile\((r".*?")\)$', src, re.M)
    assert m, "mapper pattern moved"
    return re.compile(eval(m.group(1)))  # noqa: S307 - a literal from our own source


@pytest.mark.parametrize("text", [
    "Bearer abc.def-123", "sk-ABCDEFGH", "key_12345678", "tok-abcdefgh_ijk",
    "SK-UPPERCASE1", "echo sk-abcdefghijkl and tok_abcdefghij end",
])
def test_llm_errors_redact_is_a_superset_of_the_mappers_pattern(text):
    rx = _mapper_rx()
    assert rx.search(text)                       # the mapper catches it ...
    out = llm_errors.redact(text)
    for m in rx.finditer(text):
        assert m.group(0) not in out             # ... so redact must too


def test_redact_hides_an_explicit_secret_of_any_length():
    assert "k9z" not in llm_errors.redact("the key k9z failed", ("k9z",))
    assert llm_errors.redact("abc", ("", None)) == "abc"          # NEGATIVE: empty is ignored
    assert llm_errors.redact("plain words only") == "plain words only"


# ---------------------------------------------------------------- nothing calls it yet

def test_no_module_imports_llm_client_yet():
    pat = re.compile(r"\bllm_client\b")
    offenders = []
    for base in ("src", "ui", "scripts"):
        for p in (ROOT / base).rglob("*.py"):
            if p.name == "llm_client.py":
                continue
            if pat.search(p.read_text(encoding="utf-8", errors="replace")):
                offenders.append(str(p.relative_to(ROOT)))
    for p in ROOT.glob("*.py"):
        if pat.search(p.read_text(encoding="utf-8", errors="replace")):
            offenders.append(p.name)
    assert offenders == []


def test_client_imports_nothing_from_ui_or_langchain():
    src = (ROOT / "src/agents/llm_client.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+(ui|langchain\w*)\b", src, re.M)
