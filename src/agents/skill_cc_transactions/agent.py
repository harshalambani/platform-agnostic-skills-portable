"""
agent.py - Create CC Transaction List skill.

This skill runs the extraction script directly — no LLM loop needed because
the paths are fully determined at call time and no reasoning is required.
The LLM agent path is preserved in run_with_agent() for future use.
"""
import subprocess
import sys
from pathlib import Path
from agents.base_agent import build_agent

SCRIPT = Path(__file__).parent / "scripts" / "create_cc_transaction_list.py"
SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")


def run(
    pdf_dir: str,
    output_excel: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Extract CC transactions from organized PDFs and write an Excel workbook.

    Calls the extraction script directly (no LLM loop) — paths are fully
    known at invocation time so an agentic loop adds no value and risks
    the model stalling for clarification.

    Args:
        pdf_dir:        Folder with Bank-CardType/ subfolders containing decrypted PDFs.
                        Typically the Decrypted_PDFs_Correct/ output from the sort skill.
        output_excel:   Full path for the output .xlsx file.
        config_path:    Unused (kept for API compatibility with other skills).
        model_override: Unused (kept for API compatibility with other skills).
    """
    # Ensure output directory exists
    output_path = Path(output_excel)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), pdf_dir, output_excel],
        # capture_output=False streams stdout/stderr to terminal in real time
    )
    if result.returncode != 0:
        return f"ERROR: script exited with code {result.returncode}"
    return f"Done. Output: {output_excel}"


def run_with_agent(
    pdf_dir: str,
    output_excel: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    LLM-agent version — kept for debugging / experimentation.
    Use run() for normal operation.
    """
    from agents.skill_cc_transactions.tools import extract_cc_transactions, check_pdftotext_available

    tools = [extract_cc_transactions, check_pdftotext_available]
    agent = build_agent(tools, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({
        "messages": [(
            "user",
            f"Extract all credit card transactions from these organized PDFs into Excel.\n"
            f"PDF folder:    {pdf_dir}\n"
            f"Output Excel:  {output_excel}\n"
            f"DO NOT ask for clarification. The paths are already provided above.\n"
            f"Step 1: call check_pdftotext_available to verify pdftotext is installed.\n"
            f"Step 2: call extract_cc_transactions with pdf_dir='{pdf_dir}' and "
            f"output_excel='{output_excel}'.\n"
            f"Step 3: report total transactions, breakdown by bank, and confirm the Excel is ready."
        )]
    })
    return result["messages"][-1].content
