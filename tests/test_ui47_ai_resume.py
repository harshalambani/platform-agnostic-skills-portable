"""
UI-47 -- an AI pass that stopped (out of credits, rate limit, daily cap) can be
resumed: the answers already given are kept per book + input file and a re-run
asks the AI only for the rows not done. Stubbed provider: no network, no sleep.
"""
from __future__ import annotations

import csv
import io
import json
import sys
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import llm_errors  # noqa: E402
from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
from agents.skill_gnucash_account_mapper import ai_kept  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402

MODEL = "stub-model-zz"
HIST = [{"account": "Expenses:Food", "description": "CAFE ALPHA", "frequency": 5}]
URL = "http://x.invalid"


def _rows(n, tag="MISC PAYMENT"):
    return [{"row": i, "description": f"{tag} {i}", "deposit": "0", "withdrawal": "100.00"}
            for i in range(1, n + 1)]


def _credit_error(code=402):
    body = b'{"error":{"message":"Your credit balance is too low to access the API"}}'
    return HTTPError(URL, code, "err", Message(), io.BytesIO(body))


def _refuse_credits():
    m._note_llm_error(_credit_error(), URL, MODEL, 10)
    return None


class Provider:
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
    monkeypatch.setattr(m, "_emit_mapper_progress", lambda msg: None)

    def install(script):
        prov = Provider(script)
        monkeypatch.setattr(m, "_llm_chat", prov)
        return prov
    install.sleeps = sleeps
    return install


def _kept(tmp_path, book="book", data=b"input-1"):
    f = tmp_path / "in.csv"
    f.write_bytes(data)
    return ai_kept.KeptAnswers(tmp_path / f"{book}_ai_answers.json", book, ai_kept.file_hash(str(f)))


def _pass(rows, kept=None, target_ok=None):
    return m.llm_fallback_mapping(
        unmatched_rows=rows, account_tree=[], example_mappings=[],
        config_path="fake.yaml", historical_mappings=HIST, kept=kept, target_ok=target_ok)


def _credits_after(k):
    """Warm-up + k good answers, then the provider reports it is out of credits."""
    def script(n, user):
        if user == "ping":
            return "OK"
        if n <= k + 1:
            return "1"
        return _refuse_credits()
    return script


def _ok(n, user):
    return "OK" if user == "ping" else "1"


# ---- error kind ------------------------------------------------------------

@pytest.mark.parametrize("status,text", [
    (400, "Your credit balance is too low to access the Anthropic API"),
    (429, "You exceeded your current quota, please check your plan and billing details"),
    (429, "insufficient_quota"),
    (402, "Payment Required"),
    (403, "Quota exhausted for this project"),
])
def test_classify_out_of_credits(status, text):
    class E(Exception):
        status_code = status
    # a refused key (401/403) with quota wording is still reported as credits
    assert llm_errors.classify(E(text)).kind in ("out_of_credits", "auth")
    if status != 403:
        assert llm_errors.classify(E(text)).kind == "out_of_credits"


def test_plain_429_is_still_rate_limit():
    class E(Exception):
        status_code = 429
    assert llm_errors.classify(E("Too many requests, slow down")).kind == "rate_limit"
    assert not llm_errors.is_out_of_credits(429, "Rate limit reached for requests per minute")


# ---- out of credits stops at once, answers are kept -------------------------

def test_out_of_credits_stops_after_one_call(stub, tmp_path):
    kept = _kept(tmp_path)
    prov = stub(lambda n, u: _refuse_credits())
    result = _pass(_rows(5), kept)
    assert len(prov.calls) == 1                      # the warm-up only; no retries
    assert result == {} and len(kept) == 0
    assert m._LLM_RUN_STATUS["stopped"] is True
    why = m._LLM_RUN_STATUS["reason"]
    assert "out of credits" in why and "Run again" in why and "0 of 5 rows answered and kept" in why


def test_credits_mid_run_keep_given_answers_and_name_the_counts(stub, tmp_path):
    kept = _kept(tmp_path)
    prov = stub(_credits_after(3))
    result = _pass(_rows(8), kept)
    assert sorted(result) == [1, 2, 3]
    assert len(prov.calls) == 5                      # ping + 3 answers + the refused call, then stop
    assert len(kept) == 3
    why = m._LLM_RUN_STATUS["reason"]
    assert "3 of 8 rows answered and kept" in why and "picks up from the rows not done" in why
    assert m._LLM_RUN_STATUS["unattempted_rows"] == [4, 5, 6, 7, 8]


def test_resume_asks_only_for_unanswered_rows_then_clears(stub, tmp_path):
    kept = _kept(tmp_path)
    stub(_credits_after(3))
    _pass(_rows(8), kept)
    assert kept.path.exists()
    kept2 = _kept(tmp_path)
    prov = stub(_ok)
    result = _pass(_rows(8), kept2)
    assert sorted(result) == list(range(1, 9))
    assert len(prov.calls) == 1 + 5                  # ping + only rows 4..8
    assert not any(f"MISC PAYMENT {i}" in u for u in prov.calls for i in (1, 2, 3))
    assert not m._LLM_RUN_STATUS.get("stopped")
    assert not kept2.path.exists()                   # fully finished: nothing left behind


def test_resume_returns_kept_answers_even_if_credits_are_still_out(stub, tmp_path):
    kept = _kept(tmp_path)
    stub(_credits_after(3))
    _pass(_rows(8), kept)
    kept2 = _kept(tmp_path)
    stub(lambda n, u: _refuse_credits())
    result = _pass(_rows(8), kept2)
    assert sorted(result) == [1, 2, 3] and len(kept2) == 3
    assert m._LLM_RUN_STATUS["stopped"] and "3 of 8" in m._LLM_RUN_STATUS["reason"]


def test_daily_cap_wording_says_kept_and_rerun(stub, tmp_path):
    from agents.llm_pacing import DailyCapReached
    kept = _kept(tmp_path)

    def script(n, u):
        if u == "ping" or n <= 3:
            return _ok(n, u)
        raise DailyCapReached("daily cap of 2 calls reached")
    stub(script)
    _pass(_rows(6), kept)
    why = m._LLM_RUN_STATUS["reason"]
    assert "daily cap" in why and "2 of 6 rows answered and kept" in why and "Run again" in why


# ---- negatives ---------------------------------------------------------------

def test_normal_run_leaves_nothing_and_matches_a_run_without_kept(stub, tmp_path):
    stub(_ok)
    plain = _pass(_rows(5))
    kept = _kept(tmp_path)
    with_kept = _pass(_rows(5), kept)
    assert plain == with_kept
    assert not kept.path.exists()
    assert not m._LLM_RUN_STATUS.get("stopped")


def test_skips_and_invalid_answers_are_never_kept(stub, tmp_path):
    kept = _kept(tmp_path)

    def script(n, u):
        if u == "ping":
            return "OK"
        if "MISC PAYMENT 1 " in u:
            return "0"                 # SKIP
        if "MISC PAYMENT 2 " in u:
            return "garbage words"     # invalid, and its retries
        if "MISC PAYMENT 3 " in u:
            return "1"
        return _refuse_credits()
    stub(script)
    _pass(_rows(4), kept)
    data = json.loads(kept.path.read_text(encoding="utf-8"))
    rows = list(data["files"].values())[0]
    assert len(rows) == 1 and all(v["account"] for v in rows.values())


def test_changed_row_is_asked_again(stub, tmp_path):
    kept = _kept(tmp_path)
    stub(_credits_after(3))
    _pass(_rows(6), kept)
    rows = _rows(6)
    rows[1]["description"] = "MISC PAYMENT 2 CHANGED"
    rows[2]["description"] = "MISC PAYMENT 3 ALSO CHANGED"
    kept2 = _kept(tmp_path)
    prov = stub(_ok)
    _pass(rows, kept2)
    asked = " ".join(prov.calls)
    assert "CHANGED" in asked and "MISC PAYMENT 3" in asked      # both changed rows asked again
    assert "MISC PAYMENT 1" not in asked                          # the unchanged one was not


def test_other_file_or_other_book_never_reuses_answers(stub, tmp_path):
    kept = _kept(tmp_path)
    stub(_credits_after(3))
    _pass(_rows(6), kept)
    for other in (_kept(tmp_path, data=b"input-2"), _kept(tmp_path, book="other-book")):
        prov = stub(_ok)
        assert len(other) == 0
        _pass(_rows(6), other)
        assert len(prov.calls) == 1 + 6


def test_kept_answer_with_unusable_target_is_asked_again(stub, tmp_path):
    kept = _kept(tmp_path)
    stub(_credits_after(3))
    _pass(_rows(6), kept)
    kept2 = _kept(tmp_path)
    prov = stub(_ok)
    _pass(_rows(6), kept2, target_ok=lambda a: False)             # now hidden / placeholder / missing
    assert len(prov.calls) == 1 + 6


def test_kept_answer_for_an_account_no_longer_offered_is_not_used(stub, tmp_path):
    kept = _kept(tmp_path)
    rows = _rows(2)
    stub(_credits_after(1))
    _pass(rows, kept)
    # the book's history no longer has that account at all
    kept2 = _kept(tmp_path)
    prov = stub(_ok)
    res = m.llm_fallback_mapping(
        unmatched_rows=rows, account_tree=[], example_mappings=[], config_path="fake.yaml",
        historical_mappings=[{"account": "Expenses:Other", "description": "CAFE ALPHA", "frequency": 5}],
        kept=kept2)
    assert len(prov.calls) == 1 + 2
    assert all(v["account"] == "Expenses:Other" for v in res.values())


def test_user_stop_keeps_answers_and_makes_no_further_calls(stub, tmp_path, monkeypatch):
    import ui._runner as runner
    kept = _kept(tmp_path)
    prov = stub(_ok)
    monkeypatch.setattr(runner, "is_cancelled", lambda: len(prov.calls) >= 3)
    result = _pass(_rows(8), kept)
    assert len(prov.calls) == 3 and sorted(result) == [1, 2]
    assert len(kept) == 2 and kept.path.exists()                  # not cleared by a Stop


def test_plain_429_still_backs_off_and_retries(stub, tmp_path):
    kept = _kept(tmp_path)

    def script(n, u):
        if n == 2:
            raise m._LLMRateLimited(1)
        return _ok(n, u)
    stub(script)
    result = _pass(_rows(3), kept)
    assert len(result) == 3 and stub.sleeps == [1]
    assert not m._LLM_RUN_STATUS.get("stopped")


def test_429_with_quota_text_on_the_wire_is_out_of_credits(monkeypatch):
    body = b'{"error":{"message":"You exceeded your current quota","type":"insufficient_quota"}}'

    def boom(*a, **k):
        raise HTTPError(URL, 429, "Too Many", Message(), io.BytesIO(body))
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(m, "_emit_mapper_progress", lambda msg: None)
    assert m._openai_compatible_chat(URL, "mm", "s", "u") is None
    err = m._take_llm_error()
    assert err["fatal"] and err["credits"] and "out of credits" in err["reason"]


def test_store_holds_no_descriptions_or_secrets(stub, tmp_path):
    kept = _kept(tmp_path)
    stub(_credits_after(3))
    _pass(_rows(6), kept)
    text = kept.path.read_text(encoding="utf-8")
    assert "MISC PAYMENT" not in text and "api_key" not in text.lower()


# ---- end to end: the stopped file is complete, the resume completes it ---------

def _e2e(tmp_path, monkeypatch, chat):
    monkeypatch.setattr(m, "_llm_sleep", lambda s: None)
    monkeypatch.setattr(m, "_resolve_llm_endpoint_config",
                        lambda c, mo=None: ("ollama", "http://stub.invalid", MODEL, None, 0.0))
    monkeypatch.setattr(m, "_llm_chat", chat)
    txns = [fx.txn_xml("CAFE ALPHA ORDER", "2025-06-10", [(fx.HDFC1, -10000), ("groc", 10000)])]
    book = fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts(), txns)
    monkeypatch.chdir(tmp_path)
    rows = [("2025-08-01", f"ZZ MISC {i}", "", "100.00") for i in range(1, 7)]
    csv_in = fx.canonical_csv(tmp_path / "in.csv", rows)
    out = tmp_path / "out.csv"
    summary = m.run(book, csv_in, str(out), config_path="fake.yaml", bank_name="HDFC",
                    gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        return summary, list(csv.DictReader(f))


def test_stopped_run_writes_full_file_then_resume_completes_without_duplicates(tmp_path, monkeypatch):
    state = {"n": 0}

    def credits_chat(provider, base_url, model, system, user, api_key=None, timeout=None):
        state["n"] += 1
        if user == "ping":
            return "OK"
        if state["n"] <= 3:
            return "1"
        return _refuse_credits()
    summary, got = _e2e(tmp_path, monkeypatch, credits_chat)
    assert len(got) == 6                                          # the full file, as today
    stopped = [r for r in got if "AI pass stopped" in r["MatchReason"]]
    assert stopped and all(r["Confidence"] == "suspense" for r in stopped)
    assert "out of credits" in summary
    assert "waiting for a re-run" in summary and "hand-mapping" in summary

    asked = []

    def ok_chat(provider, base_url, model, system, user, api_key=None, timeout=None):
        asked.append(user)
        return "OK" if user == "ping" else "1"
    summary2, got2 = _e2e(tmp_path, monkeypatch, ok_chat)
    assert len(got2) == 6
    assert len({r["Description"] for r in got2}) == 6             # no row twice
    assert len(asked) == 1 + len(stopped)                         # only the unanswered rows
    assert not any("AI pass stopped" in r["MatchReason"] for r in got2)
    assert "**AI pass stopped:**" not in summary2
