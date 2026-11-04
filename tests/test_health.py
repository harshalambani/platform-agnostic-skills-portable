"""
tests/test_health.py — Unit tests for ui/_health.py (Phase 6, task 6-3).

Tests cover:
  - check() with mocked HTTP for ollama (success + model list extraction)
  - check() with mocked HTTP for openai_compatible (success + model list)
  - Empty base_url → unreachable
  - Unknown provider → unreachable
  - URLError → unreachable with reason
  - Timeout → slow
  - Generic exception → unreachable
  - _get_json() basic parsing

Run with:
    cd src && python -m pytest ../tests/test_health.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import error as urlerror

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ui._health import check, HealthResult, _get_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_urlopen(body_dict: dict, status: int = 200):
    """Return a context-manager mock that urlopen can use."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(body_dict).encode("utf-8")
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


OLLAMA_ENDPOINT = {
    "provider": "ollama",
    "base_url": "http://localhost:11434",
}

OPENAI_ENDPOINT = {
    "provider": "openai_compatible",
    "base_url": "https://api.example.com/v1",
}


# ---------------------------------------------------------------------------
# check() — ollama
# ---------------------------------------------------------------------------

class TestCheckOllama:
    def test_success_with_models(self):
        body = {"models": [{"name": "gemma4"}, {"name": "llama3"}]}
        with patch("ui._health.request.urlopen", return_value=_mock_urlopen(body)):
            r = check(OLLAMA_ENDPOINT)
        assert r.ok is True
        assert r.status == "ok"
        assert "gemma4" in r.models
        assert "llama3" in r.models
        assert "2 model" in r.detail

    def test_success_empty_model_list(self):
        body = {"models": []}
        with patch("ui._health.request.urlopen", return_value=_mock_urlopen(body)):
            r = check(OLLAMA_ENDPOINT)
        assert r.ok is True
        assert r.models == ()
        assert "0 model" in r.detail


# ---------------------------------------------------------------------------
# check() — openai_compatible
# ---------------------------------------------------------------------------

class TestCheckOpenAI:
    def test_success_with_models(self):
        body = {"data": [{"id": "gpt-4o"}, {"id": "gpt-3.5-turbo"}]}
        with patch("ui._health.request.urlopen", return_value=_mock_urlopen(body)):
            r = check(OPENAI_ENDPOINT)
        assert r.ok is True
        assert "gpt-4o" in r.models
        assert len(r.models) == 2

    def test_url_uses_models_not_api_tags(self):
        """OpenAI-compatible endpoints should hit /models, not /api/tags."""
        body = {"data": []}
        with patch("ui._health.request.urlopen", return_value=_mock_urlopen(body)) as mock_open:
            check(OPENAI_ENDPOINT)
            call_args = mock_open.call_args
            req = call_args[0][0]
            assert req.full_url == "https://api.example.com/v1/models"


# ---------------------------------------------------------------------------
# check() — error cases
# ---------------------------------------------------------------------------

class TestCheckErrors:
    def test_empty_base_url(self):
        r = check({"provider": "ollama", "base_url": ""})
        assert r.ok is False
        assert r.status == "unreachable"
        assert "Empty" in r.detail

    def test_missing_base_url(self):
        r = check({"provider": "ollama"})
        assert r.ok is False
        assert r.status == "unreachable"

    def test_unknown_provider(self):
        r = check({"provider": "anthropic", "base_url": "http://x"})
        assert r.ok is False
        assert r.status == "unreachable"
        assert "Unknown provider" in r.detail

    def test_url_error(self):
        exc = urlerror.URLError(reason="Connection refused")
        with patch("ui._health.request.urlopen", side_effect=exc):
            r = check(OLLAMA_ENDPOINT)
        assert r.ok is False
        assert r.status == "unreachable"
        assert "Connection refused" in r.detail

    def test_timeout(self):
        with patch("ui._health.request.urlopen", side_effect=TimeoutError()):
            r = check(OLLAMA_ENDPOINT)
        assert r.ok is False
        assert r.status == "slow"
        assert "Timed out" in r.detail

    def test_generic_exception(self):
        with patch("ui._health.request.urlopen", side_effect=OSError("weird")):
            r = check(OLLAMA_ENDPOINT)
        assert r.ok is False
        assert r.status == "unreachable"
        assert "OSError" in r.detail

    def test_trailing_slash_stripped(self):
        """base_url with trailing slash should still produce a valid URL."""
        ep = {"provider": "ollama", "base_url": "http://localhost:11434/"}
        body = {"models": []}
        with patch("ui._health.request.urlopen", return_value=_mock_urlopen(body)) as mock_open:
            check(ep)
            req = mock_open.call_args[0][0]
            assert req.full_url == "http://localhost:11434/api/tags"


# ---------------------------------------------------------------------------
# HealthResult dataclass
# ---------------------------------------------------------------------------

class TestHealthResult:
    def test_frozen(self):
        r = HealthResult(ok=True, status="ok", detail="fine")
        with pytest.raises(AttributeError):
            r.ok = False  # type: ignore[misc]

    def test_default_models(self):
        r = HealthResult(ok=True, status="ok", detail="x")
        assert r.models == ()


# ---------------------------------------------------------------------------
# _get_json()
# ---------------------------------------------------------------------------

class TestGetJson:
    def test_parses_json(self):
        body = {"hello": "world"}
        with patch("ui._health.request.urlopen", return_value=_mock_urlopen(body)):
            result = _get_json("http://example.com/test")
        assert result == {"hello": "world"}
