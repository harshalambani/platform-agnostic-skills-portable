"""
MAP-19 -- the AI fallback must not keep going while the provider or model
keeps failing. Stubbed provider throughout: no network, no sleeping.

  (a) HTTP 429: Retry-After honoured, wait capped.
  (b) N consecutive failed calls stop the pass; the rest stays in Suspense.
  (c) No model-name list: an invalid-answer rate above a threshold over the
      first K answers stops the pass with a warning naming model and rate.
"""
from __future__ import annotations

import csv
import io
import sys
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402

N = m.LLM_MAX_CONSECUTIVE_FAILURES
K = m.LLM_VALIDITY_WINDOW_K
THRESHOLD = m.LLM_INVALID_RATE_STOP
MODEL = "stub-model-zz"

HIST = [{"account": "Expenses:Food", "description": "CAFE ALPHA", "frequency": 5}]


def _rows(n):
    return [{"row": i, "description": f"MISC PAYMENT {i}", "deposit": "0", "withdrawal": "100.00"}
            for i in range(1, n + 1)]


class Provider:
    """Scripted stand-in for _llm_chat. `script(call_no, user)` returns a str,
    None, or raises; the warm-up call ('ping') is call 1."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    def __call__(self, provider, base_url, model, system, user, api_key=None, timeout=None):
        self.calls.append(user)
        return self.script(len(self.calls), user)


@pytest.fixture
def stub(monkeypatch):
    sleeps = []
    monkeypatch.setattr(m, "_llm_sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(m, "_resolve_llm_endpoint_config",
                        lambda c, mo=None: ("ollama", "http://stub.invalid", MODEL, None, 0.0))
    logs = []
    monkeypatch.setattr(m, "_emit_mapper_progress", lambda msg: logs.append(msg))

    def install(script):
        prov = Provider(script)
        monkeypatch.setattr(m, "_llm_chat", prov)
        return prov
    install.sleeps = sleeps
    install.logs = logs
    return install


def _run_pass(n_rows):
    return m.llm_fallback_mapping(
        unmatched_rows=_rows(n_rows), account_tree=[], example_mappings=[],
        config_path="fake.yaml", historical_mappings=HIST)


# ---- (a) 429 ---------------------------------------------------------------

def test_always_429_ends_after_n_attempts(stub):
    def script(n, user):
        raise m._LLMRateLimited(3)
    prov = stub(script)
    result = _run_pass(10)
    assert result == {}
    assert len(prov.calls) == N                       # exactly N attempts, then stop
    assert m._LLM_RUN_STATUS["stopped"] is True
    assert "429" in m._LLM_RUN_STATUS["reason"]


def test_retry_after_is_honoured_and_capped(stub):
    def script(n, user):
        raise m._LLMRateLimited(3 if n == 1 else 9999)
    stub(script)
    _run_pass(3)
    assert stub.sleeps[0] == 3
    assert all(s <= m.LLM_BACKOFF_CAP_SECONDS for s in stub.sleeps)
    assert stub.sleeps[1] == m.LLM_BACKOFF_CAP_SECONDS


def test_429_without_retry_after_backs_off_exponentially(stub):
    def script(n, user):
        raise m._LLMRateLimited(None)
    stub(script)
    _run_pass(3)
    base = m.LLM_BACKOFF_BASE_SECONDS
    assert stub.sleeps[:3] == [min(base * 2 ** i, m.LLM_BACKOFF_CAP_SECONDS) for i in range(3)]


def test_one_429_then_success_is_not_stopped(stub):
    """NEGATIVE: a single rate-limit hiccup must not end the pass."""
    def script(n, user):
        if n == 2:                                   # first real call after warm-up
            raise m._LLMRateLimited(1)
        return "OK" if user == "ping" else "1"
    stub(script)
    result = _run_pass(5)
    assert len(result) == 5 and all(v["account"] for v in result.values())
    assert not m._LLM_RUN_STATUS.get("stopped")
    assert stub.sleeps == [1]


def test_http_429_from_the_wire_carries_retry_after(monkeypatch):
    hdrs = Message()
    hdrs["Retry-After"] = "7"

    def boom(*a, **k):
        raise HTTPError("http://x.invalid", 429, "Too Many", hdrs, io.BytesIO(b""))
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(m, "_emit_mapper_progress", lambda msg: None)
    for chat in (m._ollama_chat, m._openai_compatible_chat):
        with pytest.raises(m._LLMRateLimited) as ei:
            chat("http://x.invalid", "mm", "s", "u")
        assert ei.value.retry_after == 7.0


def test_other_http_errors_are_not_rate_limits(monkeypatch):
    """NEGATIVE: a 500 stays a plain failure (None), not a 429 wait."""
    def boom(*a, **k):
        raise HTTPError("http://x.invalid", 500, "Server", Message(), io.BytesIO(b""))
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(m, "_emit_mapper_progress", lambda msg: None)
    assert m._ollama_chat("http://x.invalid", "mm", "s", "u") is None


# ---- (b) consecutive failures ----------------------------------------------

def test_n_consecutive_failures_stop_the_pass_and_keep_the_rest_unmapped(stub):
    def script(n, user):
        if user == "ping":
            return "OK"
        return "1" if n <= 3 else None               # 2 rows answered, then dead
    prov = stub(script)
    result = _run_pass(20)
    assert sorted(result) == [1, 2]                  # what was mapped is kept
    assert len(prov.calls) == 1 + 2 + N
    st = m._LLM_RUN_STATUS
    assert st["stopped"] and "kept failing" in st["reason"]
    assert st["unattempted_rows"] == list(range(2 + N + 1, 21))


def test_fail_once_then_succeed_is_not_stopped(stub):
    """NEGATIVE: an isolated failure resets the count."""
    seen = {"failed": set()}

    def script(n, user):
        if user == "ping":
            return "OK"
        if n % 2 == 0 and n not in seen["failed"]:   # every other call fails once
            seen["failed"].add(n)
            return None
        return "1"
    stub(script)
    result = _run_pass(12)
    assert not m._LLM_RUN_STATUS.get("stopped")
    assert len(result) >= 6


# ---- (c) invalid-answer rate -----------------------------------------------

def test_always_invalid_is_stopped_by_the_rate_rule(stub):
    prov = stub(lambda n, user: "OK" if user == "ping" else "no idea, sorry")
    result = _run_pass(K + 10)
    assert result == {}
    st = m._LLM_RUN_STATUS
    assert st["stopped"]
    assert MODEL in st["reason"] and "100%" in st["reason"]
    assert len(st["unattempted_rows"]) == 10
    # warm-up + K first answers; every row before the K-th also got its retry
    assert len(prov.calls) == 1 + K + (K - 1) * m.LLM_MAX_RETRIES
    assert any("WARNING" in l and MODEL in l for l in stub.logs)


def test_valid_answers_with_a_few_invalid_are_not_stopped(stub, monkeypatch):
    """NEGATIVE: an invalid rate at or below the threshold never stops."""
    bad = int(K * THRESHOLD)                         # exactly at the limit: not "more than"
    stub(lambda n, user: "OK" if user == "ping" else "1")
    orig = m._parse_shortlist_answer
    state = {"n": 0}

    def parse(reply, shortlist):
        state["n"] += 1
        if state["n"] <= bad * 2:                    # bad first answers AND their retries
            return "invalid", None
        return orig(reply, shortlist)
    monkeypatch.setattr(m, "_parse_shortlist_answer", parse)
    result = _run_pass(K + 5)
    assert not m._LLM_RUN_STATUS.get("stopped")
    assert len(result) == K + 5 - bad


def test_no_model_name_list_exists():
    """NEGATIVE: the rule is behavioural. Nothing in the guard mentions a model family."""
    src = Path(m.__file__).read_text(encoding="utf-8")
    guard = src[src.index("LLM_MAX_CONSECUTIVE_FAILURES ="):src.index("_SHORTLIST_ANSWER_RE =")]
    for name in ("llama", "qwen", "gemma", "mistral", "phi", "gpt", "claude"):
        assert name not in guard.lower()


# ---- end to end: the run summary says why ----------------------------------

def test_run_summary_states_why_and_stopped_rows_are_suspense(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_llm_sleep", lambda s: None)
    monkeypatch.setattr(m, "_resolve_llm_endpoint_config",
                        lambda c, mo=None: ("ollama", "http://stub.invalid", MODEL, None, 0.0))
    monkeypatch.setattr(m, "_llm_chat", lambda *a, **k: "OK" if a[4] == "ping" else None)
    txns = [fx.txn_xml("CAFE ALPHA ORDER", "2025-06-10", [(fx.HDFC1, -10000), ("groc", 10000)])]
    book = fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts(), txns)
    monkeypatch.chdir(tmp_path)
    rows = [("2025-08-01", f"ZZ MISC {i}", "", "100.00") for i in range(1, N + 6)]
    csv_in = fx.canonical_csv(tmp_path / "in.csv", rows)
    out = tmp_path / "out.csv"
    summary = m.run(book, csv_in, str(out), config_path="fake.yaml", bank_name="HDFC",
                    gnucash_bank_account=fx.P_HDFC1)
    assert "AI pass stopped" in summary and "kept failing" in summary
    with open(out, newline="", encoding="utf-8") as f:
        got = list(csv.DictReader(f))
    assert len(got) == N + 5
    assert all(r["Confidence"] == "suspense" for r in got)
    assert sum("AI pass stopped" in r["MatchReason"] for r in got) >= 5
