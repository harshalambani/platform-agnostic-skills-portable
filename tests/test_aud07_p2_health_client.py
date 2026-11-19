"""AUD-07 P2: the health check sends its requests through the AI client.

Every status and message the health check shows must read as before. Synthetic
data only; a local fake service, or urlopen replaced.
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error as ue
from pathlib import Path
from urllib import request

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.test_aud07_llm_client import _Fake  # noqa: E402
from ui import _config, _health  # noqa: E402

KEY = "sk-synthetic-HEALTHKEY123456"


@pytest.fixture()
def fake():
    f = _Fake()
    yield f
    f.stop()


@pytest.fixture(autouse=True)
def _plain_key(monkeypatch):
    monkeypatch.setattr(_config, "decrypt_api_key", lambda raw: raw)


def _ep(f, provider="openai_compatible", **kw):
    base = f.url + ("/v1" if provider == "openai_compatible" else "")
    return {"provider": provider, "base_url": base, **kw}


def _answer(monkeypatch, status=200, body=b"{}", exc=None, headers=None):
    """Replace the network with one canned answer; returns the requests seen."""
    seen = []

    def fake_open(req, timeout=None):
        seen.append({"url": req.full_url, "auth": req.get_header("Authorization"),
                     "timeout": timeout, "data": req.data})
        if exc is not None:
            raise exc
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        if status == 200:
            class R:
                def __enter__(self): return self
                def __exit__(self, *a): return False
                def read(self): return data
            return R()
        raise ue.HTTPError(req.full_url, status, "Reason Phrase", headers or {}, io.BytesIO(data))
    monkeypatch.setattr(request, "urlopen", fake_open)
    return seen


# ------------------------------------------------------------ the list check

def test_list_check_reads_models_through_the_client(fake):
    fake.plan = [(200, {"data": [{"id": "models/gemini-x"}, {"id": "gpt-y"}, "junk"]}, {}, 0)]
    res = _health._check_uncached(_ep(fake))
    assert res.ok and res.status == "ok" and res.models == ("gemini-x", "gpt-y")
    assert res.detail == "OK — 2 model(s)."
    assert fake.requests[0]["path"] == "/v1/models"


def test_401_from_models_keeps_its_wording_and_hides_the_key(monkeypatch):     # NEGATIVE
    seen = _answer(monkeypatch, 401, {"error": {"message": f"bad key {KEY}"}})
    res = _health._check_uncached({"provider": "openai_compatible",
                                   "base_url": "https://api.example.com/v1", "api_key": KEY})
    assert not res.ok and res.status == "unreachable"
    assert res.detail == "HTTP 401: Reason Phrase"
    assert KEY not in res.detail
    assert seen[0]["auth"] == f"Bearer {KEY}" and seen[0]["timeout"] == 2.0


def test_list_check_failure_wordings_are_unchanged(monkeypatch):
    ep = {"provider": "ollama", "base_url": "http://127.0.0.1:11434"}
    _answer(monkeypatch, exc=ue.URLError("Connection refused"))
    r = _health._check_uncached(ep)
    assert (r.status, r.detail) == ("unreachable", "URLError: Connection refused")
    _answer(monkeypatch, exc=TimeoutError())
    r = _health._check_uncached(ep)
    assert (r.status, r.detail) == ("slow", "Timed out after 2s.")
    _answer(monkeypatch, exc=OSError("weird"))
    r = _health._check_uncached(ep)
    assert (r.status, r.detail) == ("unreachable", "OSError: weird")
    _answer(monkeypatch, body=b"not json")
    r = _health._check_uncached(ep)
    assert r.status == "unreachable" and r.detail.startswith("JSONDecodeError: ")


def test_key_never_appears_in_a_list_check_line_or_log(monkeypatch, caplog):   # NEGATIVE
    caplog.set_level("DEBUG")
    ep = {"provider": "openai_compatible", "base_url": "https://api.example.com/v1", "api_key": KEY}
    for exc in (ue.URLError(f"cannot reach with {KEY}"), OSError(f"reset {KEY}"),
                ue.HTTPError("https://x", 500, f"oops {KEY}", {}, io.BytesIO(b"{}"))):
        _answer(monkeypatch, exc=exc)
        assert KEY not in _health._check_uncached(ep).detail
    assert KEY not in caplog.text


def test_placeholder_keys_send_nothing(fake):
    fake.plan = [(200, {"data": []}, {}, 0)]
    for k in ("", "not-needed"):
        _health._check_uncached(_ep(fake, api_key=k))
        assert fake.requests[-1]["auth"] is None                              # NEGATIVE
    fake.plan = [(200, {"models": []}, {}, 0)]
    _health._check_uncached(_ep(fake, "ollama", api_key=KEY))
    assert fake.requests[-1]["auth"] is None                                  # ollama never gets a key


# ------------------------------------------------------------ the model probe

@pytest.mark.parametrize("provider", ["ollama", "openai_compatible"])
def test_probe_answers(fake, provider):
    fake.plan = [(200, {"anything": 1}, {}, 0)]
    r = _health.probe_model(_ep(fake, provider), "models/m1")
    assert (r.ok, r.status, r.detail) == (True, "ok", "Model 'm1' answered.")
    assert fake.requests[0]["body"]["messages"] == [{"role": "user", "content": "ping"}]
    assert fake.requests[0]["body"]["model"] == "m1"


def test_probe_a_2xx_that_is_not_json_still_answers(fake):
    fake.plan = [(200, b"plain words", {}, 0)]
    assert _health.probe_model(_ep(fake), "m").detail == "Model 'm' answered."


@pytest.mark.parametrize("status,body,expect", [
    (401, {"error": {"message": "nope"}}, (False, "auth", "HTTP 401: the key was refused. nope")),
    (403, {}, (False, "auth", "HTTP 403: the key was refused.")),
    (404, {"error": {"message": "gone"}}, (False, "model", "HTTP 404: model 'm' was refused by this address. gone")),
    (400, {"error": {"message": "bad model name"}}, (False, "model", "HTTP 400: model 'm' was refused by this address. bad model name")),
    (400, {"error": {"message": "max_tokens is too large"}}, (True, "ok", "Model 'm' reachable (HTTP 400 about the token limit).")),
    (429, {"error": {"message": "slow down"}}, (True, "ok", "Model 'm' reachable (HTTP 429).")),
    (429, {"error": {"message": "insufficient_quota"}}, (True, "ok", "Model 'm' reachable (HTTP 429).")),
    (500, {"error": {"message": "boom"}}, (False, "model", "HTTP 500: boom")),
])
def test_probe_http_answers_read_as_before(fake, status, body, expect):
    fake.plan = [(status, body, {}, 0)]
    r = _health.probe_model(_ep(fake), "m")
    assert (r.ok, r.status, r.detail) == expect


def test_probe_500_without_a_message_uses_the_http_reason(fake):
    fake.plan = [(500, b"<html>", {}, 0)]
    r = _health.probe_model(_ep(fake), "m")
    assert r.status == "model" and r.detail.startswith("HTTP 500: ")


def test_probe_401_is_key_refused_not_unreachable(fake):                       # NEGATIVE
    fake.plan = [(401, {"error": {"message": "nope"}}, {}, 0)]
    r = _health.probe_model(_ep(fake), "m")
    assert r.status == "auth" and "the key was refused" in r.detail and r.status != "unreachable"


def test_probe_slow_and_unreachable_wordings(monkeypatch):
    ep = {"provider": "ollama", "base_url": "http://127.0.0.1:11434"}
    _answer(monkeypatch, exc=TimeoutError())
    r = _health.probe_model(ep, "m", timeout=7)
    assert (r.status, r.detail) == ("slow", "Model 'm' did not answer within 7s.")
    _answer(monkeypatch, exc=ue.URLError("refused"))
    r = _health.probe_model(ep, "m")
    assert (r.status, r.detail) == ("unreachable", "URLError: refused")
    _answer(monkeypatch, exc=OSError("weird"))
    assert _health.probe_model(ep, "m").detail == "OSError: weird"


def test_probe_default_timeouts_are_120_and_20(monkeypatch):
    seen = _answer(monkeypatch)
    _health.probe_model({"provider": "ollama", "base_url": "http://127.0.0.1:11434"}, "m")
    _health.probe_model({"provider": "openai_compatible", "base_url": "https://api.example.com/v1"}, "m")
    _health.probe_model({"provider": "openai_compatible", "base_url": "https://api.example.com/v1"}, "m", timeout=5)
    assert [s["timeout"] for s in seen] == [120.0, 20.0, 5]


def test_probe_sends_the_key_only_as_a_header(monkeypatch):
    seen = _answer(monkeypatch)
    _health.probe_model({"provider": "openai_compatible", "base_url": "https://api.example.com/v1",
                         "api_key": KEY}, "m")
    assert seen[0]["auth"] == f"Bearer {KEY}" and KEY.encode() not in seen[0]["data"]


def test_probe_keyless_placeholder_sends_nothing(fake):                        # NEGATIVE
    fake.plan = [(200, {}, {}, 0)]
    _health.probe_model(_ep(fake, api_key="not-needed"), "m")
    assert fake.requests[0]["auth"] is None


def test_probe_key_never_appears_in_a_message_or_log(monkeypatch, caplog):     # NEGATIVE
    caplog.set_level("DEBUG")
    ep = {"provider": "openai_compatible", "base_url": "https://api.example.com/v1", "api_key": KEY}
    for status in (400, 401, 404, 429, 500):
        _answer(monkeypatch, status, {"error": {"message": f"echo {KEY} Bearer {KEY}"}})
        assert KEY not in _health.probe_model(ep, "m").detail
    for exc in (ue.URLError(f"x {KEY}"), OSError(f"x {KEY}")):
        _answer(monkeypatch, exc=exc)
        assert KEY not in _health.probe_model(ep, "m").detail
    assert KEY not in caplog.text


def test_probe_address_refusal_sends_nothing_and_says_why(monkeypatch):        # NEGATIVE
    seen = _answer(monkeypatch)
    r = _health.probe_model({"provider": "openai_compatible", "base_url": "http://192.0.2.7/v1",
                             "api_key": KEY}, "m")
    assert not r.ok and r.status == "unreachable" and "plain http" in r.detail and KEY not in r.detail
    assert seen == []


def test_get_json_still_hands_back_the_original_exception(monkeypatch):
    _answer(monkeypatch, 500, {})
    with pytest.raises(ue.HTTPError):
        _health._get_json("http://127.0.0.1:1/x")


# ------------------------------------------------------------ the frozen build

def test_the_frozen_build_ships_the_whole_agents_folder_so_llm_client_is_found():
    spec = (ROOT / "bundling" / "paskills.spec").read_text(encoding="utf-8")
    assert '(str(SRC_AGENTS), "agents")' in spec          # every agents/*.py is data
    assert (ROOT / "src" / "agents" / "llm_client.py").is_file()
    # its third-party import (yaml) is declared; the rest is the standard library
    assert '"yaml",' in spec
