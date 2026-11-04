"""
run_accounts.py -- ITR-14: the list of accounts one ITR Workbook run needed.

The run writes a small JSON file next to its output on EVERY run (a fully
mapped one too), so Review Mapping can list exactly the accounts the run
reported and nothing else. It records the entity, the FY and, per account, the
guid, the path and whether the run resolved it, held it or left it unmapped.
The workbook itself is never parsed for this.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SUFFIX = "-run-accounts.json"
RESOLVED, HELD, UNMAPPED = "resolved", "held", "unmapped"
VERSION = 1


def file_name(output_path: str, entity_key: str | None) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", entity_key or "")
    infix = f"-{safe}" if safe else ""
    return f"{output_path}{infix}{SUFFIX}"


def build(result, entity_key: str | None, fy: str | None) -> dict:
    """The sidecar payload from a mapping ResolutionResult."""
    accounts = []
    for guid, leaf in (result.resolved or {}).items():
        accounts.append({"guid": guid, "path": leaf.path, "state": RESOLVED})
    for leaf in result.unmapped or []:
        if not leaf.guid:
            continue
        accounts.append({"guid": leaf.guid, "path": leaf.path,
                         "state": HELD if leaf.held else UNMAPPED})
    return {"version": VERSION, "entity": entity_key or "", "fy": fy or "", "accounts": accounts}


def write(result, output_path: str, entity_key: str | None, fy: str | None) -> str | None:
    """Write the sidecar. Never raises; returns the path or None."""
    if result is None:
        return None
    try:
        p = Path(file_name(output_path, entity_key))
        p.write_text(json.dumps(build(result, entity_key, fy), indent=1), encoding="utf-8")
        return str(p)
    except Exception:  # noqa: BLE001
        return None


def read(path) -> dict | None:
    """The sidecar as a dict, or None when unreadable or not this format."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        return None
    return data
