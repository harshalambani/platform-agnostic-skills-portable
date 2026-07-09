"""
agent.py — 26AS TDS Journal agent.

The file paths are bound here, into closure tools, so the LLM never has to
pass a path (small local models garble long Windows paths). The model only:
  1. calls build_journals()  — no arguments,
  2. optionally calls apply_overrides(overrides={...}) — account picks only.
The deterministic builder always writes a valid, balanced CSV; the override
step is pure polish.
"""
from pathlib import Path

from langchain_core.tools import tool

from agents.base_agent import build_agent
from agents.skill_26as_journal import tools as T

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")


def _make_tools(xlsx_path: str, gnucash_path: str, output_path: str) -> list:
    """Build the closure tools that bind the file paths, so the LLM never has to
    pass a path (small local models garble long Windows paths) — it only chooses
    accounts. Defined at module scope (not nested in ``run``) so tests can
    construct and inspect the tool schemas without invoking an LLM."""

    @tool
    def build_journals() -> str:
        """Build the GnuCash TDS journal CSV. Takes NO arguments — the workbook,
        GnuCash file and output path are already configured. Writes and verifies
        the CSV and lists any NEEDS REVIEW deductors with their candidate
        accounts. Call this first."""
        return T.run_build(xlsx_path, gnucash_path, output_path)

    @tool
    def apply_overrides(overrides: dict | None = None) -> str:
        """Optional. Resolve the NEEDS REVIEW deductors. `overrides` is an OBJECT
        mapping the deductor Sr.No (as a string) to a chosen full account path,
        e.g. {"2": "Income:Interest Income:Interest on HDFC - FD"}.
        Use only Sr numbers flagged NEEDS REVIEW and account paths from their
        candidate lists; omit any you are unsure about (leaving them on Suspense
        is correct). Do not pass any file paths. `overrides` is optional and
        defaults to none — calling with no overrides is a harmless no-op that
        leaves the already-verified CSV unchanged."""
        return T.run_apply(xlsx_path, gnucash_path, output_path, overrides)

    return [build_journals, apply_overrides]


def run(
    xlsx_path: str,
    gnucash_path: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """Build GnuCash TDS journals from a 26AS Convert workbook + a .gnucash file.

    Paths are captured in the closure tools below, so the model cannot mistype
    them — it only chooses accounts for the NEEDS REVIEW deductors.
    """
    tools = _make_tools(xlsx_path, gnucash_path, output_path)
    agent = build_agent(tools, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({
        "messages": [(
            "user",
            "Create the GnuCash TDS journals. Call build_journals() first. If it "
            "reports NEEDS REVIEW deductors and a candidate clearly fits, call "
            "apply_overrides with your picks; otherwise you are done. Then report "
            "the saved CSV, how many matched vs. went to Suspense, and any "
            "accounts to create."
        )]
    })
    # Return a SINGLE deterministic summary computed from the output files, not
    # the model's free-text narration — a small local model mislabels the counts
    # (and mistypes the filename), which contradicts the real numbers. The LLM's
    # only job was to make the apply_overrides tool calls; its prose is dropped.
    summary = T.final_summary(output_path, gnucash_path)
    if summary:
        return summary
    return result["messages"][-1].content
