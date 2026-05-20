"""
agent.py — 26AS LangGraph agent.
"""
from pathlib import Path
from agents.base_agent import build_agent
from agents.skill_26as.tools import extract_26as, verify_26as_output

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")
TOOLS = [extract_26as, verify_26as_output]


def run(
    pdf_path: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Run the 26AS agent on a PDF and return the final response.

    Args:
        pdf_path:       Path to the Form 26AS PDF file.
        output_path:    Path where the output .xlsx should be saved.
        config_path:    Path to config.yaml.
        model_override: Optional model name, e.g. 'llama3.1', 'qwen3', 'phi4-mini'.
                        Overrides the default_model in config.yaml.
    """
    agent = build_agent(TOOLS, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({
        "messages": [(
            "user",
            f"Convert this Form 26AS PDF to Excel.\n"
            f"Input PDF:    {pdf_path}\n"
            f"Output Excel: {output_path}\n"
            f"Run extraction, then verify the output."
        )]
    })
    return result["messages"][-1].content
