"""
apply_mapping_corrections.py -- LOCAL-ONLY dev helper: reads a reviewed
workbook's "Mapping Review" sheet (write_workbook.write_mapping_review_sheet)
and applies every non-blank Correction cell that names a valid tag back into
an UPDATED copy of the entity's mapping YAML. A touched entry (whether it
was previously unmapped or already mapped with a different tag) is marked
approved -- suggested_by_llm cleared, note replaced -- since a human just
confirmed it via the Correction cell.

Like bootstrap_mappings.py, this script NEVER writes to the real mapping
file in place: it writes a new file at `output_yaml` for you to review and
rename into Data/itr/mappings/<entity>.mapping.yaml yourself. A Correction
cell naming an unknown tag is reported, never applied.

Usage (from the repo root, with the venv active):
    python src/agents/skill_itr_workbook/scripts/apply_mapping_corrections.py \\
        Data/itr/mappings/Harshal.mapping.yaml \\
        reviewed/Harshal-ITR.xlsx \\
        Harshal.mapping.updated.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import openpyxl  # noqa: E402

import tags as tag_vocab  # noqa: E402
from configs import MappingEntry, dump_mapping_entries, load_mapping  # noqa: E402

REVIEW_SHEET = "Mapping Review"
_APPROVED_NOTE = "approved via Mapping Review sheet correction"


def read_corrections(reviewed_xlsx: str) -> tuple[dict, list]:
    """Scan the reviewed workbook's Mapping Review sheet. Returns
    (valid: {guid: (tag, path)}, invalid: [(path, guid, bad_tag)]) for every
    row with a non-blank Correction cell."""
    wb = openpyxl.load_workbook(reviewed_xlsx, data_only=True)
    ws = wb[REVIEW_SHEET]

    valid: dict[str, tuple[str, str]] = {}
    invalid: list[tuple] = []
    in_data_block = False

    for row in ws.iter_rows(values_only=True):
        if not row:
            continue
        if row[0] == "Account path":
            in_data_block = True
            continue
        if not in_data_block:
            continue
        if row[0] is None or (isinstance(row[0], str) and row[0].startswith(("Destination:", "UNMAPPED", "Subtotal"))):
            in_data_block = False
            continue

        path, guid = row[0], row[7] if len(row) > 7 else None
        correction = row[6] if len(row) > 6 else None
        if not guid or correction is None:
            continue
        correction = str(correction).strip()
        if not correction:
            continue

        if tag_vocab.is_valid_tag(correction):
            valid[guid] = (correction, str(path))
        else:
            invalid.append((path, guid, correction))

    return valid, invalid


def apply_corrections_map(
    mapping_file: str,
    corrections: dict,
    output_yaml: str,
    paths: dict | None = None,
) -> tuple[int, list]:
    """Load `mapping_file` (an empty/missing file is treated as zero
    entries -- lets a true cold-start entity, which has no mapping file yet,
    be corrected straight into existence), apply every entry in
    `corrections` ({guid: tag}) that names a valid tag, and write the result
    to `output_yaml`. NEVER touches `mapping_file` in place -- the caller
    decides whether/how `output_yaml` replaces it (see the UI's backup
    discipline in ui/tabs/itr_mapping_review.py).

    A touched entry (whether previously unmapped or already mapped with a
    different tag) is marked approved: `suggested_by_llm` cleared, `note`
    replaced with `_APPROVED_NOTE` -- a human just confirmed it.

    `paths` supplies the account path for a guid that has no existing entry
    in `mapping_file` (a previously-unmapped leaf, whose path is only known
    from the proposed-mappings snippet / parsed tree, not the mapping file
    itself). A guid with neither an existing entry nor a supplied path is
    written with an empty path.

    Returns (count applied, invalid corrections as [(path, guid, bad_tag)]),
    mirroring apply_corrections()'s return shape.
    """
    mp = Path(mapping_file)
    if mp.is_file():
        entries = dict(load_mapping(mapping_file).entries)
    else:
        entries = {}
    paths = paths or {}

    applied = 0
    invalid: list[tuple] = []
    for guid, tag in corrections.items():
        if not guid or tag is None:
            continue
        tag = str(tag).strip()
        if not tag:
            continue
        existing = entries.get(guid)
        path = paths.get(guid) or (existing.path if existing else "")
        if not tag_vocab.is_valid_tag(tag):
            invalid.append((path, guid, tag))
            continue
        entries[guid] = MappingEntry(
            guid=guid,
            path=path,
            tag=tag,
            flags=existing.flags if existing else [],
            note=_APPROVED_NOTE,
            suggested_by_llm=None,
        )
        applied += 1

    Path(output_yaml).write_text(dump_mapping_entries(list(entries.values())), encoding="utf-8")
    return applied, invalid


def apply_corrections(mapping_file: str, reviewed_xlsx: str, output_yaml: str) -> tuple[int, list]:
    """Load `mapping_file`, apply every valid correction from
    `reviewed_xlsx`, and write the result to `output_yaml` (never touches
    `mapping_file` itself). Returns (count applied, invalid corrections).

    Thin wrapper around `apply_corrections_map`: reads the reviewed
    workbook's Correction cells (which already validate tags via
    `read_corrections`), then delegates the entries build + write."""
    valid, invalid = read_corrections(reviewed_xlsx)
    corrections = {guid: tag for guid, (tag, _path) in valid.items()}
    paths = {guid: path for guid, (_tag, path) in valid.items()}
    applied, _ = apply_corrections_map(mapping_file, corrections, output_yaml, paths=paths)
    return applied, invalid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mapping_file", help="The entity's current Data/itr/mappings/<entity>.mapping.yaml")
    parser.add_argument("reviewed_xlsx", help="The ITR workbook, reviewed, with Correction cells filled in")
    parser.add_argument("output_yaml", help="Where to write the updated mapping snippet")
    args = parser.parse_args()

    applied, invalid = apply_corrections(args.mapping_file, args.reviewed_xlsx, args.output_yaml)
    print(f"Applied {applied} correction(s) -> {args.output_yaml}")
    if invalid:
        print(f"{len(invalid)} correction(s) NOT applied (unknown tag):")
        for path, guid, tag in invalid:
            print(f"  - {path} (guid {guid}): {tag!r} is not a valid tag")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
