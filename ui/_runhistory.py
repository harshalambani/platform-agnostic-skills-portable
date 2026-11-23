"""
ui/_runhistory.py -- one structured line per finished run (UI-54).

The per-run text logs (ui/_runlog.py) hold the full step-by-step story of one
run. This file is the short index over them: skill, entity, year, result and,
when the run did not end well, why. It is what the Home dashboard reads.

File: <data root>/logs/run-history.jsonl, one JSON object per line, appended
once when a run ends (success, failure, stop, cancel, page closed).

Rules this module keeps:
  * It never raises. A write that fails is noted once per process (a Python
    warning, which the app's warning log collects) and the run carries on
    exactly as if nothing had happened.
  * One process-wide lock guards append and trim, so two runs finishing
    together cannot interleave lines or lose a record during a trim.
  * Retention: the newest MAX_RECORDS records, and nothing older than
    MAX_AGE_DAYS. A trim rewrites to a temp file and os.replace()s it, so a
    crash mid-trim leaves the old file whole.
  * tail() skips any line it cannot parse (for example a half-written last
    line after a crash).
  * The tab a skill lives under is NOT stored: the dashboard works it out
    from the skill at display time, so a later tab move cannot leave stale
    names in old records.
"""
from __future__ import annotations

import json
import os
import threading
import warnings
from datetime import datetime, timedelta
from pathlib import Path

from ui import _config

SCHEMA = 1
MAX_RECORDS = 1000
MAX_AGE_DAYS = 90
WHY_MAX = 300

RESULTS = ("ok", "attention", "stopped", "failed", "cancelled",
           "interrupted", "not_ready", "refused")

FIELDS = ("v", "ts", "run", "skill", "entity", "year", "result", "why", "secs", "log", "out")

# Test switch: the hook in ui/tabs/_generic.py skips recording when False.
ENABLED = True

# Look at the file for age/size every this many appends (and on the first one
# in a process), not on every run.
_TRIM_EVERY = 25

_LOCK = threading.Lock()
_state = {"appends": 0, "warned": False}


def path() -> Path:
    """Where the history lives. Resolved through the app's own data root;
    nothing is created until the first append."""
    return _config.data_root_dir() / "logs" / "run-history.jsonl"


def _warn_once(what: str) -> None:
    if _state["warned"]:
        return
    _state["warned"] = True
    try:
        warnings.warn(f"run history not written: {what}", RuntimeWarning, stacklevel=3)
    except Exception:  # noqa: BLE001 -- a warnings filter set to "error" must not break a run
        pass


def make_record(*, run="", skill="", entity="", year="", result="ok", why="",
                secs=0, log="", out="", ts=None) -> dict:
    """A well-formed record: known fields only, bounded text, valid result."""
    result = result if result in RESULTS else "failed"
    why = " ".join(str(why or "").split())[:WHY_MAX]

    def _s(v, n=200):
        return str(v or "")[:n]
    try:
        secs = max(0, int(round(float(secs or 0))))
    except (TypeError, ValueError):
        secs = 0
    return {
        "v": SCHEMA,
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "run": _s(run), "skill": _s(skill), "entity": _s(entity), "year": _s(year),
        "result": result, "why": why, "secs": secs, "log": _s(log), "out": _s(out),
    }


def _parse(line: str):
    try:
        rec = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(rec, dict) or "ts" not in rec or "skill" not in rec:
        return None
    return rec


def _age_ok(rec: dict, cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(rec["ts"])) >= cutoff
    except (ValueError, TypeError):
        return False


def _trim_locked(p: Path, now: datetime | None = None) -> None:
    """Keep the newest MAX_RECORDS records no older than MAX_AGE_DAYS. Caller
    holds _LOCK. Rewrites only when something has to go."""
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return
    cutoff = (now or datetime.now()) - timedelta(days=MAX_AGE_DAYS)
    kept = []
    for ln in lines:
        rec = _parse(ln)
        if rec is not None and _age_ok(rec, cutoff):
            kept.append(ln)
    kept = kept[-MAX_RECORDS:]
    if kept == lines:
        return
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text("".join(k + "\n" for k in kept), encoding="utf-8")
        os.replace(tmp, p)
    except BaseException:
        try:                                # a failed trim leaves the old file whole and no temp file
            tmp.unlink()
        except OSError:
            pass
        raise


def trim(now: datetime | None = None) -> bool:
    """Apply the retention rules now. Never raises; True when it ran."""
    try:
        with _LOCK:
            _trim_locked(path(), now)
        return True
    except Exception as e:  # noqa: BLE001
        _warn_once(f"{type(e).__name__}: {e}")
        return False


def append(record: dict) -> bool:
    """Add one record. Never raises; returns whether it was written."""
    try:
        rec = make_record(**{k: record.get(k, "") for k in FIELDS if k not in ("v",)}) \
            if isinstance(record, dict) else None
        if rec is None:
            return False
        line = json.dumps(rec, ensure_ascii=True, separators=(",", ":")) + "\n"
        p = path()
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8", newline="\n") as f:
                f.write(line)
            _state["appends"] += 1
            if _state["appends"] == 1 or _state["appends"] % _TRIM_EVERY == 0:
                _trim_locked(p)
        return True
    except Exception as e:  # noqa: BLE001
        _warn_once(f"{type(e).__name__}: {e}")
        return False


def tail(n: int = 20) -> list[dict]:
    """The last n readable records, oldest first. Unreadable lines are
    skipped. Never raises (an unreadable or missing file gives [])."""
    try:
        with _LOCK:
            text = path().read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return []
    out = []
    for ln in text.splitlines():
        rec = _parse(ln)
        if rec is not None:
            out.append(rec)
    return out[-max(0, int(n)):] if n else []


def scope_from_inputs(skill, input_map: dict) -> tuple[str, str]:
    """(entity, year) as the user picked them on the form, '' when the skill
    has no such input. Entity: 'entity', else the first of 'entities', else
    'entity_a + entity_b'. Year: the financial year, else the assessment year."""
    def _v(name):
        return str((input_map or {}).get(name) or "").strip()
    entity = _v("entity")
    if not entity:
        first = [x.strip() for x in _v("entities").split(",") if x.strip()]
        entity = first[0] if first else ""
    if not entity:
        a, b = _v("entity_a"), _v("entity_b")
        entity = " + ".join(x for x in (a, b) if x)
    year = _v("fy") or _v("ay")
    return entity, year
