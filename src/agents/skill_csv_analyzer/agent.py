"""
agent.py — CSV Data Analyzer (agent mode).

Builds a LangGraph ReAct agent with pandas-based tools and sends
the user's natural-language question along with the CSV path.
"""
from __future__ import annotations

from pathlib import Path

from agents.base_agent import build_agent
from agents.skill_csv_analyzer.tools import describe_csv, query_csv

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")
TOOLS = [describe_csv, query_csv]


def run(
    csv_path: str,
    question: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """
    Analyse a CSV file and answer a natural-language question.

    Args:
        csv_path:       Path to the CSV file.
        question:       The user's question about the data.
        output_path:    Where to write the output .md file.
        config_path:    Path to config.yaml (LLM settings).
        model_override: Optional model name override.

    Returns:
        The LLM's analysis (also written to *output_path*).
    """
    src = Path(csv_path)
    if not src.is_file():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    if not question.strip():
        raise ValueError("No question provided.")

    print(f"[csv_analyzer] CSV: {src.name}, question: {question[:80]}…")

    agent = build_agent(TOOLS, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({
        "messages": [(
            "user",
            f"Analyse this CSV file and answer my question.\n\n"
            f"**CSV file:** {csv_path}\n"
            f"**Question:** {question}\n\n"
            f"Start by calling describe_csv to understand the data, "
            f"then use query_csv to answer the question."
        )]
    })

    answer = result["messages"][-1].content

    # Write output.
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(answer, encoding="utf-8")
    print(f"[csv_analyzer] Analysis written to {out}")

    return answer
