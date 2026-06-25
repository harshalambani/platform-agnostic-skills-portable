"""
agent.py — Parser Generator (DEV-TIME) LangGraph agent.

SKELETON (v1.1 Session A — design only). This wires the standard skill shape
(build_agent(TOOLS, SYSTEM_PROMPT, config_path, model_override)) but ships an
empty TOOLS list: the tools are implemented in Session B (see AGENT.md ->
"Planned tools"). The skill is intentionally NOT registered in the UI — its
skill.yaml omits the discovery-required fields (same mechanism as adapter_bob).

Purpose: an LLM-supported developer tool that creates / corrects / edits the
project's embedded fuzzy deterministic parsers, template-based, triggered
manually when a parser's tie-out fails (exit code 2). Every result must pass
the quality gate (AST validate -> lint -> re-run tie-out, exit 0) before a
developer commits it.
"""
from __future__ import annotations

from pathlib import Path

# Session B imports the agent builder here:
#   from agents.base_agent import build_agent
# (kept out of the skeleton to stay import-clean while TOOLS is empty.)

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")

# Session B populates this from a sibling tools.py, e.g.:
#   from agents.skill_parser_generator.tools import (
#       validate_parser, run_tieout, apply_template_edit,
#   )
#   TOOLS = [validate_parser, run_tieout, apply_template_edit]
TOOLS: list = []


def run(
    parser_path: str,
    sample_input: str,
    tieout_target: str | None = None,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """
    Correct or edit a failing fuzzy deterministic parser (template-based).

    SKELETON: Session B implements the tool-driven body. The signature reflects
    the locked design — a developer points the skill at the parser that exited
    tie-out (exit 2) plus the sample input it failed on.

    Args:
        parser_path:    Path to the failing parser
                        (src/agents/skill_<X>/scripts/parse_<format>.py).
        sample_input:   The input file the parser failed to tie out.
        tieout_target:  Optional expected closing balance / tie-out reference.
        config_path:    Path to legacy config.yaml for the model.
        model_override: Optional model name overriding default_model.
    """
    raise NotImplementedError(
        "skill_parser_generator is a v1.1 Session A skeleton; the tool-driven "
        "run() is implemented in Session B. See AGENT.md for the design."
    )
    # Session B body (sketch):
    #   agent = build_agent(TOOLS, SYSTEM_PROMPT, config_path, model_override)
    #   result = agent.invoke({"messages": [(
    #       "user",
    #       f"Parser failing tie-out: {parser_path}\n"
    #       f"Sample input: {sample_input}\n"
    #       f"Tie-out target: {tieout_target or '(use the statement's own '
    #       f'printed closing balance)'}\n"
    #       f"Diagnose which template blanks are wrong, propose a minimal "
    #       f"template-respecting edit, then run the quality gate "
    #       f"(AST -> lint -> tie-out). Report the diff and gate results; "
    #       f"do not commit."
    #   )]})
    #   return result["messages"][-1].content
