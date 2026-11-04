"""
MAP-06 -- the ScriptedLLM harness (tests/scripted_llm.py) runs the mapper's
whole AI pass offline through the REAL guard, pacer and prompts.

Synthetic book only (gnc_book_fixture). No real names, no real numbers.
"""
from __future__ import annotations

import csv
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as mapper  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402
from scripted_llm import INVALID, FAIL, RateLimit, ScriptedLLM, choose  # noqa: E402

DINING = "Expenses:Food:Dining"


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


def _run(tmp_path, llm, descs, name="a", txns=None):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    book = fx.write_book(d / "b.gnucash", fx.standard_accounts(), txns or _history())
    csv_in = fx.canonical_csv(d / "in.csv",
                              [("2025-08-%02d" % (i + 1), t, "", "100.00")
                               for i, t in enumerate(descs)])
    out = d / "out.csv"
    mapper.run(book, csv_in, str(out), config_path=llm.config_path, bank_name="HDFC",
               gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_scripted_answers_give_identical_output_twice(tmp_path, llm):
    """The same script on the same book yields byte-identical results and the
    same prompts, run after run."""
    descs = ["QQQ ALPHA SHOP", "QQQ BETA SHOP"]
    llm.say("QQQ", choose(DINING))
    first = _run(tmp_path, llm, descs, "a")
    calls1 = [c["user"] for c in llm.real_calls()]
    llm.calls.clear()
    second = _run(tmp_path, llm, descs, "b")
    calls2 = [c["user"] for c in llm.real_calls()]
    assert first == second
    assert calls1 == calls2 and calls1
    assert all(r["Account"] == DINING and r["Confidence"] == "llm" for r in first)
    # negative: nothing went to the network
    assert llm.http_attempts == []


def test_invalid_answers_never_become_an_invented_account(tmp_path, llm):
    """NEGATIVE: an unparseable answer books nothing to the LLM's guess."""
    llm.say("QQQ", INVALID)
    rows = _run(tmp_path, llm, ["QQQ ALPHA SHOP"])
    assert rows[0]["Confidence"] != "llm"
    assert "Suspense" in rows[0]["Account"]


def test_429_backs_off_on_the_fake_clock_then_succeeds(tmp_path, llm):
    """The real MAP-19 backoff runs: it waits the server's Retry-After on the
    FAKE clock (no real sleeping), then the retry lands."""
    llm.say("QQQ", [RateLimit(7), choose(DINING)])
    rows = _run(tmp_path, llm, ["QQQ ALPHA SHOP"])
    assert 7 in llm.clock.sleeps or any(s >= 7 for s in llm.clock.sleeps)
    assert rows[0]["Account"] == DINING
    assert len(llm.real_calls()) == 2


def test_pacer_spaces_calls_on_the_fake_clock(tmp_path, monkeypatch):
    h = ScriptedLLM(monkeypatch, tmp_path, min_gap=30)
    try:
        h.say("QQQ", choose(DINING))
        _run(tmp_path, h, ["QQQ ONE", "QQQ TWO", "QQQ THREE"])
        times = [c["at"] for c in h.calls]
        gaps = [b - a for a, b in zip(times, times[1:])]
        assert len(times) >= 3
        assert all(g >= 30 - 1e-6 for g in gaps), gaps
    finally:
        h.close()


def test_pacer_is_not_bypassed_when_pacing_is_off(tmp_path, llm):
    """NEGATIVE: with no gap configured the fake clock never advances."""
    llm.say("QQQ", choose(DINING))
    t0 = llm.clock.now()
    _run(tmp_path, llm, ["QQQ ONE", "QQQ TWO"])
    assert llm.clock.now() == t0
    assert llm.clock.sleeps == []


def test_real_map14_guard_withholds_an_own_account_answer(tmp_path, llm):
    """The scripted model wants a third-party payment on an own bank account:
    the real MAP-14 gate withholds it to Suspense."""
    llm.say("ZZQ", choose(fx.P_HDFC2))
    hist = _history() + [
        fx.txn_xml("ZZQ MERCHANT", "2025-06-12", [(fx.HDFC1, -10000), (fx.HDFC2, 10000)]),
        fx.txn_xml("ZZQ MERCHANT", "2025-06-13", [(fx.HDFC1, -10000), ("dining", 10000)]),
    ]
    rows = _run(tmp_path, llm, ["ZZQ MERCHANT PAYMENT"], txns=hist)
    # the own account really was offered to the model (not a vacuous pass)
    assert any(fx.P_HDFC2 in c["user"] for c in llm.real_calls())
    assert rows[0]["Confidence"] == "suspense"
    assert rows[0]["Account"] != "Root Account:" + fx.P_HDFC2
    assert "your own account" in rows[0]["MatchReason"]


def test_provider_failure_books_nothing_to_the_ai(tmp_path, llm):
    llm.say("QQQ", FAIL)
    rows = _run(tmp_path, llm, ["QQQ ONE"])
    assert rows[0]["Confidence"] != "llm"


def test_stop_ends_the_ai_pass_and_makes_no_more_calls(tmp_path, monkeypatch):
    """The real MAP-20 pacer honours Stop while it waits for its slot: after
    the user stops during the first answer, no further call is made and the
    remaining rows are not booked by the AI."""
    h = ScriptedLLM(monkeypatch, tmp_path, min_gap=30)
    try:
        def first_then_stop(prompt):
            h.stop()
            return choose(DINING)
        h.say("QQQ", first_then_stop)
        rows = _run(tmp_path, h, ["QQQ ONE", "QQQ TWO", "QQQ THREE"])
        assert len(h.real_calls()) == 1
        assert [r["Confidence"] for r in rows].count("llm") == 1
    finally:
        h.close()


def test_a_real_network_attempt_fails_loudly(monkeypatch, tmp_path):
    """NEGATIVE: anything that reaches for the network trips the wire, and the
    fixture teardown (close) fails the test even if the code swallowed it."""
    h = ScriptedLLM(monkeypatch, tmp_path)
    with pytest.raises(AssertionError):
        urllib.request.urlopen("http://example.invalid/")
    with pytest.raises(AssertionError, match="real HTTP attempted"):
        h.close()
    h.http_attempts.clear()
    h.close()


def test_harness_sits_below_the_pacer_and_guard():
    """The harness replaces only the transport functions, not _llm_chat."""
    src = Path(mapper.__file__).read_text(encoding="utf-8")
    assert "def _llm_chat(" in src
    text = (ROOT / "tests" / "scripted_llm.py").read_text(encoding="utf-8")
    assert 'setattr(m, "_llm_chat"' not in text
