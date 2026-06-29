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


def run_ui(
    task: str,
    parser_path: str,
    parser_args: str = "",
    notes: str = "",
    output_path: str = "",
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """
    UI entry point (skill.yaml entry_point: agent:run_ui).

    Builds a concrete instruction from the Parser Generator tab's fields, drives
    the agent via run(), saves the report into the run's output directory, and
    returns the report for the tab to display. The structured fields keep the
    powered-by-LLM step honest: the tab still only edits blanks and runs the
    gate via the deterministic tools.
    """
    task_l = (task or "").lower()
    parser_path = (parser_path or "").strip()
    parser_args = (parser_args or "").strip()
    notes = (notes or "").strip()

    if not parser_path:
        return "Error: please provide a parser path."

    if "creat" in task_l:
        instruction = (
            f"Create a new parser at {parser_path} for this statement format: "
            f"{notes or '(name it via FORMAT_NAME)'}. Call "
            "create_parser_from_template to fill FORMAT_NAME and any blanks you "
            "can infer, then validate_parser the result. Tell me clearly that the "
            "extract_rows and write_output bodies are stubs that still need "
            "implementing by hand. Do not commit."
        )
    else:
        instruction = (
            f"The parser at {parser_path} failed its tie-out (exit 2)."
            + (f" Symptom / expected balance: {notes}." if notes else "")
            + " Use extract_blanks to list the editable blanks, fix only the "
            "wrong blank constant(s) with apply_template_edit, then re-run the "
            f"tie-out with run_tieout (args: {parser_args or '<none provided>'}). "
            "Report the change you made and the gate result; do not claim "
            "success unless run_tieout reports exit 0. Do not commit."
        )

    report = run(instruction, config_path=config_path, model_override=model_override)

    if output_path:
        out_dir = Path(output_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "parser-generator-report.md").write_text(report, encoding="utf-8")
    return report
