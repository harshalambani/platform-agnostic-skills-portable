"""
agent.py — HSBC Bank Statement LangGraph agent.
"""
from pathlib import Path
from agents.base_agent import build_agent
from agents.skill_hsbc.tools import run_hsbc_pipeline, skip_ocr_pipeline

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")
TOOLS = [run_hsbc_pipeline, skip_ocr_pipeline]


def run(
    pdf_dir: str,
    work_dir: str,
    output_path: str,
    title: str = "HSBC Statement",
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Run the HSBC agent and return the final response.

    Args:
        pdf_dir:        Directory containing HSBC PDF statements.
        work_dir:       Scratch directory for intermediate files.
        output_path:    Path where the output .xlsx should be saved.
        title:          Workbook title shown in the Summary sheet.
        config_path:    Path to config.yaml.
        model_override: Optional model name, e.g. 'llama3.1', 'qwen3', 'phi4-mini'.
    """
    agent = build_agent(TOOLS, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({
        "messages": [(
            "user",
            f"Process these HSBC bank statement PDFs into a clean Excel workbook.\n"
            f"PDF directory: {pdf_dir}\n"
            f"Work directory: {work_dir}\n"
            f"Output Excel:  {output_path}\n"
            f"Title: {title}\n"
            f"Run the full pipeline (including OCR) and report the summary."
        )]
    })
    return result["messages"][-1].content
