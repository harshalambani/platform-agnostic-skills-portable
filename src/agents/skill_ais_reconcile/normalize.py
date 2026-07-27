"""
normalize.py — turn a decrypted Income-Tax AIS JSON dict into a flat, typed
intermediate representation.

The AIS JSON is schema-driven at the row level: `partB.sections[*].elements[*]`
carry both `l1`/`l2` blocks, and each block declares its own field names/types
via `columnLabel` rather than using a fixed set of columns across the whole
file. This module reads that descriptor info at runtime and coerces each
`columnData` row accordingly — it must NOT hardcode field names, because
sectionKeys/categories are open-ended (new AIS sections get added by the tax
department over time).

Real AIS exports use TWO different `columnData` row shapes, and either level
(l1/l2) may use either:
  - POSITIONAL: each row is a plain list of scalar values. `columnLabel` is
    either a list of {field, name, type, seq, displayFeedback, ...} dict
    descriptors (seq = the column index into the row; only a subset of
    columns may be described) or a list of plain header strings aligned 1:1
    by position. `columnDataType` (when present) is a parallel list of type
    strings aligned to the row indices and is the authoritative coercion
    type, taking priority over a descriptor's own `type`.
  - DICT-KEYED: each row is already a {field: value} dict (this shows up in
    hand-built/legacy fixtures; kept supported for backward compatibility).

Nothing here talks to the filesystem or network; `normalize()` is a pure
function over an already-decrypted dict (see decrypt.decrypt_ais).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


@dataclass
class AisRow:
    """One row from one level (l1 or l2) of one element, with its fields
    coerced per the file's own columnLabel descriptors."""
    section_key: str
    section_title: str
    category: str                     # element `title`, e.g. "Interest from deposit"
    info_src_id: str | None
    level: str                        # "l1" or "l2"
    fields: dict[str, Any]            # coerced business fields keyed by `field` name
    status: str | None = None
    trans_feedback: Any = None
    feedback_editable_field: str | None = None


@dataclass
class SectionSummary:
    section_key: str
    category: str
    row_count: int


@dataclass
class NormalizedAis:
    json_version: str | None
    download_date: str | None
    logged_in_pan: str | None
    part_a: dict[str, str]
    rows: list[AisRow] = field(default_factory=list)
    summary: list[SectionSummary] = field(default_factory=list)


def _coerce(raw_value: Any, decl_type: str | None) -> Any:
    """Coerce a single columnData value per its declared `type`. Defensive:
    blank/None/unparseable values degrade to None rather than raising, since
    AIS rows routinely leave optional fields blank."""
    if raw_value is None:
        return None
    if isinstance(raw_value, str) and raw_value.strip() == "":
        return None

    t = (decl_type or "").strip().lower()
    if t == "decimal":
        try:
            return Decimal(str(raw_value))
        except (InvalidOperation, ValueError):
            return None
    if t == "date":
        # Dates are surfaced as the raw string (format varies by section); a
        # downstream phase that needs a real date object can parse this with
        # the section's known format. Returning None on non-string junk only.
        return str(raw_value) if raw_value is not None else None
    # "string" and any unknown/future type: pass through as-is (stringified
    # only if it's some non-primitive so the row stays JSON/CSV friendly).
    if isinstance(raw_value, (str, int, float, bool)):
        return raw_value
    return str(raw_value)


def _field_descriptors(column_label: Any) -> list[dict]:
    """Return only the dict-shaped descriptors from a columnLabel list, in
    order — some columnLabel entries in header-like sections are plain
    strings rather than {field, name, type, ...} dicts, and those carry no
    field/type info to drive coercion so they're skipped."""
    if not isinstance(column_label, list):
        return []
    return [d for d in column_label if isinstance(d, dict)]


def _feedback_editable_field(descriptors: list[dict]) -> str | None:
    for d in descriptors:
        if d.get("displayFeedback") is True:
            return d.get("field")
    return None


def _positional_maps(column_label: Any, column_data_type: Any) -> tuple[dict[int, str], dict[int, str | None], str | None]:
    """Build (name_by_index, type_by_index, editable_field) for POSITIONAL
    columnData rows (real AIS files: each row is a list of scalars, not a
    field-keyed dict).

    columnLabel comes in two forms and either level (l1/l2) may use either:
      - dict-descriptors (typically l1): {field, name, type, seq,
        displayFeedback, ...}. `seq` is the column index into the row, and
        only a SUBSET of the row's columns may be described.
      - plain-string headers (typically l2): a list of strings aligned 1:1
        (positionally) with the row values.

    columnDataType, when present, is a parallel list of type strings aligned
    to the row indices and takes priority over a descriptor's own `type`
    (the real file's columnDataType is the authoritative positional key)."""
    name_by_index: dict[int, str] = {}
    type_by_index: dict[int, str | None] = {}
    editable_field: str | None = None

    if isinstance(column_label, list) and column_label and all(
        isinstance(d, dict) for d in column_label
    ):
        for d in column_label:
            seq = d.get("seq")
            fld = d.get("field")
            if isinstance(seq, int) and fld:
                name_by_index[seq] = fld
                type_by_index[seq] = d.get("type")
                if d.get("displayFeedback") is True:
                    editable_field = fld
    elif isinstance(column_label, list):
        for i, label in enumerate(column_label):
            if isinstance(label, str) and label.strip():
                name_by_index[i] = label

    if isinstance(column_data_type, list):
        for i in name_by_index:
            if i < len(column_data_type) and column_data_type[i]:
                type_by_index[i] = column_data_type[i]

    return name_by_index, type_by_index, editable_field


def _normalize_positional_row(row: list, name_by_index: dict[int, str],
                               type_by_index: dict[int, str | None]) -> tuple[dict[str, Any], str | None, Any]:
    """Coerce a POSITIONAL columnData row (list of scalars) into a fields
    dict, pulling status/transFeedback out into their own return values
    rather than leaving them in `fields`."""
    fields = {
        name_by_index[i]: _coerce(val, type_by_index.get(i))
        for i, val in enumerate(row)
        if name_by_index.get(i)
    }
    status = fields.pop("status", None)
    trans_feedback = fields.pop("transFeedback", None)
    return fields, status, trans_feedback


def _normalize_level(level_block: dict | None, section_key: str, section_title: str,
                      category: str, info_src_id: str | None, level: str) -> list[AisRow]:
    if not level_block or not isinstance(level_block, dict):
        return []

    column_label = level_block.get("columnLabel")
    descriptors = _field_descriptors(column_label)
    type_by_field = {d.get("field"): d.get("type") for d in descriptors if d.get("field")}
    dict_editable_field = _feedback_editable_field(descriptors)

    name_by_index, type_by_index, positional_editable_field = _positional_maps(
        column_label, level_block.get("columnDataType")
    )
    editable_field = positional_editable_field or dict_editable_field

    rows_data = level_block.get("columnData")
    if not isinstance(rows_data, list):
        return []

    out: list[AisRow] = []
    for row in rows_data:
        if isinstance(row, list):
            # Real AIS shape: columnData rows are positional lists of
            # scalars; columnLabel/columnDataType supply the index -> field
            # mapping. This is the shape a real portal export uses.
            coerced_fields, status, trans_feedback = _normalize_positional_row(
                row, name_by_index, type_by_index
            )
        elif isinstance(row, dict):
            # Legacy/synthetic shape: columnData rows are already field-keyed
            # dicts. Kept for backward compatibility with hand-built fixtures.
            coerced_fields = {
                k: _coerce(v, type_by_field.get(k))
                for k, v in row.items()
                if k not in ("status", "transFeedback")
            }
            status = row.get("status")
            trans_feedback = row.get("transFeedback")
        else:
            continue

        out.append(AisRow(
            section_key=section_key,
            section_title=section_title,
            category=category,
            info_src_id=info_src_id,
            level=level,
            fields=coerced_fields,
            status=status,
            trans_feedback=trans_feedback,
            feedback_editable_field=editable_field,
        ))
    return out


def _part_a_dict(part_a: Any) -> dict[str, str]:
    """partA carries parallel columnLabel/columnData arrays of plain strings
    (PAN, Aadhaar, Name, DOB, Mobile, Email, Address, ...), not the
    field-descriptor-object shape partB uses. Zip them into a simple dict."""
    if not isinstance(part_a, dict):
        return {}
    labels = part_a.get("columnLabel") or []
    data = part_a.get("columnData") or []
    if not isinstance(labels, list) or not isinstance(data, list):
        return {}
    out: dict[str, str] = {}
    for label, value in zip(labels, data):
        key = label if isinstance(label, str) else (
            label.get("name") or label.get("field") if isinstance(label, dict) else None
        )
        if key:
            out[str(key)] = value
    return out


def normalize(ais: dict) -> NormalizedAis:
    """Flatten a decrypted AIS dict into a NormalizedAis: metadata + a flat
    list of typed rows (one per l1/l2 element row) + a per-(section,category)
    row-count summary. Schema-driven — reads field names/types from the
    file's own columnLabel descriptors rather than a hardcoded field list, so
    unknown/future sectionKeys still normalize correctly (they just won't be
    specially interpreted downstream)."""
    metadata = ais.get("metadata") or {}
    part_a = _part_a_dict(ais.get("partA"))

    rows: list[AisRow] = []
    counts: dict[tuple[str, str], int] = {}

    part_b = ais.get("partB") or {}
    sections = part_b.get("sections")
    if not isinstance(sections, list):
        sections = []

    for section in sections:
        if not isinstance(section, dict):
            continue
        section_key = section.get("sectionKey") or ""
        section_title = section.get("title") or section.get("heading") or ""

        elements = section.get("elements")
        if not isinstance(elements, list):
            continue

        for element in elements:
            if not isinstance(element, dict):
                continue
            category = element.get("title") or ""
            info_src_id = element.get("infoSrcId")

            for level_name in ("l1", "l2"):
                level_rows = _normalize_level(
                    element.get(level_name), section_key, section_title,
                    category, info_src_id, level_name,
                )
                rows.extend(level_rows)
                if level_rows:
                    key = (section_key, category)
                    counts[key] = counts.get(key, 0) + len(level_rows)

    summary = [
        SectionSummary(section_key=k[0], category=k[1], row_count=v)
        for k, v in counts.items()
    ]

    return NormalizedAis(
        json_version=metadata.get("jsonVersion"),
        download_date=metadata.get("downloadDate"),
        logged_in_pan=metadata.get("loggedInPan"),
        part_a=part_a,
        rows=rows,
        summary=summary,
    )
