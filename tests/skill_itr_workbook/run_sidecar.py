"""Test helper (ITR-14): write the ITR Workbook run's account-list sidecar."""
from __future__ import annotations

import json
from pathlib import Path


def write_run(data_root, entity, unmapped=(), resolved=(), held=(), fy="", name="out.xlsx"):
    """Write <outputs>/<name>-<ENTITY>-run-accounts.json. Each argument is an
    iterable of guids (or (guid, path) pairs)."""
    def _acc(items, state):
        out = []
        for it in items:
            g, p = (it, f"Assets/{it}") if isinstance(it, str) else it
            out.append({"guid": g, "path": p, "state": state})
        return out
    outd = Path(data_root) / "outputs"
    outd.mkdir(parents=True, exist_ok=True)
    p = outd / f"{name}-{entity}-run-accounts.json"
    p.write_text(json.dumps({"version": 1, "entity": entity, "fy": fy,
                             "accounts": _acc(resolved, "resolved") + _acc(held, "held")
                             + _acc(unmapped, "unmapped")}), encoding="utf-8")
    return p


def leaves_of(accounts):
    """Guids of the accounts that are nobody's parent."""
    parents = {a.parent_id for a in accounts if a.parent_id}
    return [a.id for a in accounts if a.id not in parents]
