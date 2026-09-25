"""
tests/skill_gnucash_account_mapper/conftest.py --

MAP-06: a reusable, offline ScriptedLLM harness for the mapper's LLM-fallback
tests, built on the existing stubbing pattern already used by
test_deposit_withdrawal_classification.py (monkeypatch.setattr(agent,
"_llm_chat", ...) / monkeypatch.setattr(agent, "_resolve_llm_endpoint_config",
...)) and test_llm_provider_dispatch.py (monkeypatch.setattr("urllib.request.
urlopen", ...) for the lower-level dispatch tests).

`scripted_llm` replaces agent._llm_chat with a fake that:
  - returns queued replies in order (one per non-"ping" call);
  - always answers "OK" to the warm-up "ping" call, without consuming a
    queued reply;
  - records every call as a (system, user) prompt pair, in order, on
    `.calls` (also exposes `.prompts` as the `user` half only, and
    `.non_warmup_calls` / `.non_warmup_prompts` with the "ping" warm-up
    filtered out, since most tests only care about the real classification
    prompts);
  - raises AssertionError if a call arrives after the queue is exhausted,
    so a test that expects N calls and gets N+1 fails loudly instead of
    silently returning None/StopIteration data;
  - also stubs agent._resolve_llm_endpoint_config to a fixed, synthetic
    (provider, base_url, model, api_key, temperature) tuple, since real
    config resolution is out of scope for these tests.

It also blocks urllib.request.urlopen for the duration of the test, so any
code path that bypasses the _llm_chat stub and tries to make a real network
call fails immediately with a clear error, rather than hanging or reaching
a real endpoint.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402


class ScriptedLLM:
    """Queued-reply fake for agent._llm_chat, with call recording."""

    def __init__(self, replies: Optional[List[str]] = None):
        self._queue: List[str] = list(replies) if replies else []
        self.calls: List[Tuple[str, str]] = []  # (system, user), in order

    def queue(self, *replies: str) -> "ScriptedLLM":
        """Add more scripted replies (fluent, for readability in tests)."""
        self._queue.extend(replies)
        return self

    def __call__(self, provider, base_url, model, system, user, api_key=None, timeout=None):
        self.calls.append((system, user))
        if user == "ping":
            return "OK"  # warm-up reply -- never consumes a queued reply
        if not self._queue:
            raise AssertionError(
                f"ScriptedLLM: no scripted reply left for call #{len(self.calls)} "
                f"(user prompt: {user[:80]!r}...)"
            )
        return self._queue.pop(0)

    @property
    def prompts(self) -> List[str]:
        return [user for _system, user in self.calls]

    @property
    def non_warmup_calls(self) -> List[Tuple[str, str]]:
        return [(s, u) for s, u in self.calls if u != "ping"]

    @property
    def non_warmup_prompts(self) -> List[str]:
        return [u for _s, u in self.non_warmup_calls]

    @property
    def call_count(self) -> int:
        """Number of real (non-warm-up) calls made so far."""
        return len(self.non_warmup_calls)


def _fake_resolve_llm_endpoint_config(config_path, model_override=None):
    return "ollama", "http://fake-ollama.invalid:11434", "fake-scripted-model", None, 0.0


@pytest.fixture
def scripted_llm(monkeypatch):
    """Replace agent._llm_chat with a ScriptedLLM, stub config resolution,
    silence progress output, and block any real network access for the
    duration of the test.

    Usage:
        def test_something(scripted_llm):
            scripted_llm.queue("Expenses:Food and Dining")
            result = agent.llm_fallback_mapping(..., config_path="fake.yaml")
            assert scripted_llm.call_count == 1
    """
    fake = ScriptedLLM()

    def _blocked_urlopen(*args, **kwargs):
        raise AssertionError(
            "scripted_llm: a real urllib.request.urlopen call was attempted -- "
            "the mapper's LLM path must go through the ScriptedLLM stub, never "
            "the network, in tests."
        )

    monkeypatch.setattr(agent, "_llm_chat", fake)
    monkeypatch.setattr(agent, "_resolve_llm_endpoint_config", _fake_resolve_llm_endpoint_config)
    monkeypatch.setattr(agent, "_emit_mapper_progress", lambda msg: None)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked_urlopen)

    return fake


def make_historical_mappings(entries: Dict[str, List[str]], frequency: int = 1) -> List[Dict]:
    """Build a historical_mappings list (the shape llm_fallback_mapping /
    _build_historical_prompt / _retry_with_focused_prompt expect) from a
    simple {account: [description, ...]} dict, all synthetic data.
    """
    out = []
    for account, descriptions in entries.items():
        for desc in descriptions:
            out.append({"account": account, "description": desc, "frequency": frequency})
    return out
