"""
tests/skill_gnucash_account_mapper/test_mapper_relevance_ranking.py --
regression guard for MAP-02: the relevance pre-filter degenerated to
reverse-alphabetical order.

Background (src/agents/skill_gnucash_account_mapper/agent.py):

  _score_account_relevance (feeding _build_historical_prompt's "top 10
  relevant + 2 by frequency") and _retry_with_focused_prompt both built a
  list of (score, acct, descs) tuples and called `scored.sort(reverse=True)`.
  When a transaction description shares no keywords with any historical
  description, every score is 0 -- and Python's tuple comparison then falls
  through the tied score to compare `acct` strings in REVERSE, so the "top"
  accounts were just the last few account names alphabetically, not a
  relevance ranking of any kind.

  Fixed with one shared ranking helper (_rank_account_groups) used by both
  call sites, with an explicit sort key: score desc, then total historical
  frequency desc, then account name ASC. A zero-score group is never
  treated as "relevant" -- _build_historical_prompt only fills its
  remaining slots from the frequency ranking (itself now also name-asc
  tie-broken), never from a zero-score "relevance" pick.

All account names/descriptions below are synthetic, invented for this test
file.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402


def _groups(pairs):
    """pairs: {account: (description, frequency)} -> the groups shape the
    ranking helpers expect: {account: [(description, frequency), ...]}."""
    return {acct: [(desc, freq)] for acct, (desc, freq) in pairs.items()}


# ---------------------------------------------------------------------------
# _rank_account_groups -- zero overlap must never fall back to reverse-alpha
# ---------------------------------------------------------------------------

def test_zero_overlap_ranking_is_frequency_then_name_asc_not_reverse_alpha():
    groups = _groups({
        "Expenses:Zebra Category": ("SOMETHING UNRELATED", 5),
        "Expenses:Apple Category": ("SOMETHING UNRELATED", 5),
        "Expenses:Mango Category": ("SOMETHING UNRELATED", 3),
    })
    ranked = agent._rank_account_groups(groups, "COMPLETELY DIFFERENT TOKENS HERE")
    names = [acct for _score, acct, _descs in ranked]

    # Reverse-alphabetical would be: Zebra, Mango, Apple.
    assert names != ["Expenses:Zebra Category", "Expenses:Mango Category", "Expenses:Apple Category"]
    # Frequency desc (5,5,3), then name asc among the tied-frequency pair.
    assert names == [
        "Expenses:Apple Category",
        "Expenses:Zebra Category",
        "Expenses:Mango Category",
    ]
    assert all(score == 0 for score, _acct, _descs in ranked)


def test_equal_scores_never_order_by_reverse_name():
    """Two groups that score equally (non-zero, identical overlap) and have
    equal frequency must break the tie by name ASC, never reverse name."""
    groups = _groups({
        "Expenses:Zeta Group": ("SWIGGY FOOD ORDER", 2),
        "Expenses:Alpha Group": ("SWIGGY FOOD ORDER", 2),
    })
    ranked = agent._rank_account_groups(groups, "SWIGGY FOOD ORDER")
    names = [acct for _score, acct, _descs in ranked]
    assert names[0] == "Expenses:Alpha Group"
    assert names[1] == "Expenses:Zeta Group"


def test_ranking_identical_across_insertion_orders():
    pairs = {
        "Expenses:Groceries": ("BIGBASKET ORDER", 4),
        "Expenses:Utilities": ("ELECTRICITY BILL", 4),
        "Expenses:Rent": ("HOUSE RENT PAYMENT", 1),
        "Income:Salary": ("MONTHLY SALARY CREDIT", 10),
    }
    groups_order_a = _groups(pairs)
    groups_order_b = {k: groups_order_a[k] for k in reversed(list(groups_order_a))}

    ranked_a = agent._rank_account_groups(groups_order_a, "UNRELATED DESCRIPTION TEXT")
    ranked_b = agent._rank_account_groups(groups_order_b, "UNRELATED DESCRIPTION TEXT")

    names_a = [acct for _s, acct, _d in ranked_a]
    names_b = [acct for _s, acct, _d in ranked_b]
    assert names_a == names_b


# ---------------------------------------------------------------------------
# _score_account_relevance -- now a thin wrapper, same guarantees
# ---------------------------------------------------------------------------

def test_score_account_relevance_matches_rank_account_groups():
    groups = _groups({
        "Expenses:Zebra Category": ("SOMETHING UNRELATED", 5),
        "Expenses:Apple Category": ("SOMETHING UNRELATED", 5),
    })
    scored = agent._score_account_relevance(groups, "NO OVERLAP AT ALL")
    names = [acct for _score, acct, _descs in scored]
    assert names == ["Expenses:Apple Category", "Expenses:Zebra Category"]


# ---------------------------------------------------------------------------
# _build_historical_prompt -- end result seen by the LLM
# ---------------------------------------------------------------------------

def test_build_historical_prompt_zero_overlap_not_reverse_alphabetical():
    historical_mappings = []
    # Five accounts, all sharing no keyword with the transaction below, with
    # descending frequency by design so a correct frequency ranking is
    # unambiguous and clearly distinguishable from reverse-alphabetical.
    for name, freq in [
        ("Expenses:Zulu Items", 1),
        ("Expenses:Yankee Items", 1),
        ("Expenses:Whiskey Items", 1),
        ("Expenses:Victor Items", 1),
        ("Expenses:Uniform Items", 1),
    ]:
        for _ in range(freq):
            historical_mappings.append({"account": name, "description": "SOME OLD DESC", "frequency": freq})

    prompt = agent._build_historical_prompt(historical_mappings, "BRAND NEW UNSEEN TOKEN", "")

    # All five have zero overlap and equal frequency -> must be name-ASC,
    # i.e. Uniform, Victor, Whiskey, Yankee, Zulu -- not reverse-alpha
    # (Zulu, Yankee, Whiskey, Victor, Uniform).
    positions = {name: prompt.find(name) for name in [
        "Expenses:Uniform Items", "Expenses:Victor Items", "Expenses:Whiskey Items",
        "Expenses:Yankee Items", "Expenses:Zulu Items",
    ]}
    assert all(p >= 0 for p in positions.values())
    ordered = sorted(positions, key=positions.get)
    assert ordered == [
        "Expenses:Uniform Items", "Expenses:Victor Items", "Expenses:Whiskey Items",
        "Expenses:Yankee Items", "Expenses:Zulu Items",
    ]


def test_build_historical_prompt_relevant_group_ranked_above_zero_score_groups():
    historical_mappings = [
        {"account": "Expenses:Food and Dining", "description": "SWIGGY FOOD ORDER", "frequency": 1},
        {"account": "Expenses:Zulu Unrelated", "description": "SOMETHING ELSE ENTIRELY", "frequency": 50},
    ]
    prompt = agent._build_historical_prompt(historical_mappings, "SWIGGY FOOD ORDER", "")
    # The relevant (score > 0) group must appear before the zero-score
    # group, even though the zero-score group has vastly higher frequency.
    assert prompt.find("Expenses:Food and Dining") < prompt.find("Expenses:Zulu Unrelated")
