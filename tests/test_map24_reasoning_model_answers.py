"""
MAP-24 -- AI answers from a reasoning model.

A model that reasons before it answers (gpt-oss) ran out of the 120-token reply
cap, so most answers came back empty or cut off and were counted invalid. Now:

  (a) the AI pass records WHY each unusable answer was unusable (empty / cut off
      by the cap / unparseable) and prints one summary line;
  (b) the reply cap is larger, and gpt-oss models (only) are sent
      reasoning_effort "low".

Only message CONTENT is ever the answer: a reasoning field is never parsed.
No network: urlopen is faked. Synthetic book only.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402
from scripted_llm import ScriptedLLM, choose  # noqa: E402

DINING = "Expenses:Food:Dining"


# ---- the request that goes on the wire -------------------------------------------

class _Resp:
    def __init__(self, body):
        self._b = json.dumps(body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture(monkeypatch, response):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data.decode())
        sent["url"] = req.full_url
        return _Resp(response)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return sent


OAI_OK = {"choices": [{"message": {"content": "2"}, "finish_reason": "stop"}]}


def test_gpt_oss_request_carries_reasoning_effort_low_and_the_larger_cap(monkeypatch):
    sent = _capture(monkeypatch, OAI_OK)
    m._openai_compatible_chat("https://x.invalid/v1", "openai/gpt-oss-120b", "s", "u", api_key="k")
    assert sent["body"]["reasoning_effort"] == "low"
    assert sent["body"]["max_tokens"] == m.LLM_REPLY_MAX_TOKENS > 120


@pytest.mark.parametrize("model", ["llama-3.3-70b-versatile", "gemma4:12b", "gpt-4o-mini",
                                   "qwen3-32b", "openai/gpt-5"])
def test_other_models_never_get_reasoning_effort(monkeypatch, model):
    """NEGATIVE: an unknown field can 400 on other OpenAI-compatible servers."""
    sent = _capture(monkeypatch, OAI_OK)
    m._openai_compatible_chat("https://x.invalid/v1", model, "s", "u")
    assert "reasoning_effort" not in sent["body"]
    assert "reasoning" not in sent["body"]


def test_ollama_request_never_gets_reasoning_effort_even_for_gpt_oss(monkeypatch):
    """NEGATIVE: the Ollama path has no such parameter."""
    sent = _capture(monkeypatch, {"message": {"content": "1"}, "done_reason": "stop"})
    m._ollama_chat("http://localhost:11434", "gpt-oss:20b", "s", "u")
    assert "reasoning_effort" not in json.dumps(sent["body"])
    assert sent["body"]["options"]["num_predict"] == m.LLM_REPLY_MAX_TOKENS


def test_finish_reason_is_plumbed_without_changing_the_none_on_failure_contract(monkeypatch):
    _capture(monkeypatch, {"choices": [{"message": {"content": "1"}, "finish_reason": "length"}]})
    r = m._openai_compatible_chat("https://x.invalid/v1", "m", "s", "u")
    assert r == "1" and r.finish_reason == "length"

    _capture(monkeypatch, {"message": {"content": ""}, "done_reason": "length"})
    r = m._ollama_chat("http://localhost:11434", "m", "s", "u")
    assert r == "" and r.finish_reason == "length"

    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert m._openai_compatible_chat("https://x.invalid/v1", "m", "s", "u") is None
    assert m._ollama_chat("http://localhost:11434", "m", "s", "u") is None


def test_a_reasoning_field_is_never_returned_as_the_answer(monkeypatch):
    """NEGATIVE: the answer is message content only; reasoning text with a
    number in it is not an answer."""
    _capture(monkeypatch, {"choices": [{"message": {
        "content": None, "reasoning": "The best fit is option 2 because ...",
    }, "finish_reason": "length"}]})
    r = m._openai_compatible_chat("https://x.invalid/v1", "openai/gpt-oss-120b", "s", "u")
    assert r == ""
    _capture(monkeypatch, {"message": {"content": "", "thinking": "I pick 2."}, "done_reason": "stop"})
    assert m._ollama_chat("http://localhost:11434", "m", "s", "u") == ""


# ---- the parse + cause --------------------------------------------------------------

SHORT = ["Expenses:A", "Expenses:B", "Expenses:C"]


def test_cause_of_each_unusable_answer():
    p = m._parse_reply_with_cause
    assert p(m._Reply("2", "stop"), SHORT) == ("matched", "Expenses:B", "")
    assert p(m._Reply("0", "stop"), SHORT)[0] == "skip"
    assert p(m._Reply("", "stop"), SHORT) == ("invalid", None, "empty")
    assert p("", SHORT) == ("invalid", None, "empty")
    assert p(None, SHORT) == ("invalid", None, "empty")
    assert p(m._Reply("", "length"), SHORT) == ("invalid", None, "cut off")
    assert p(m._Reply("probably the second one", "stop"), SHORT) == ("invalid", None, "unparseable")


def test_a_cut_off_reply_is_never_read_as_a_list_number():
    """NEGATIVE: "2" cut off by the cap could have been "27"; not trusted."""
    assert m._parse_reply_with_cause(m._Reply("2", "length"), SHORT)[0] == "invalid"
    assert m._parse_reply_with_cause(m._Reply("Option 2", "length"), SHORT)[0] == "invalid"


def test_reasoning_text_containing_a_number_is_not_an_answer():
    """NEGATIVE: chatty text with a digit in it is unparseable, not option 2."""
    r = m._Reply("Let me think. Candidate 2 looks best, 1 is worse.", "stop")
    assert m._parse_reply_with_cause(r, SHORT) == ("invalid", None, "unparseable")


def test_clean_answer_gets_the_same_pick_as_before():
    for text, want in (("1", "Expenses:A"), ("3", "Expenses:C"), (" 2 \n", "Expenses:B")):
        assert m._parse_reply_with_cause(m._Reply(text, "stop"), SHORT)[1] == want
        assert m._parse_shortlist_answer(text, SHORT)[1] == want
        # a plain str (no finish_reason) is unchanged too
        assert m._parse_reply_with_cause(text, SHORT)[1] == want


# ---- the whole pass -----------------------------------------------------------------

@pytest.fixture
def llm(monkeypatch, tmp_path):
    h = ScriptedLLM(monkeypatch, tmp_path)
    yield h
    h.close()


def _history():
    return [
        fx.txn_xml("SOMETHING ELSE", "2025-06-10", [(fx.HDFC1, -10000), ("groc", 10000)]),
        fx.txn_xml("RESTAURANT VISIT", "2025-06-11", [(fx.HDFC1, -20000), ("dining", 20000)]),
    ]


def _run(tmp_path, llm, descs):
    d = tmp_path / "a"
    d.mkdir(exist_ok=True)
    book = fx.write_book(d / "b.gnucash", fx.standard_accounts(), _history())
    csv_in = fx.canonical_csv(d / "in.csv", [("2025-08-%02d" % (i + 1), t, "", "100.00")
                                             for i, t in enumerate(descs)])
    out = d / "out.csv"
    m.run(book, csv_in, str(out), config_path=llm.config_path, bank_name="HDFC",
          gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_summary_line_counts_each_cause(tmp_path, llm):
    llm.say("OKROW", choose(DINING))
    llm.say("SKIPROW", "0")
    llm.say("EMPTYROW", m._Reply("", "stop"))
    llm.say("CUTROW", m._Reply("", "length"))
    llm.say("JUNKROW", "it is probably the second one")
    rows = _run(tmp_path, llm, ["OKROW A", "SKIPROW A", "EMPTYROW A", "CUTROW A", "JUNKROW A"])
    line = [p for p in llm.progress if p.startswith("AI answers:")]
    assert line == ["AI answers: 1 ok, 1 skip, 1 empty, 1 cut off, 1 unparseable"]
    by = {r["Description"]: r for r in rows}
    assert by["OKROW A"]["Account"] == DINING
    for k in ("EMPTYROW A", "CUTROW A", "JUNKROW A"):
        assert by[k]["Account"] != DINING          # unusable answers book nothing
        assert by[k]["Confidence"] != "llm"


def test_a_cut_off_answer_that_looks_like_a_valid_number_books_nothing(tmp_path, llm):
    """NEGATIVE, end to end: "1" with finish_reason length is not trusted."""
    llm.say("CUTROW", m._Reply("1", "length"))
    rows = _run(tmp_path, llm, ["CUTROW A"])
    assert rows[0]["Confidence"] != "llm"
    assert rows[0]["Account"] != DINING


def test_the_stop_rule_is_unchanged(tmp_path, llm):
    """MAP-19: more than 80% invalid over the first K stops the pass."""
    llm.say("EMPTYROW", m._Reply("", "length"))
    n = m.LLM_VALIDITY_WINDOW_K + 5
    _run(tmp_path, llm, [f"EMPTYROW {i}" for i in range(n)])
    assert any("AI pass stopped" in p for p in llm.progress)
    assert m.LLM_VALIDITY_WINDOW_K == 20 and m.LLM_INVALID_RATE_STOP == 0.8
    assert m.LLM_MAX_RETRIES == 1


def test_an_own_account_pick_is_still_refused(tmp_path, llm):
    """NEGATIVE: with a clean reply (finish_reason stop) naming an own bank
    account for a third-party row, the MAP-14 gate still withholds it."""
    def pick_own(user):
        for line in user.splitlines():
            import re
            mo = re.match(r"^(\d+)\. (.*)$", line)
            if mo and fx.P_HDFC2 in mo.group(2):
                return m._Reply(mo.group(1), "stop")
        return "0"
    llm.say("ZZQ", pick_own)
    hist = _history() + [
        fx.txn_xml("ZZQ MERCHANT", "2025-06-12", [(fx.HDFC1, -10000), (fx.HDFC2, 10000)]),
        fx.txn_xml("ZZQ MERCHANT", "2025-06-13", [(fx.HDFC1, -10000), ("dining", 10000)]),
    ]
    d = tmp_path / "o"
    d.mkdir()
    book = fx.write_book(d / "b.gnucash", fx.standard_accounts(), hist)
    csv_in = fx.canonical_csv(d / "in.csv", [("2025-08-01", "ZZQ MERCHANT PAYMENT", "", "100.00")])
    out = d / "out.csv"
    m.run(book, csv_in, str(out), config_path=llm.config_path, bank_name="HDFC",
          gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        row = list(csv.DictReader(f))[0]
    assert any(fx.P_HDFC2 in c["user"] for c in llm.real_calls())
    assert row["Confidence"] == "suspense"
    assert "your own account" in row["MatchReason"]
