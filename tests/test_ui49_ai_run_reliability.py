"""UI-49: AI-run reliability (fixes a-g). Synthetic fixtures, mocked HTTP only."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "agents"))

import agents.skill_gnucash_account_mapper.agent as m  # noqa: E402
from agents import llm_errors as le  # noqa: E402
from ui import _health  # noqa: E402
from ui.tabs import _generic, settings  # noqa: E402


def _http_error(code, body, headers=None):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return urllib.error.HTTPError("http://x/chat/completions", code, "err",
                                  headers or {}, io.BytesIO(raw))


class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(m, "_emit_mapper_progress", lambda s: None)
    monkeypatch.setattr(m, "_llm_sleep", lambda s: None)
    _health.clear_result_cache()
    _health.clear_capability_cache()
    _generic.invalidate_model_choices()
    yield
    _generic.invalidate_model_choices()


# ---------------------------------------------------------------- (a) window

def _guard_with(monkeypatch, outcomes):
    """Outcome True = the call gets a reply; False = no reply (non-fatal)."""
    it = iter(outcomes)

    def fake(*a, **k):
        m._LLM_LAST_ERROR.clear()
        m._LLM_LAST_ERROR.update({"reason": "HTTP 500", "fatal": False, "credits": False})
        return "ok" if next(it) else None
    monkeypatch.setattr(m, "_llm_chat", fake)
    return m._LLMGuard("mdl")


def test_a_occasional_refusals_do_not_stop_a_mostly_answered_run(monkeypatch):
    # 4 refusals in a row, then an answer, repeated: never 5 in a row, 80% fail
    # would need > 16 of 20; here 12 of 20 fail.
    pattern = ([False] * 4 + [True] * 2) * 4
    g = _guard_with(monkeypatch, pattern)
    for _ in pattern:
        g.ask("openai_compatible", "http://x", "mdl", "s", "u")
        assert not g.stopped
    # negative: the wrong behaviour (stopping) did not occur
    assert g.reason == ""


def test_a_run_that_mostly_fails_stops_even_with_rare_successes(monkeypatch):
    # 4 failures then 1 success, repeated: consecutive never reaches 5, but 16/20 fail
    # is exactly 80% (not more than) so it keeps going; 17+/20 stops.
    pattern = ([False] * 4 + [True]) * 3 + [False] * 5   # 17 failures in last 20
    g = _guard_with(monkeypatch, pattern)
    for _ in pattern:
        g.ask("openai_compatible", "http://x", "mdl", "s", "u")
        if g.stopped:
            break
    assert g.stopped
    assert "kept failing" in g.reason and "of the last 20" in g.reason or "in a row" in g.reason


def test_a_five_in_a_row_still_stops(monkeypatch):
    g = _guard_with(monkeypatch, [False] * 5)
    for _ in range(5):
        g.ask("openai_compatible", "http://x", "mdl", "s", "u")
    assert g.stopped and "5 calls in a row" in g.reason


def test_a_real_out_of_credits_stops_at_the_first_call(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(429, {"error": {"message": "Your credit balance is too low"}})
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    g = m._LLMGuard("mdl")
    assert g.ask("openai_compatible", "http://x", "mdl", "s", "u") is None
    assert g.stopped and g.out_of_credits


# ------------------------------------------------------- (b) rate vs credits

GEMINI_RATE = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                         "message": "You exceeded your current quota. Quota exceeded for metric: "
                                    "requests per minute. Please retry in 21s."}}


def test_b_quota_worded_rate_limit_is_not_out_of_credits():
    assert not le.is_out_of_credits(429, json.dumps(GEMINI_RATE))
    assert not le.is_out_of_credits(429, "exceeded your current quota, 20 requests per day")


def test_b_true_billing_still_out_of_credits():
    assert le.is_out_of_credits(429, "You exceeded your current quota, please check your plan and billing details")
    assert le.is_out_of_credits(402, "")
    assert le.is_out_of_credits(429, "insufficient_quota")
    assert le.is_out_of_credits(400, "Your credit balance is too low")
    assert not le.is_out_of_credits(429, "Too many requests")


def test_b_gemini_429_backs_off_as_rate_limit(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise _http_error(429, GEMINI_RATE)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(m._LLMRateLimited):
        m._openai_compatible_chat("http://x", "gemini-2.5-flash", "s", "u")
    g = m._LLMGuard("mdl")
    g.ask("openai_compatible", "http://x", "gemini-2.5-flash", "s", "u")
    assert g.stopped and g.rate_limited and not g.out_of_credits


# --------------------------------------------------- (c) reasoning / (e) name

def _capture_payload(monkeypatch, model):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen.update(json.loads(req.data))
        return _Resp({"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]})
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    m._openai_compatible_chat("http://x", model, "s", "u")
    return seen


def test_c_gemini_25_gets_limited_thinking(monkeypatch):
    assert _capture_payload(monkeypatch, "gemini-2.5-flash").get("reasoning_effort") == "low"


def test_c_gpt_oss_unchanged_and_others_get_none(monkeypatch):
    assert _capture_payload(monkeypatch, "openai/gpt-oss-20b").get("reasoning_effort") == "low"
    assert "reasoning_effort" not in _capture_payload(monkeypatch, "llama-3.3-70b-versatile")
    assert "reasoning_effort" not in _capture_payload(monkeypatch, "gemma3:12b")
    assert "reasoning_effort" not in _capture_payload(monkeypatch, "gemini-1.5-flash")


def test_e_chat_call_drops_the_models_prefix_and_leaves_others(monkeypatch):
    assert _capture_payload(monkeypatch, "models/gemini-2.5-flash")["model"] == "gemini-2.5-flash"
    assert _capture_payload(monkeypatch, "llama-3.3-70b-versatile")["model"] == "llama-3.3-70b-versatile"
    assert _capture_payload(monkeypatch, "qwen2.5:7b")["model"] == "qwen2.5:7b"


# ------------------------------------------------------------ (d) pre-check

OPENAI_EP = {"provider": "openai_compatible", "base_url": "http://host/v1", "default_model": "good"}


def _router(chat, models=("good", "other")):
    calls = {"chat": 0, "models": 0}

    def fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if url.endswith("/models"):
            calls["models"] += 1
            return _Resp({"data": [{"id": n} for n in models]})
        calls["chat"] += 1
        calls["sent"] = json.loads(req.data)
        return chat(req)
    return fake, calls


def test_d_good_endpoint_and_model_pass_with_one_call(monkeypatch):
    fake, calls = _router(lambda r: _Resp({"choices": []}))
    monkeypatch.setattr("urllib.request.urlopen", fake)
    health, used = _health.check_with_model(OPENAI_EP, "good")
    assert health.ok and used == "good" and calls["chat"] == 1
    assert calls["sent"]["max_tokens"] == 1


@pytest.mark.parametrize("code", [404, 400])
def test_d_wrong_model_fails_at_the_check(monkeypatch, code):
    def chat(r):
        raise _http_error(code, {"error": {"message": "model not found"}})
    fake, calls = _router(chat)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    health, used = _health.check_with_model(OPENAI_EP, "good")
    assert not health.ok and health.status == "model"


def test_d_model_the_address_does_not_serve_fails(monkeypatch):
    # listed model falls back; an endpoint that refuses it on the call still fails
    def chat(r):
        raise _http_error(404, [{"error": {"message": "models/zzz is not found"}}])
    fake, calls = _router(chat)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    health, _ = _health.check_with_model(OPENAI_EP, "zzz")
    assert not health.ok
    assert "is not found" in health.detail      # list-wrapped body read (g)


def test_d_bad_key_fails_at_the_check(monkeypatch):
    def chat(r):
        raise _http_error(401, {"error": {"message": "bad key"}})
    fake, calls = _router(chat)
    monkeypatch.setattr("urllib.request.urlopen", fake)
    health, _ = _health.check_with_model(OPENAI_EP, "good")
    assert not health.ok and health.status == "auth"


def test_d_unreachable_endpoint_makes_no_chat_call(monkeypatch):
    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.URLError("down")
    monkeypatch.setattr("urllib.request.urlopen", fake)
    health, _ = _health.check_with_model(OPENAI_EP, "good")
    assert not health.ok and all(c.endswith("/models") for c in calls)


def test_d_optional_ai_step_still_only_warns():
    from types import SimpleNamespace
    skill = SimpleNamespace(requires=SimpleNamespace(llm=True, llm_precheck="warn"))
    assert _generic.llm_precheck_mode(skill) == "warn"
    skill2 = SimpleNamespace(requires=SimpleNamespace(llm=True, llm_precheck="stop"))
    assert _generic.llm_precheck_mode(skill2) == "stop"


# ----------------------------------------------- (e) listed names / default

def test_e_gemini_listed_names_are_bare_and_default_stays_selected(monkeypatch):
    fake, _ = _router(lambda r: _Resp({}), models=("models/gemini-2.5-flash", "models/gemini-2.5-pro"))
    monkeypatch.setattr("urllib.request.urlopen", fake)
    ep = {"provider": "openai_compatible", "base_url": "http://g/v1beta/openai",
          "default_model": "gemini-2.5-pro"}
    res = _health.check(ep)
    assert res.models == ("gemini-2.5-flash", "gemini-2.5-pro")
    choices = _health._choices_from(res, ep)
    monkeypatch.setattr(_generic._config, "load_portable_config",
                        lambda: {"active_endpoint": "g", "endpoints": {"g": ep}})
    assert _generic._default_model_value(choices) == "gemini-2.5-pro"
    # a default written with the prefix still selects the bare entry
    ep2 = dict(ep, default_model="models/gemini-2.5-pro")
    monkeypatch.setattr(_generic._config, "load_portable_config",
                        lambda: {"active_endpoint": "g", "endpoints": {"g": ep2}})
    assert _generic._default_model_value(choices) == "gemini-2.5-pro"


def test_e_ollama_and_groq_names_unchanged(monkeypatch):
    fake, _ = _router(lambda r: _Resp({}), models=("llama-3.3-70b-versatile", "openai/gpt-oss-20b"))
    monkeypatch.setattr("urllib.request.urlopen", fake)
    res = _health.check({"provider": "openai_compatible", "base_url": "http://groq/openai/v1"})
    assert res.models == ("llama-3.3-70b-versatile", "openai/gpt-oss-20b")
    assert _health.bare_model_name("qwen2.5:7b") == "qwen2.5:7b"
    assert _health.bare_model_name("library/model:latest") == "library/model:latest"


def test_e_run_never_sends_an_unlisted_model(monkeypatch):
    fake, calls = _router(lambda r: _Resp({}), models=("good", "other"))
    monkeypatch.setattr("urllib.request.urlopen", fake)
    health, used = _health.check_with_model(OPENAI_EP, "from-the-old-endpoint")
    assert used in ("good", "other") and calls["sent"]["model"] == used
    assert calls["sent"]["model"] != "from-the-old-endpoint"


# --------------------------------------------------- (f) endpoint switching

def test_f_cache_follows_the_active_endpoint(monkeypatch):
    cfg = {"active_endpoint": "A", "endpoints": {
        "A": {"provider": "ollama", "base_url": "http://a"},
        "B": {"provider": "openai_compatible", "base_url": "http://b/v1"}}}
    monkeypatch.setattr(_generic._config, "load_portable_config", lambda: cfg)
    lists = {"http://a": [("a1", "a1")], "http://b/v1": [("b1", "b1")]}
    monkeypatch.setattr(_generic._health, "get_model_choices",
                        lambda ep: lists[ep["base_url"]])
    assert _generic._refresh_models(use_cache=True) == [("a1", "a1")]
    cfg["active_endpoint"] = "B"          # switched WITHOUT any explicit invalidation
    assert _generic._refresh_models(use_cache=True) == [("b1", "b1")]
    assert ("a1", "a1") not in _generic._refresh_models(use_cache=True)


def test_f_switch_helper_clears_the_cached_list(monkeypatch):
    cfg = {"active_endpoint": "A", "endpoints": {
        "A": {"provider": "ollama", "base_url": "http://a"},
        "B": {"provider": "ollama", "base_url": "http://b"}}}
    monkeypatch.setattr(_generic._config, "load_portable_config", lambda: cfg)
    monkeypatch.setattr(settings._config, "load_portable_config", lambda: cfg)
    monkeypatch.setattr(settings._config, "write_portable_config", lambda c: None)
    monkeypatch.setattr(_generic._health, "get_model_choices", lambda ep: [(ep["base_url"], ep["base_url"])])
    _generic._refresh_models(use_cache=True)
    assert _generic._choices_cache is not None
    settings._switch_active("B")
    assert _generic._choices_cache is None
    # a failed switch (unknown name) does not clear it
    _generic._refresh_models(use_cache=True)
    settings._switch_active("nope")
    assert _generic._choices_cache is not None


# ------------------------------------------------------ (g) error body shapes

def _msg(body):
    err = _http_error(400, body)
    return m._provider_message(err)


def test_g_object_body_reads_as_before():
    assert _msg({"error": {"message": "boom"}}) == "boom"
    assert _msg({"message": "plain"}) == "plain"
    assert _msg({"error": "text error"}) == "text error"


def test_g_list_wrapped_body_gives_the_same_message():
    assert _msg([{"error": {"message": "boom"}}]) == "boom"
    assert le.message_from_body([{"error": {"message": "boom"}}]) == le.message_from_body({"error": {"message": "boom"}})


def test_g_empty_list_or_non_json_does_not_crash_and_falls_back_to_status():
    assert _msg([]) == ""
    assert _msg(b"<html>gateway</html>") == "<html>gateway</html>"
    assert _msg(b"") == ""
    # the shown reason falls back to the HTTP status
    def fake_urlopen(req, timeout=None):
        raise _http_error(500, [])
    import unittest.mock as um
    with um.patch("urllib.request.urlopen", fake_urlopen):
        assert m._openai_compatible_chat("http://x", "mdl", "s", "u") is None
    assert m._LLM_LAST_ERROR.get("reason", "").endswith("HTTP 500") or "HTTP 500" in m._LLM_LAST_ERROR.get("reason", "")


def test_g_classify_reads_a_list_body():
    exc = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b"[]"))
    exc.body = [{"error": {"message": "model not found"}}]
    assert le.message_from_body(exc.body) == "model not found"
