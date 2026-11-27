"""AUD-07 P3: the account mapper sends its AI calls through the single client.

The mapper's names and its "reply or None" contract are unchanged. Synthetic
data only; a local fake service, or urlopen replaced.
"""
from __future__ import annotations

import io
import json
import urllib.error as ue
from pathlib import Path
from urllib import request

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

from agents import llm_client, llm_pacing  # noqa: E402
from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
from tests.test_aud07_llm_client import _Fake, OLLAMA_OK, OPENAI_OK  # noqa: E402

KEY = "sk-synthetic-MAPPERKEY123456"
HTTPS = "https://llm.example.invalid/v1"


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    llm_pacing.reset_registry()
    monkeypatch.setattr(llm_pacing, "default_state_path", lambda: tmp_path / "pace.json")
    f = _Fake()
    m._LLM_LAST_ERROR.clear()
    yield f
    f.stop()
    llm_pacing.reset_registry()


def _cfg(tmp_path, url, provider="ollama", **extra):
    blk = {"base_url": url, "default_model": "synthetic-model", **extra}
    p = tmp_path / "llm.yaml"
    p.write_text(yaml.safe_dump({"provider": provider, provider: blk}), encoding="utf-8")
    return str(p)


def _rows():
    return [{"row": 1, "description": "SYNTHETIC SHOP", "withdrawal": "100", "deposit": ""}]


def _count_acquires(monkeypatch, provider, url):
    pacer = llm_pacing.configure(provider, url, 0.0, 0)
    n = {"acquire": 0}
    real = pacer.acquire

    def counted():
        n["acquire"] += 1
        return real()
    monkeypatch.setattr(pacer, "acquire", counted)
    return n


# ------------------------------------------------------------ names and aliases

def test_the_mapper_names_are_the_clients(fake):
    assert m._LLMRateLimited is llm_client.RateLimited
    assert m._Reply is llm_client.Reply
    assert m._parse_retry_after is llm_client.parse_retry_after
    for name in ("_llm_chat", "_ollama_chat", "_openai_compatible_chat", "_resolve_llm_endpoint_config",
                 "_redact_llm_text", "_provider_message", "_note_llm_error", "_LLMGuard",
                 "_LLM_TIMEOUT_SECONDS", "_LLM_WARMUP_TIMEOUT"):
        assert hasattr(m, name)
    assert (m._LLM_TIMEOUT_SECONDS, m._LLM_WARMUP_TIMEOUT) == (60, 180)


# ------------------------------------------------------------ pacing exactly once

@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_a_call_is_paced_exactly_once(fake, monkeypatch, provider):             # NEGATIVE: not twice
    url = fake.url if provider == "ollama" else fake.url + "/v1"
    fake.plan = [(200, OLLAMA_OK if provider == "ollama" else OPENAI_OK, {}, 0)]
    n = _count_acquires(monkeypatch, provider, url)
    out = m._llm_chat(provider, url, "m", "s", "u")
    assert out == "Expenses:Food" and out.finish_reason == "stop"
    assert n["acquire"] == 1
    assert len(fake.requests) == 1


def test_a_patched_transport_is_still_paced_once(fake, monkeypatch):
    n = _count_acquires(monkeypatch, "ollama", fake.url)
    monkeypatch.setattr(m, "_ollama_chat", lambda *a, **k: "ok")
    assert m._llm_chat("ollama", fake.url, "m", "s", "u") == "ok"
    assert n["acquire"] == 1


def test_the_transport_functions_do_not_pace_by_themselves(fake, monkeypatch):  # as before
    fake.plan = [(200, OLLAMA_OK, {}, 0)]
    n = _count_acquires(monkeypatch, "ollama", fake.url)
    assert m._ollama_chat(fake.url, "m", "s", "u") == "Expenses:Food"
    assert n["acquire"] == 0


# ------------------------------------------------------------ the same request as before

def test_ollama_request_values_are_the_mappers_own(fake):
    fake.plan = [(200, OLLAMA_OK, {}, 0)]
    m._ollama_chat(fake.url, "gemini-3-pro", "SYS", "USR")
    body = fake.requests[0]["body"]
    assert fake.requests[0]["path"] == "/api/chat"
    assert body["options"] == {"temperature": 0.0, "num_predict": m.LLM_REPLY_MAX_TOKENS}   # 300, whatever the name
    assert body["stream"] is False and fake.requests[0]["auth"] is None


def test_openai_request_values_are_the_mappers_own(fake):
    fake.plan = [(200, OPENAI_OK, {}, 0)]
    m._openai_compatible_chat(fake.url + "/v1", "models/gemini-2.5-pro", "SYS", "USR")
    body = fake.requests[0]["body"]
    assert fake.requests[0]["path"] == "/v1/chat/completions"
    assert body["model"] == "gemini-2.5-pro"
    assert body["max_tokens"] == m._reply_cap_for("models/gemini-2.5-pro") == m.LLM_REPLY_MAX_TOKENS_THINKING
    assert body["temperature"] == 0.0 and body["stream"] is False
    assert body.get("reasoning_effort") == m._reasoning_effort_for("models/gemini-2.5-pro")


def test_the_timeout_given_is_the_timeout_used(monkeypatch):
    seen = []

    def fake_open(req, timeout=None):
        seen.append(timeout)

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(OPENAI_OK).encode()
        return R()
    monkeypatch.setattr(request, "urlopen", fake_open)
    m._openai_compatible_chat(HTTPS, "m", "s", "u", api_key=KEY, timeout=m._LLM_WARMUP_TIMEOUT)
    m._openai_compatible_chat(HTTPS, "m", "s", "u", api_key=KEY, timeout=m._LLM_TIMEOUT_SECONDS)
    m._openai_compatible_chat(HTTPS, "m", "s", "u", api_key=KEY)
    assert seen == [180, 60, 60.0]


# ------------------------------------------------------------ 429: no retry, the guard decides

@pytest.mark.parametrize("fn", ["_ollama_chat", "_openai_compatible_chat"])
def test_429_is_raised_once_and_never_retried(fake, fn):                       # NEGATIVE
    fake.plan = [(429, {"error": {"message": "slow down"}}, {"Retry-After": "5"}, 0)]
    url = fake.url if fn == "_ollama_chat" else fake.url + "/v1"
    with pytest.raises(m._LLMRateLimited) as ei:
        getattr(m, fn)(url, "m", "s", "u")
    assert ei.value.retry_after == 5.0 and len(fake.requests) == 1


def test_the_guard_waits_out_a_429_then_takes_the_reply(fake, monkeypatch):
    fake.plan = [(429, {}, {"Retry-After": "3"}, 0), (200, OLLAMA_OK, {}, 0)]
    waits = []
    monkeypatch.setattr(m, "_llm_sleep", waits.append)
    guard = m._LLMGuard("m")
    assert guard.ask("ollama", fake.url, "m", "s", "u") == "Expenses:Food"
    assert waits == [3.0] and len(fake.requests) == 2


# ------------------------------------------------------------ never a guessed account

def test_a_rate_limited_pass_writes_no_guessed_account(fake, tmp_path, monkeypatch):    # NEGATIVE
    fake.plan = [(200, OLLAMA_OK, {}, 0)] + [(429, {}, {}, 0)]
    monkeypatch.setattr(m, "_llm_sleep", lambda s: None)
    out = m.llm_fallback_mapping(_rows(), ["Expenses:Food"], [], _cfg(tmp_path, fake.url))
    assert out == {}
    assert m._LLM_RUN_STATUS.get("stopped") is True


def test_a_paced_out_pass_writes_no_guessed_account(fake, tmp_path):             # NEGATIVE
    fake.plan = [(200, OLLAMA_OK, {}, 0)]
    cfg = _cfg(tmp_path, fake.url, daily_cap=1)           # the warm-up uses the one call
    out = m.llm_fallback_mapping(_rows(), ["Expenses:Food"], [], cfg)
    assert out == {}
    assert m._LLM_RUN_STATUS.get("stopped") is True
    assert len(fake.requests) == 1


# ------------------------------------------------------------ the key

def _http(code, body):
    return ue.HTTPError(HTTPS + "/chat/completions", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


def test_the_key_is_in_no_mapper_message_log_or_last_error(monkeypatch, caplog, capsys):   # NEGATIVE
    caplog.set_level("DEBUG")
    excs = [_http(401, {"error": {"message": f"bad {KEY}"}}), _http(500, {"error": {"message": f"Bearer {KEY}"}}),
            _http(404, {"error": {"message": f"model gone {KEY}"}}), OSError(f"reset {KEY}"),
            ue.URLError(f"nope {KEY}")]
    for exc in excs:
        monkeypatch.setattr(request, "urlopen", lambda *a, _e=exc, **k: (_ for _ in ()).throw(_e))
        m._LLM_LAST_ERROR.clear()
        assert m._openai_compatible_chat(HTTPS, "m", "s", "u", api_key=KEY, timeout=3) is None
        assert KEY not in json.dumps(m._LLM_LAST_ERROR)
    assert KEY not in capsys.readouterr().out and KEY not in caplog.text


def test_the_mapper_still_masks_with_three_stars():
    assert m._redact_llm_text(f"x {KEY} y", KEY) == "x *** y"
    assert m._redact_llm_text("Bearer abc.def") == "***"


def test_no_not_needed_bearer_header_is_sent_any_more(fake):
    fake.plan = [(200, OPENAI_OK, {}, 0)]
    m._openai_compatible_chat(fake.url + "/v1", "m", "s", "u", api_key="not-needed")
    assert fake.requests[0]["auth"] is None


def test_a_key_on_plain_http_is_refused_before_anything_is_sent(fake):          # NEGATIVE
    out = m._openai_compatible_chat(fake.url + "/v1", "m", "s", "u", api_key=KEY)
    assert out is None and fake.requests == []
    err = m._take_llm_error()
    assert err["fatal"] is True and "plain http" in err["reason"] and KEY not in err["reason"]


# ------------------------------------------------------------ the endpoint lookup stays strict

def test_resolve_endpoint_config_returns_the_same_tuple_and_stays_strict(tmp_path):
    p = _cfg(tmp_path, "http://127.0.0.1:5/", "openai_compatible", api_key="k1", temperature=0.3)
    assert m._resolve_llm_endpoint_config(p) == ("openai_compatible", "http://127.0.0.1:5", "synthetic-model", "k1", 0.3)
    assert m._resolve_llm_endpoint_config(p, "other")[2] == "other"
    q = tmp_path / "q.yaml"
    q.write_text("provider: ollama\nollama:\n  base_url: http://127.0.0.1:5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="No model"):
        m._resolve_llm_endpoint_config(str(q))
    q.write_text("provider: ollama\nollama:\n  default_model: m\n", encoding="utf-8")
    with pytest.raises(ValueError, match="No base_url"):
        m._resolve_llm_endpoint_config(str(q))
    q.write_text("ollama:\n  base_url: http://x\n  default_model: m\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provider"):
        m._resolve_llm_endpoint_config(str(q))
