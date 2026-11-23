"""
ui/_coverage_last.py -- the last Coverage Gap Detector check per book (UI-54).

The Coverage Gap Detector scans a GnuCash book and reports months with no
transactions. Each check is remembered here so the Home dashboard can show the
last result for each entity and financial year, with its date, without
scanning anything itself.

File: <data root>/logs/coverage-last.json
    {"v": 1, "entries": {"<entity key>|<year>": {...}}}
One entry per entity + financial year; a later check replaces the earlier one.
An entry holds: entity (key, or the label for a book no entity owns), label,
year, checked (ISO time), books (books that contributed), accounts_checked,
gap_accounts, gap_months (total), include_other, and `accounts`: for each
account with a gap, its empty months and the months after its last transaction.

Rules this module keeps:
  * Reading it scans nothing. Only check_now() (the Home "Check now" button,
    built in a later step) and the Coverage skill's own run write to it, and
    both go through the read-only scan in the skill.
  * Writes are atomic (temp file + os.replace) under one process lock, and
    never raise: a failed save leaves the run exactly as it was.
"""
from __future__ import annotations

import json
import os
import threading
import warnings
from datetime import datetime
from pathlib import Path

from ui import _config

SCHEMA = 1
_LOCK = threading.Lock()
_state = {"warned": False}


def path() -> Path:
    return _config.data_root_dir() / "logs" / "coverage-last.json"


def _warn_once(what: str) -> None:
    if _state["warned"]:
        return
    _state["warned"] = True
    try:
        warnings.warn(f"last coverage check not saved: {what}", RuntimeWarning, stacklevel=3)
    except Exception:  # noqa: BLE001
        pass


def entry_key(entity: str, year: str) -> str:
    return f"{entity}|{year}"


def entries_from_scan(scan, now: datetime | None = None) -> dict:
    """{key: entry} for a finished scan, grouped by entity + financial year.
    Pure: reads the scan result only."""
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    groups: dict = {}
    for r in scan.results:
        entity = r.entity_key or r.entity
        g = groups.setdefault(entry_key(entity, r.fy_key), {
            "entity": entity, "label": r.entity, "year": r.fy_key, "checked": stamp,
            "books": set(), "accounts_checked": 0, "gap_accounts": 0, "gap_months": 0,
            "include_other": bool(getattr(scan, "include_other", False)), "accounts": {},
        })
        g["books"].add(r.book)
        g["accounts_checked"] += 1
        if r.has_gaps:
            g["gap_accounts"] += 1
            g["gap_months"] += len(r.gap_months) + len(r.trailing_months)
            g["accounts"][r.account_path] = {
                "section": r.section,
                "gap_months": list(r.gap_months),
                "trailing_months": list(r.trailing_months),
            }
    for g in groups.values():
        g["books"] = len(g["books"])
    return groups


def _read_locked(p: Path) -> dict:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ent = data.get("entries") if isinstance(data, dict) else None
    return ent if isinstance(ent, dict) else {}


def record_scan(scan, now: datetime | None = None) -> bool:
    """Save a finished scan as the last check for each entity + year it covered.
    Never raises; True when the file was written."""
    tmp = None
    try:
        new = entries_from_scan(scan, now)
        if not new:
            return False
        p = path()
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            entries = _read_locked(p)
            entries.update(new)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps({"v": SCHEMA, "entries": entries}, indent=1, sort_keys=True),
                           encoding="utf-8")
            os.replace(tmp, p)
        return True
    except Exception as e:  # noqa: BLE001
        if tmp is not None:
            try:                            # a failed save leaves the old file whole and no temp file
                tmp.unlink()
            except OSError:
                pass
        _warn_once(f"{type(e).__name__}: {e}")
        return False


def load() -> dict:
    """All saved entries, {key: entry}. Scans nothing; {} when there is no file
    or it cannot be read. Never raises."""
    try:
        with _LOCK:
            return _read_locked(path())
    except Exception:  # noqa: BLE001
        return {}


def last_for(entity: str, year: str) -> dict | None:
    """The saved entry for one entity + financial year, or None."""
    return load().get(entry_key(entity, year))


def check_now(book_paths, entities_path=None, include_other: bool = False, today=None):
    """Run the same read-only scan the skill runs, save it as the last check, and return the
    scan. For the Home "Check now" button. Never called at start-up."""
    from agents.skill_gnucash_coverage import agent as _cov  # noqa: PLC0415
    ep = Path(entities_path) if entities_path else None
    paths = [str(b) for b in book_paths]
    scan = _cov.scan_books(paths, ep, include_other, today=today)
    record_scan(scan)
    return scan
