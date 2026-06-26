"""
agent.py - Parser Generator (DEV-TIME) LangGraph agent.

An LLM-supported developer tool that creates / corrects / edits the project's
embedded fuzzy deterministic parsers, template-based, triggered manually when a
parser's tie-out fails (exit code 2). Every mutation goes through the
deterministic tools in tools.py, so the LLM can only fill the format-specific
blanks - never the balance oracle or the 0/1/2 exit contract. Each result must
pass the quality gate (validate -> run tie-out, exit 0) before a developer
commits it.

NOT registered in the UI: skill.yaml omits the discovery-required fields (same
mechanism as adapter_bob). Invoke it from the dev CLI:
    python -m agents.skill_parser_generator --help
"""
from __future__ import annotations

from pathlib import Path

from agents.skill_parser_generator.tools import (
    apply_template_edit,
    create_parser_from_template,
    extract_blanks,
    run_tieout,
    validate_parser,
)

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")

TOOLS = [
    extract_blanks,
    validate_parser,
    apply_template_edit,
    create_parser_from_template,
    run_tieout,
]


def run(
    instruction: str,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """
    Drive the Parser Generator agent with a developer instruction.

    The instruction is free-form but should name the parser to fix (or the
    format to create), the sample input, and the tie-out symptom - for example:

        "parse_krc_ledger.py exited 2 on sample_ac109.pdf: recomputed closing
         101234.50 vs printed 101230.50. Find the wrong blank, fix it, and
         re-run the tie-out (args: sample_ac109.pdf out.xlsx)."

    The agent inspects blanks, edits only blank constants, and runs the quality
    gate via tools; it must not claim success unless run_tieout reports exit 0.
    Returns the agent's final message. Does not commit.
    """
    from agents.base_agent import build_agent

    agent = build_agent(TOOLS, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({"messages": [("user", instruction)]})
    return result["messages"][-1].content
