"""
entity_scope.py -- decide whose file a prior-step output file is.

An `output_file` picker lists files from the shared outputs folder. Several
entities write there, so "the newest file" is not "this entity's file". This
module reads the identity a workbook carries and answers one question: does
this file belong to the selected entity?

The 26AS workbook (skill_26as/scripts/extract_26as_to_xlsx.py) is named after
the INPUT PDF (`<stamp>-<pdf stem>-26AS.xlsx`), which carries no entity, so
the filename cannot be used. Row 2 of every part sheet holds the assessee:

    Assessee Name: <name>  |  PAN: <pan>  |  Financial Year: 2025-26  |  ...

Rules (never guess, never fall back to another entity's file):
  * both PANs known  -> equal PANs, or not this entity's file;
  * otherwise names  -> equal after case/space normalisation, or not;
  * a workbook whose identity cannot be read is nobody's: it matches nobody.

Pure Python plus openpyxl in read-only mode; nothing is written.
"""
from __future__ import annotations

import re
from pathlib import Path

_PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_FY_RE = re.compile(r"Financial Year:\s*(\d{4}-\d{2})")
_NAME_RE = re.compile(r"Assessee Name:\s*(.*?)\s*(?:\||$)")

# `match` globs whose files carry a readable assessee identity.
SCOPABLE_MATCHES = frozenset({"*-26AS.xlsx"})

_cache: dict[tuple[str, float], dict | None] = {}


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").casefold()).strip()


def read_26as_identity(path: str | Path) -> dict | None:
    """{'name','pan','fy'} from a 26AS workbook's row-2 meta line, or None."""
    p = Path(path)
    try:
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return None
    if key in _cache:
        return _cache[key]
    ident: dict | None = None
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            row = next(ws.iter_rows(min_row=2, max_row=2, values_only=True), ())
            meta = " | ".join(str(c) for c in row if c)
        finally:
            wb.close()
        pan = _PAN_RE.search(meta.upper())
        nm = _NAME_RE.search(meta)
        fy = _FY_RE.search(meta)
        if pan or nm:
            ident = {
                "name": nm.group(1).strip() if nm else "",
                "pan": pan.group(0) if pan else "",
                "fy": fy.group(1) if fy else "",
            }
    except Exception:
        ident = None
    _cache[key] = ident
    return ident


def load_entity_identities(entities_yaml: str | Path) -> dict[str, dict]:
    """{entity_key: {'name','pan'}} from an entities.yaml; {} when unreadable."""
    try:
        import yaml
        raw = yaml.safe_load(Path(entities_yaml).read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, f in raw.items():
        if isinstance(f, dict):
            out[str(key)] = {"name": str(f.get("name") or ""), "pan": str(f.get("pan") or "")}
    return out


def identity_matches(workbook: dict | None, entity: dict | None) -> bool:
    """True only when the workbook positively identifies as `entity`."""
    if not workbook or not entity:
        return False
    wpan, epan = (workbook.get("pan") or "").upper(), (entity.get("pan") or "").upper()
    if wpan and epan:
        return wpan == epan
    wn, en = _norm_name(workbook.get("name", "")), _norm_name(entity.get("name", ""))
    return bool(wn and en and wn == en)


def describe_mismatch(workbook: dict | None, entity_key: str, entity: dict | None) -> str:
    """Human message for a workbook that is not the selected entity's."""
    if not workbook:
        return (f"the 26AS workbook does not state whose it is (no assessee name or PAN "
                f"on its sheets), so it cannot be used for {entity_key}")
    who = workbook.get("name") or workbook.get("pan") or "another assessee"
    return (f"the 26AS workbook belongs to {who}, not to {entity_key}"
            f" ({(entity or {}).get('name') or 'unnamed'})")


def filter_choices(
    choices: list[tuple[str, str]],
    entity_key: str | None,
    entities_yaml: str | Path,
    fy: str | None = None,
) -> list[tuple[str, str]]:
    """Keep only this entity's files (and this FY's, when given).

    No entity selected, unknown entity, or an unreadable file -> not offered.
    """
    if not entity_key:
        return []
    ent = load_entity_identities(entities_yaml).get(entity_key)
    if ent is None:
        return []
    kept = []
    for label, value in choices:
        ident = read_26as_identity(value)
        if not identity_matches(ident, ent):
            continue
        if fy and ident and ident.get("fy") and ident["fy"] != fy:
            continue
        kept.append((label, value))
    return kept


def check_26as_owner(
    xlsx_path: str | Path, entity_key: str, name: str, pan: str,
) -> tuple[str, str]:
    """Run-time guard: is this 26AS workbook the selected entity's?

    Returns ("ok", ""), ("mismatch", message) or ("unknown", message). A
    mismatch must stop the run; "unknown" (the workbook states no assessee)
    is reported but cannot be decided either way.
    """
    ent = {"name": name, "pan": pan}
    ident = read_26as_identity(xlsx_path)
    if ident is None:
        return "unknown", (
            "the 26AS workbook does not state whose it is (no assessee name or PAN "
            f"could be read from it), so it could not be checked against {entity_key}")
    if identity_matches(ident, ent):
        return "ok", ""
    return "mismatch", describe_mismatch(ident, entity_key, ent)
