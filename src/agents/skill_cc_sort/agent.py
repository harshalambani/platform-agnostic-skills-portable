"""
agent.py - Extract and Sort CC PDFs — DIRECT mode, no LLM.

Frozen-mode note: in frozen mode sys.executable is pa_skills.exe, so we use
runpy.run_path() to avoid re-launching the entire exe as a child process; in
source mode we use subprocess. Pre-flight dependency checks are deterministic
(no language model).
"""
import shutil
import sys
from pathlib import Path

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
    Decrypt + sort raw CC PDFs/MSGs into organized folders under output_folder.
    Direct mode — no LLM. config_path / model_override accepted for interface
    compatibility and ignored.
    """
    # Deterministic pre-flight checks (qpdf is also checked by the framework).
    if shutil.which("qpdf") is None:
        return "ERROR: qpdf not found on PATH. Install qpdf and retry."
    notes = []
    try:
        import extract_msg  # noqa: F401
    except Exception:
        notes.append("Note: extract-msg not importable — .msg inputs will be "
                     "skipped; PDFs are still processed.")

    args = [input_folder, output_folder]
    if password:
        args.append(password)

    print(f"\n[Running] {SCRIPT.name} {' '.join(args)}\n")
    rc = _run_script(SCRIPT, args)
    if rc != 0:
        return f"Sort failed with exit code {rc}. Check output above for details."

    msg = (
        f"Sort completed successfully.\n"
        f"Input:  {input_folder}\n"
        f"Output: {output_folder}\n"
        f"Check the Decrypted_PDFs_Correct/ folder in the output location."
    )
    if notes:
        msg += "\n\n" + "\n".join(notes)
    return msg
