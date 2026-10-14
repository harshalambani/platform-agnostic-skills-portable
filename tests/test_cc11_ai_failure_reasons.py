"""CC-11: a failed AI call records the HTTP status and the provider's message, never the
key; a stopped AI pass is the first thing the result says and is never a plain success.
Synthetic data only; no network (urlopen is patched)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src"), str(ROOT / "src" / "agents"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_gnucash_account_mapper import agent as m          # noqa: E402

KEY = "sk-synthetic-secret-0123456789abcdef"
BASE = "https://llm.example.invalid/v1"


def _http(code, message="", echo_key=False):
    body = {"error": {"message": message + (f" (key {KEY})" if echo_key else "")}} if message or echo_key else {}
    return HTTPError(BASE + "/chat/completions", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


def _call(exc, model="synthetic-model", timeout=7):
    m._LLM_LAST_ERROR.clear()
    with patch("urllib.request.urlopen", side_effect=exc):
        out = m._openai_compatible_chat(BASE, model, "s", "u", api_key=KEY, timeout=timeout)
    assert out is None
    return m._take_llm_error()


def test_401_and_403_read_as_a_refused_key_with_the_status_and_provider_message():
    for code in (401, 403):
        err = _call(_http(code, "Invalid API key provided"))
        assert err["fatal"] is True
        assert "refused the API key" in err["reason"]
        assert f"HTTP {code}" in err["reason"] and "Invalid API key provided" in err["reason"]


def test_a_401_never_reads_as_did_not_answer():                                  # NEGATIVE
    err = _call(_http(401, "bad key"))
    assert "did not answer" not in err["reason"].lower() and "no answer" not in err["reason"].lower()


def test_the_key_never_appears_in_a_reason_even_if_the_provider_echoes_it():     # NEGATIVE
    for exc in (_http(401, "nope", echo_key=True), _http(500, "boom", echo_key=True),
                _http(404, "model not found", echo_key=True)):
        err = _call(exc)
        assert KEY not in err["reason"] and "sk-synthetic" not in err["reason"]


def test_model_not_found_has_its_own_wording():
    err = _call(_http(404, "The model `nope-1` does not exist"), model="nope-1")
    assert "does not offer model nope-1" in err["reason"] and err["fatal"] is True
    assert "refused the API key" not in err["reason"]


def test_a_timeout_has_its_own_wording_and_is_not_fatal():
    err = _call(URLError(TimeoutError("timed out")), timeout=9)
    assert "no answer from llm.example.invalid within 9s" in err["reason"] and err["fatal"] is False
    assert "refused" not in err["reason"]


def test_a_server_error_is_not_fatal_and_names_the_status():                       # NEGATIVE
    err = _call(_http(503, "overloaded"))
    assert err["fatal"] is False and "HTTP 503" in err["reason"] and "overloaded" in err["reason"]


def _cfg(tmp_path):
    p = tmp_path / "llm.yaml"
    p.write_text(yaml.safe_dump({"provider": "openai_compatible",
                                 "openai_compatible": {"base_url": BASE, "default_model": "synthetic-model",
                                                       "api_key": KEY}}), encoding="utf-8")
    return str(p)


def _rows():
    return [{"row": 1, "description": "SYNTHETIC SHOP", "withdrawal": "100", "deposit": ""}]


def test_a_401_on_the_warm_up_stops_the_pass_at_once_with_the_real_reason(tmp_path, capsys):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise _http(401, "Invalid API key")
    with patch("urllib.request.urlopen", side_effect=boom):
        out = m.llm_fallback_mapping(_rows(), ["Expenses:Food"], [], _cfg(tmp_path))
    assert out == {}
    st = m._LLM_RUN_STATUS
    assert st["stopped"] is True and "refused the API key" in st["reason"] and "HTTP 401" in st["reason"]
    assert "did not answer" not in st["reason"]
    assert len(calls) == 1                      # no point retrying a refused key
    log = capsys.readouterr().out
    assert KEY not in log and "refused the API key" in log


def test_the_key_is_not_in_the_run_status_or_the_log_for_a_generic_failure(tmp_path, capsys):   # NEGATIVE
    with patch("urllib.request.urlopen", side_effect=OSError(f"connection reset for {KEY}")):
        m.llm_fallback_mapping(_rows(), ["Expenses:Food"], [], _cfg(tmp_path))
    assert KEY not in json.dumps(m._LLM_RUN_STATUS, default=str)
    assert KEY not in capsys.readouterr().out
