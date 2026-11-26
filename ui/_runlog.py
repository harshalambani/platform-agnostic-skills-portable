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

import os
import re
import time
from datetime import datetime
from pathlib import Path

from ui import _config


# AUD-13: run logs are kept for 90 days. Only files in this one folder whose
# name is a run-log name (<timestamp>-<skill>.log) and whose own modified date is
# older than the limit are removed; nothing else is looked at.
RETENTION_DAYS = 90
_RUN_LOG_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}-.+\.log$")


def prune_old_logs(logs_dir: Path, *, days: int = RETENTION_DAYS, now: float | None = None) -> int:
    """Remove run-log files in `logs_dir` older than `days`. Returns how many
    were removed. Never raises: a failed prune must never stop a run.

    Only plain files named like a run log are considered (a link, a folder or
    any other file is left alone), and nothing outside `logs_dir` is touched:
    if `logs_dir` is itself a link to somewhere else, nothing is pruned."""
    removed = 0
    try:
        logs_dir = Path(logs_dir)
        if logs_dir.is_symlink() or logs_dir.is_junction():
            return 0
        cutoff = (time.time() if now is None else now) - days * 86400
        with os.scandir(logs_dir) as it:
            entries = list(it)
        for e in entries:
            try:
                if not _RUN_LOG_NAME.match(e.name):
                    continue
                if e.is_symlink() or e.is_junction() or not e.is_file(follow_symlinks=False):
                    continue
                if e.stat(follow_symlinks=False).st_mtime >= cutoff:
                    continue
                os.unlink(e.path)
                removed += 1
            except Exception:
                continue   # locked or read-only: leave it, carry on
    except Exception:
        pass
    return removed


def new_log_path(skill_name: str) -> Path:
    """Allocate (but don't create) a timestamped log path for one run."""
    logs_dir = _config.data_root_dir() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    prune_old_logs(logs_dir)   # AUD-13: once per new run log
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in skill_name)
    return logs_dir / f"{stamp}-{safe_name}.log"


def redact(text: str, secrets=()) -> str:
    """Replace every non-empty secret value (a statement password, an API key)
    with ***, then any bearer token, key=value secret or "--password x" flag
    still in the text. The rules live in agents.secret_redact (SEC-15)."""
    from agents.secret_redact import redact_text
    return redact_text(text, secrets)


def write_run_log(
    path: Path,
    *,
    skill_name: str,
    run_log_lines: list[str],
    traceback_text: str | None = None,
    secrets=(),
) -> None:
    """Write the full per-run log. Never raises -- logging must never be the
    thing that breaks a run. Any value in ``secrets`` (password inputs) is
    replaced by *** wherever it appears."""
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
        path.write_text(redact("\n".join(parts), secrets), encoding="utf-8")
    except Exception:
        pass


def append_outcome(path: Path, text: str, *, secrets=()) -> None:
    """CC-08: add the run's final error or result text to the log file the UI
    names as "Full log", so the file holds what the screen showed. Existing
    content is left as it was. Never raises; secrets are redacted."""
    try:
        if not text:
            return
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n\n=== Outcome ===\n" + redact(str(text), secrets) + "\n")
    except Exception:
        pass
