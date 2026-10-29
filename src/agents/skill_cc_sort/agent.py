"""
agent.py - Extract and Sort CC PDFs — DIRECT mode, no LLM.

Frozen-mode note: in frozen mode sys.executable is pa_skills.exe, so we use
runpy.run_path() to avoid re-launching the entire exe as a child process; in
source mode we use subprocess. Pre-flight dependency checks are deterministic
(no language model).
"""
import json
import os
import shutil
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent / "scripts" / "extract_sort_cc_pdfs.py"


def _run_script(script: Path, args: list[str], stdin_text: str | None = None) -> int:
    """
    Run a Python script. In frozen mode, uses runpy to avoid re-launching
    the exe. In source mode, uses subprocess for clean process isolation.
    """
    if getattr(sys, "frozen", False):
        import runpy
        import io
        saved_argv = sys.argv[:]
        saved_stdin = sys.stdin
        sys.argv = [str(script)] + args
        if stdin_text is not None:
            sys.stdin = io.StringIO(stdin_text)
        try:
            runpy.run_path(str(script), run_name="__main__")
            return 0
        except SystemExit as e:
            return int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)
        finally:
            sys.argv = saved_argv
            sys.stdin = saved_stdin
    else:
        import subprocess
        result = subprocess.run(
            [sys.executable, str(script)] + args,
            capture_output=False, text=True, input=stdin_text,
        )
        return result.returncode


def run(
    input_folder: str,
    output_folder: str,
    password: str = "",
    period: str = "",
    custom_start: str = "",
    custom_end: str = "",
    config_path: str = "config.yaml",
    model_override: str = None,
    settings_path: str = "",
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

    # SEC-15: the password goes to the script on stdin, never in its argv.
    args = [input_folder, output_folder]
    if password:
        args.append("--password-stdin")

    # The password is deliberately not echoed -- joining argv here put the live
    # statement password into stdout on every run. See _pw_label() in
    # scripts/extract_sort_cc_pdfs.py.
    shown = [input_folder, output_folder] + (["<password hidden>"] if password else [])
    print(f"\n[Running] {SCRIPT.name} {' '.join(shown)}\n")
    # CC-28: the script reads the card list (cc_cards.yaml, beside the settings file).
    # It runs as a subprocess, so the settings path travels in the environment.
    _env = "PASK_CC_SETTINGS_PATH"
    _saved = os.environ.get(_env)
    if settings_path:
        os.environ[_env] = str(settings_path)
    else:
        os.environ.pop(_env, None)
    try:
        rc = _run_script(SCRIPT, args,
                         stdin_text=(password + "\n") if password else None)
    finally:
        if _saved is None:
            os.environ.pop(_env, None)
        else:
            os.environ[_env] = _saved
    if rc != 0:
        return f"Sort failed with exit code {rc}. Check output above for details."

    out_dir = Path(output_folder)
    failed: list[str] = []
    refused: list[dict] = []
    cards_problem = None
    unreadable = False
    try:
        _res = json.loads((out_dir / "sort_results.json").read_text(encoding="utf-8"))
        failed = list(_res.get("failed_decryption", []))
        refused = list(_res.get("refused", []))
        cards_problem = _res.get("cards_problem")
    except Exception:
        unreadable = True   # never a silent success: decrypt failures may be missing

    # CC-01: completeness check -- never say "successfully" over a gap.
    from agents import period_picker
    from agents.skill_cc_sort import completeness
    try:
        chosen = period_picker.resolve_period(period, custom_start, custom_end)
        report = completeness.check_folder(out_dir / "Decrypted_PDFs_Correct", chosen, failed,
                                           results_unreadable=unreadable)
    except ValueError as e:
        return f"ERROR: {e}"
    issues = list(report.issues)
    # CC-28: a file refused because of its card digits is never placed silently.
    for r in refused:
        issues.append(f"{r.get('file', '?')} was not placed on a card: {r.get('reason', '')}. "
                      f"It is in Unknown-Unknown.")
    if cards_problem:
        issues.append(f"The card list was not used: {cards_problem}. Statement CSVs cannot be "
                      f"placed without it, and PDFs were placed by the words in their file name only.")
    body = report.panel_text()
    try:
        (out_dir / "completeness_report.txt").write_text(report.full_text() + "\n", encoding="utf-8")
    except OSError:
        pass

    if issues:
        head = (f"Sort completed with {len(issues)} issue(s):\n\n"
                + "\n".join(f"- {i}" for i in issues))
    else:
        head = "Sort completed successfully."
    # UI-39: every line is its own Markdown list item, so none runs into the next in the panel.
    msg = (
        f"{head}\n\n"
        f"- Input:  {input_folder}\n"
        f"- Output: {output_folder}\n"
        f"- Check the Decrypted_PDFs_Correct/ folder in the output location.\n\n"
        f"{body}"
    )
    if notes:
        msg += "\n\n" + "\n".join(notes)
    return msg
