"""DOC-09: help of every skill that uses the LLM carries the endpoint address rule."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent / "src" / "agents"
RULE = ("http:// works only for this computer and private-network addresses, "
        "and never with an API key. Anything else needs https://. "
        "Change the Base URL in Settings.")
ROW = "Run errors mentioning the endpoint / model."
# LLM skills whose help has no endpoint row yet (own row is a separate call).
NO_ROW_YET = {"skill_cc_spend_booking", "skill_itr_workbook"}


def _skills():
    out = {}
    for p in sorted(ROOT.glob("skill_*/skill.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if d.get("display_name"):          # registered (visible) skills only
            out[p.parent.name] = d
    return out


def _rows(d):
    return (d.get("help") or {}).get("troubleshooting") or []


LLM = {n: d for n, d in _skills().items()
       if (d.get("requires") or {}).get("llm", True) is True}   # absent = uses LLM
OTHER = {n: d for n, d in _skills().items() if n not in LLM}


def test_there_are_llm_skills():
    assert len(LLM) >= 8


@pytest.mark.parametrize("name", sorted(set(LLM) - NO_ROW_YET))
def test_llm_skill_endpoint_row_carries_the_address_rule(name):
    rows = [r for r in _rows(LLM[name]) if r.get("problem") == ROW]
    assert len(rows) == 1, name
    assert RULE in rows[0]["fix"]
    assert "Settings" in rows[0]["fix"]


@pytest.mark.parametrize("name", sorted(NO_ROW_YET))
def test_known_gap_stays_listed_honestly(name):
    # If a row is added, drop the name from NO_ROW_YET so it is guarded.
    assert name in LLM
    assert not [r for r in _rows(LLM[name]) if r.get("problem") == ROW]


@pytest.mark.parametrize("name", sorted(OTHER))
def test_non_llm_skill_help_does_not_mention_the_rule(name):   # NEGATIVE
    text = yaml.safe_dump(OTHER[name].get("help") or {})
    assert "Base URL" not in text
    assert "private-network" not in text
    assert RULE not in text
