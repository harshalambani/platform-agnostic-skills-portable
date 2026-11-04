"""
tests/skill_gnucash_account_mapper/test_mapper_answer_ambiguity.py --
regression guard for MAP-01: an ambiguous leaf was resolved by set-iteration
order.

Background (src/agents/skill_gnucash_account_mapper/agent.py):

  _validate_llm_answer's partial/prefix-stripped matching looped
  `for acct in account_set:` and returned the FIRST _segment_match hit.
  account_set is a Python `set`, so when the LLM's answer is a ':'-delimited
  tail shared by more than one account (e.g. "Food and Dining" matches both
  "Expenses:Food and Dining" and "Expenses:Travel:Food and Dining"), which
  account got returned depended on PYTHONHASHSEED / set-construction order --
  a nondeterministic, effectively-random mapping decision.

  Fixed by collecting ALL segment-tail matches; exactly one resolves, two or
  more is ambiguous and returns None (never a heuristic pick such as
  shortest path) -- the same "a tie returns no match" policy already used by
  MAP-08's keyword fallback scoring. An exact full-path match still always
  wins outright before the tail-matching tier is even considered.

All account names below are synthetic, invented for this test file.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402


# ---------------------------------------------------------------------------
# Positive cases
# ---------------------------------------------------------------------------

def test_unique_tail_still_resolves():
    account_set = {
        "Expenses:Food and Dining",
        "Expenses:Travel:Airfare",
        "Income:Salary",
    }
    assert agent._validate_llm_answer("Food and Dining", account_set) == "Expenses:Food and Dining"


def test_exact_match_beats_tail_match():
    """An account that IS a full-path exact match must win outright, even
    when another account in the set would also tail-match the same
    answer."""
    account_set = {
        "Food and Dining",
        "Expenses:Food and Dining",
    }
    assert agent._validate_llm_answer("Food and Dining", account_set) == "Food and Dining"


# ---------------------------------------------------------------------------
# Negative: ambiguous tail -> None, deterministically
# ---------------------------------------------------------------------------

_AMBIGUOUS_SET_A = {
    "Expenses:Food and Dining",
    "Expenses:Travel:Food and Dining",
}
_AMBIGUOUS_SET_B = {
    "Expenses:Travel:Food and Dining",
    "Expenses:Food and Dining",
}  # same members, built in the opposite literal order


def test_ambiguous_tail_returns_none_insertion_order_a():
    assert agent._validate_llm_answer("Food and Dining", set(_AMBIGUOUS_SET_A)) is None


def test_ambiguous_tail_returns_none_insertion_order_b():
    """Same logical set, built via the opposite insertion order -- must
    give the SAME (None) result as order A. On the old first-hit-wins code
    this could return either account depending on set iteration order."""
    assert agent._validate_llm_answer("Food and Dining", set(_AMBIGUOUS_SET_B)) is None


def test_ambiguous_tail_never_resolves_via_explicit_incremental_build():
    """Build the set by adding elements one at a time, in both orders, and
    confirm neither ever produces a non-None (i.e. an arbitrary pick)."""
    s1 = set()
    s1.add("Expenses:Food and Dining")
    s1.add("Expenses:Travel:Food and Dining")

    s2 = set()
    s2.add("Expenses:Travel:Food and Dining")
    s2.add("Expenses:Food and Dining")

    result1 = agent._validate_llm_answer("Food and Dining", s1)
    result2 = agent._validate_llm_answer("Food and Dining", s2)
    assert result1 is None
    assert result2 is None


def test_three_way_ambiguous_tail_returns_none():
    account_set = {
        "Expenses:Food and Dining",
        "Expenses:Travel:Food and Dining",
        "Expenses:Business:Food and Dining",
    }
    assert agent._validate_llm_answer("Food and Dining", account_set) is None


def test_ambiguous_tail_deterministic_across_pythonhashseed(tmp_path):
    """Run the ambiguous-tail scenario in fresh subprocesses under two
    different PYTHONHASHSEED values. Set iteration order for str keys is a
    function of the hash seed, so this is the most direct regression guard
    against the original set-iteration-order bug: both subprocesses must
    report the same (None) result."""
    script = tmp_path / "check_ambiguous.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from agents.skill_gnucash_account_mapper import agent\n"
        "account_set = {'Expenses:Food and Dining', 'Expenses:Travel:Food and Dining'}\n"
        "result = agent._validate_llm_answer('Food and Dining', account_set)\n"
        "print('RESULT=' + repr(result))\n",
        encoding="utf-8",
    )

    results = []
    for seed in ("0", "12345"):
        proc = subprocess.run(
            [sys.executable, str(script)],
            env={"PYTHONHASHSEED": seed, "PATH": __import__("os").environ.get("PATH", "")},
            capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0, f"seed={seed} failed: {proc.stderr}"
        line = next(out_line for out_line in proc.stdout.splitlines() if out_line.startswith("RESULT="))
        results.append(line)

    assert results[0] == results[1] == "RESULT=None", results
