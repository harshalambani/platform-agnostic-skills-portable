"""
tests/skill_gnucash_account_mapper/test_llm_provider_dispatch.py --
regression guard for MAP-05: the mapper's LLM fallback only spoke Ollama's
/api/chat protocol, even when the configured endpoint was openai_compatible.

Background (src/agents/skill_gnucash_account_mapper/agent.py):

  The mapper is handed a MATERIALIZED LEGACY config produced by
  ui/_config.py's materialize_legacy_config() / _legacy_from_endpoint(),
  which has the shape:

      {"provider": <provider>,
       <provider>: {base_url, default_model, temperature, [api_key]},
       "output_dir": "./outputs"}

  So the endpoint block is named after cfg["provider"] -- "ollama" OR
  "openai_compatible" -- not always "ollama". The old
  ``_resolve_ollama_config()`` did ``cfg.get("ollama") or {}``
  unconditionally, ignoring cfg["provider"] entirely. For an
  openai_compatible endpoint this meant:
    - base_url silently fell back to "http://localhost:11434"
    - model silently fell back to "gemma4:12b"
    - _ollama_chat() posted Ollama-format requests to POST {base_url}/api/chat
      with no Authorization header -- to a local port that was never the
      configured endpoint, producing nothing.

  The fix adds explicit provider dispatch (_resolve_llm_endpoint_config +
  _llm_chat) that reads cfg["provider"] and the correspondingly-named
  block, and for openai_compatible posts to {base_url}/chat/completions
  with an Authorization: Bearer <api_key> header and the OpenAI
  request/response schema -- with NO hard-coded localhost/model fallback,
  and a hard failure (ValueError, surfaced via _emit_mapper_progress) for
  an unknown/missing provider.

All configs and HTTP calls in this file are synthetic/monkeypatched.
No real network calls are made.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib import error as urllib_error

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402


# ---------------------------------------------------------------------------
# Config fixtures -- exact shape of ui/_config.py's _legacy_from_endpoint()
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, provider: str, **endpoint_overrides) -> Path:
    endpoint = {
        "base_url": "https://synthetic-openai-proxy.example.invalid/v1",
        "default_model": "synthetic-model-7b",
        "temperature": 0.0,
    }
    if provider == "openai_compatible":
        endpoint["api_key"] = "sk-synthetic-test-key-000111"
    endpoint.update(endpoint_overrides)

    cfg = {
        "provider": provider,
        provider: endpoint,
        "output_dir": "./outputs",
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(cfg), encoding="utf-8")
    return cfg_path


# ---------------------------------------------------------------------------
# _resolve_llm_endpoint_config -- provider-aware resolution
# ---------------------------------------------------------------------------

def test_openai_compatible_config_resolves_its_own_base_url_and_model(tmp_path):
    cfg_path = _write_config(tmp_path, "openai_compatible",
                              base_url="https://real-endpoint.example.invalid",
                              default_model="gpt-oss-20b")
    provider, base_url, model, api_key, _temp = agent._resolve_llm_endpoint_config(str(cfg_path))
    assert provider == "openai_compatible"
    assert base_url == "https://real-endpoint.example.invalid"
    assert model == "gpt-oss-20b"
    assert api_key == "sk-synthetic-test-key-000111"


def test_ollama_config_still_resolves_correctly_no_regression(tmp_path):
    cfg_path = _write_config(tmp_path, "ollama",
                              base_url="http://real-ollama-host:11434",
                              default_model="gemma3:12b")
    provider, base_url, model, api_key, _temp = agent._resolve_llm_endpoint_config(str(cfg_path))
    assert provider == "ollama"
    assert base_url == "http://real-ollama-host:11434"
    assert model == "gemma3:12b"
    assert api_key is None


# ── Negative tests: the actual MAP-05 defect ────────────────────────────

def test_openai_compatible_base_url_never_falls_back_to_localhost_ollama_port(tmp_path):
    """The old resolver read cfg['ollama'] unconditionally; for an
    openai_compatible config that key doesn't exist, so it silently fell
    back to 'http://localhost:11434'. Must never happen now."""
    cfg_path = _write_config(tmp_path, "openai_compatible",
                              base_url="https://api.mycompany-llm.example.invalid")
    provider, base_url, _model, _api_key, _temp = agent._resolve_llm_endpoint_config(str(cfg_path))
    assert base_url != "http://localhost:11434"
    assert base_url == "https://api.mycompany-llm.example.invalid"


def test_openai_compatible_model_never_silently_becomes_gemma4_12b(tmp_path):
    """The old resolver's model fallback was the hard-coded string
    'gemma4:12b' whenever ep.get('default_model') was falsy/missing under
    the wrong key. The configured model must be used instead."""
    cfg_path = _write_config(tmp_path, "openai_compatible", default_model="my-actual-deployed-model")
    _provider, _base_url, model, _api_key, _temp = agent._resolve_llm_endpoint_config(str(cfg_path))
    assert model != "gemma4:12b"
    assert model == "my-actual-deployed-model"


def test_unknown_provider_errors_rather_than_falling_back_to_localhost(tmp_path):
    """A config with an unrecognised/missing provider must fail loud
    (ValueError) rather than defaulting to Ollama-at-localhost."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump({"provider": "some_future_provider",
                                    "some_future_provider": {"base_url": "https://x.invalid"}}),
                         encoding="utf-8")
    with pytest.raises(ValueError):
        agent._resolve_llm_endpoint_config(str(cfg_path))


def test_missing_provider_key_errors_rather_than_falling_back_to_localhost(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump({"ollama": {"base_url": "http://localhost:11434"}}),
                         encoding="utf-8")
    with pytest.raises(ValueError):
        agent._resolve_llm_endpoint_config(str(cfg_path))


# ---------------------------------------------------------------------------
# _llm_chat dispatch -- HTTP request shape per provider
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_openai_compatible_request_never_hits_api_chat_endpoint(monkeypatch):
    """An openai_compatible call must POST to /chat/completions, never
    Ollama's /api/chat path."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"choices": [{"message": {"content": "Expenses:Misc"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    reply = agent._llm_chat(
        "openai_compatible", "https://api.example.invalid", "some-model",
        "system prompt", "user prompt", api_key="sk-test-123",
    )

    assert reply == "Expenses:Misc"
    assert captured["url"] == "https://api.example.invalid/chat/completions"
    assert "/api/chat" not in captured["url"]


def test_openai_compatible_request_carries_bearer_authorization_header(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeResponse({"choices": [{"message": {"content": "SKIP"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    agent._llm_chat(
        "openai_compatible", "https://api.example.invalid", "some-model",
        "system prompt", "user prompt", api_key="sk-super-secret-token",
    )

    assert captured["headers"].get("authorization") == "Bearer sk-super-secret-token"


def test_openai_compatible_without_api_key_sends_no_authorization_header(monkeypatch):
    """No api_key configured -> no Authorization header at all, rather than
    'Bearer None' or similar."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeResponse({"choices": [{"message": {"content": "SKIP"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    agent._llm_chat(
        "openai_compatible", "https://api.example.invalid", "some-model",
        "system prompt", "user prompt", api_key=None,
    )

    assert "authorization" not in captured["headers"]


def test_ollama_dispatch_still_uses_api_chat_no_regression(monkeypatch):
    """Ollama provider must still hit /api/chat with the Ollama payload
    shape -- the fix must not have broken the existing behaviour."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse({"message": {"content": "Expenses:Misc"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    reply = agent._llm_chat(
        "ollama", "http://localhost:11434", "gemma3:12b",
        "system prompt", "user prompt",
    )

    assert reply == "Expenses:Misc"
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert "messages" in captured["body"]
    assert captured["body"]["model"] == "gemma3:12b"


def test_unknown_provider_chat_dispatch_raises_rather_than_defaulting_to_ollama(monkeypatch):
    """_llm_chat itself must refuse an unrecognised provider rather than
    silently treating it as Ollama."""
    def fail_if_called(*a, **kw):
        raise AssertionError("urlopen should never be called for an unknown provider")

    monkeypatch.setattr("urllib.request.urlopen", fail_if_called)

    with pytest.raises(ValueError):
        agent._llm_chat("carrier_pigeon", "https://x.invalid", "model", "sys", "user")


# ---------------------------------------------------------------------------
# End-to-end through llm_fallback_mapping() -- config error surfaces loudly
# ---------------------------------------------------------------------------

def test_llm_fallback_mapping_surfaces_config_error_instead_of_guessing(tmp_path, monkeypatch):
    """When the config can't be resolved (unknown provider), the whole LLM
    fallback pass must bail out cleanly (empty result) rather than ever
    reaching a network call built from guessed defaults."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump({"provider": "totally_unknown"}), encoding="utf-8")

    def fail_if_called(*a, **kw):
        raise AssertionError("urlopen should never be called when config resolution fails")

    monkeypatch.setattr("urllib.request.urlopen", fail_if_called)
    messages = []
    monkeypatch.setattr(agent, "_emit_mapper_progress", lambda msg: messages.append(msg))

    result = agent.llm_fallback_mapping(
        unmatched_rows=[{"row": 1, "description": "SOME TXN", "deposit": "10", "withdrawal": "0"}],
        account_tree=["Expenses:Misc"],
        example_mappings=[],
        config_path=str(cfg_path),
    )

    assert result == {}
    assert any("LLM config error" in m for m in messages)
