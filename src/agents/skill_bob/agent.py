"""
agent.py — Bank of Baroda LangGraph agent.
"""
from pathlib import Path
from agents.base_agent import build_agent
from agents.skill_bob.tools import extract_bob

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")
TOOLS = [extract_bob]


def run(
    pdf_path: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Run the BoB agent on a PDF and return the final response.

    Args:
        pdf_path:       Path to the Bank of Baroda statement PDF.
        output_path:    Path where the output .csv should be saved.
        config_path:    Path to config.yaml.
        model_override: Optional model name, e.g. 'llama3.1', 'qwen3', 'phi4-mini'.
    """
    agent = build_agent(TOOLS, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({
        "messages": [(
            "user",
            f"Extract transactions from this Bank of Baroda PDF to CSV.\n"
            f"Input PDF:  {pdf_path}\n"
            f"Output CSV: {output_path}"
        )]
    })
    return result["messages"][-1].content
