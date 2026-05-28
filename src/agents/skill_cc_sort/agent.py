"""
agent.py - Extract and Sort CC PDFs LangGraph agent (v3.0).

Frozen-mode note: this skill calls a Python script directly (Step 2).
In frozen mode sys.executable is pa_skills.exe, so we use runpy.run_path()
to avoid re-launching the entire exe as a child process. In source mode
we keep the normal subprocess.run() approach.
"""
import sys
from pathlib import Path

from agents.base_agent import build_agent
from agents.skill_cc_sort.tools import check_qpdf_available, check_extract_msg_available

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")
TOOLS = [check_qpdf_available, check_extract_msg_available]

SCRIPT = Path(__file__).parent / "scripts" / "extract_sort_cc_pdfs.py"


def _run_script(script: Path, args: list[str]) -> int:
    """
    Run a Python script. In frozen mode, uses runpy to avoid re-launching
    the exe. In source mode, uses subprocess for clean process isolation.
    """
    if getattr(sys, "frozen", False):
        import runpy
        saved_argv = sys.argv[:]
        sys.argv = [str(script)] + args
        try:
            runpy.run_path(str(script), run_name="__main__")
            return 0
        except SystemExit as e:
            return int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)
        finally:
            sys.argv = saved_argv
    else:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(script)] + args,
            capture_output=False, text=True,
        )
        return result.returncode


def run(
    input_folder: str,
    output_folder: str,
    password: str = "",
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Run the CC Sort agent and return the final response.

    Strategy: use the LLM only for pre-flight checks (qpdf, extract-msg).
    Call the script directly with exact paths to avoid LLM path substitution bugs.

    Args:
        input_folder:   Folder containing MSG files and/or raw PDFs.
        output_folder:  Folder where organized output will be created.
        password:       Decryption password(s), comma-separated. Empty = auto-detect.
        config_path:    Path to config.yaml.
        model_override: Optional model name, e.g. 'gemma4', 'llama3.1'.
    """
    # Step 1: pre-flight checks via LLM agent
    agent = build_agent(TOOLS, SYSTEM_PROMPT, config_path, model_override)
    check_result = agent.invoke({
        "messages": [(
            "user",
            "Check that qpdf and extract-msg are both available. "
            "Report OK or what needs to be installed."
        )]
    })
    check_summary = check_result["messages"][-1].content
    print(f"[Pre-flight] {check_summary}")

    # Step 2: call the script directly with exact paths (no LLM involvement).
    # Uses runpy in frozen mode to avoid subprocess → pa_skills.exe → shim overhead.
    args = [input_folder, output_folder]
    if password:
        args.append(password)

    print(f"\n[Running] {SCRIPT.name} {' '.join(args)}\n")
    rc = _run_script(SCRIPT, args)

    if rc != 0:
        return f"Sort failed with exit code {rc}. Check output above for details."

    return (
        f"Sort completed successfully.\n"
        f"Input:  {input_folder}\n"
        f"Output: {output_folder}\n"
        f"Check the Decrypted_PDFs_Correct/ folder in the output location."
    )
