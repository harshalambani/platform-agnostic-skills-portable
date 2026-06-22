"""
agent.py — 26AS TDS Journal LangGraph agent.

Deterministic-first: the builder does fuzzy account matching with a Suspense
fallback. The agent's only job is the LLM-fallback step — resolving any
deductor the matcher flagged NEEDS REVIEW by picking from its candidate
accounts — then verifying the result. On a clean match (nothing flagged) the
agent simply builds and verifies.
"""
from pathlib import Path

from agents.base_agent import build_agent
from agents.skill_26as_journal.tools import (
    apply_journal_overrides,
    build_tds_journals,
    verify_journal_csv,
)

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")
TOOLS = [build_tds_journals, apply_journal_overrides, verify_journal_csv]


def run(
    xlsx_path: str,
    gnucash_path: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Build GnuCash TDS journals from a 26AS Convert workbook + a .gnucash file.

    Args:
        xlsx_path:      Path to the 26AS workbook produced by the Convert tab.
        gnucash_path:   Path to the user's .gnucash file (for account matching).
        output_path:    Path where the journal .csv should be saved.
        config_path:    Path to config.yaml.
        model_override: Optional model name overriding default_model.
    """
    agent = build_agent(TOOLS, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({
        "messages": [(
            "user",
            f"Create GnuCash TDS journals.\n"
            f"26AS workbook: {xlsx_path}\n"
            f"GnuCash file:  {gnucash_path}\n"
            f"Output CSV:    {output_path}\n"
            f"Build the journals, resolve any NEEDS REVIEW deductors from their "
            f"candidate accounts (leave on Suspense if none fit), then verify."
        )]
    })
    return result["messages"][-1].content
