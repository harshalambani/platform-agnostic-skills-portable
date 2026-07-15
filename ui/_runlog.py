"""
ui/_runlog.py — centralized per-run log files.

Every skill run gets its own file at Data/logs/<timestamp>-<skill>.log,
written once the run finishes (success, failure, or cancellation). Before
this, exceptions surfaced only as bare fragments in the UI, and agent-mode
tool failures could be silently absorbed into the LLM's narrated reply with
no trace anywhere -- the on-disk warnings.log held only Gradio's own noise.
This is why the HDFC ``.format()`` crash and the HSBC OCR ZeroDivisionError
were both invisible until reproduced manually outside the UI.

The log always includes the run's own step-by-step markdown log (which, for
agent-mode skills, doubles as the tool-call transcript -- see
``_runner._format_event``), plus a full traceback when the run raised.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ui import _config


def new_log_path(skill_name: str) -> Path:
    """Allocate (but don't create) a timestamped log path for one run."""
    logs_dir = _config.data_root_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in skill_name)
    return logs_dir / f"{stamp}-{safe_name}.log"


def write_run_log(
    path: Path,
    *,
    skill_name: str,
    run_log_lines: list[str],
    traceback_text: str | None = None,
) -> None:
    """Write the full per-run log. Never raises -- logging must never be the
    thing that breaks a run."""
    try:
        parts = [
            f"Skill: {skill_name}",
            f"Timestamp: {datetime.now().isoformat()}",
            "",
            "=== Run log (agent-mode: this is also the tool-call transcript) ===",
            *(run_log_lines or ["(empty)"]),
        ]
        if traceback_text:
            parts += ["", "=== Traceback ===", traceback_text]
        path.write_text("\n".join(parts), encoding="utf-8")
    except Exception:
        pass
