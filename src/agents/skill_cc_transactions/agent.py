"""
agent.py - Create CC Transaction List skill.

This skill runs the extraction script directly — no LLM loop needed because
the paths are fully determined at call time and no reasoning is required.
The LLM agent path is preserved in run_with_agent() for future use.

The script is loaded in-process, so it also works in the frozen app.
"""
from pathlib import Path


SCRIPT = Path(__file__).parent / "scripts" / "create_cc_transaction_list.py"
SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")


def _load_script():
    """Import the extraction script in-process (works frozen and from source)."""
    import importlib.util
    import sys
    spec = importlib.util.spec_from_file_location("create_cc_transaction_list", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def run(
    pdf_dir: str,
    output_excel: str,
    period: str = "",
    custom_start: str = "",
    custom_end: str = "",
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Extract CC transactions from organized PDFs and write an Excel workbook.

    Calls the extraction script in-process (no LLM loop). Statements that
    overlap the chosen period are kept; the Tie-out sheet covers whole
    statements. The period (default: last completed FY) is named in the
    result and on the Summary sheet.

    Args:
        pdf_dir:        Folder with Bank-CardType/ subfolders containing decrypted PDFs.
        output_excel:   Full path for the output .xlsx file.
        period, custom_start, custom_end: the shared period picker inputs.
        config_path, model_override: unused (API compatibility).
    """
    from agents.period_picker import resolve_period

    try:
        start, end, label = resolve_period(period, custom_start, custom_end)
    except ValueError as e:
        return f"ERROR: {e}"
    folder = Path(pdf_dir)
    if not folder.is_dir():
        return f"ERROR: PDF folder not found: {pdf_dir}"
    output_path = Path(output_excel)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mod = _load_script()
    result = mod.run_extraction(folder, output_path, start, end, label)
    return result.message


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
    from agents.skill_cc_transactions.tools import extract_cc_transactions
    from agents.base_agent import build_agent

    tools = [extract_cc_transactions]
    agent = build_agent(tools, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({
        "messages": [(
            "user",
            f"Extract all credit card transactions from these organized PDFs into Excel.\n"
            f"PDF folder:    {pdf_dir}\n"
            f"Output Excel:  {output_excel}\n"
            f"DO NOT ask for clarification. The paths are already provided above.\n"
                        f"Step 1: call extract_cc_transactions with pdf_dir='{pdf_dir}' and "
            f"output_excel='{output_excel}'.\n"
            f"Step 2: report total transactions, breakdown by bank, and confirm the Excel is ready."
        )]
    })
    return result["messages"][-1].content
