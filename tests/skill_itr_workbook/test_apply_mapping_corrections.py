"""
tests/skill_itr_workbook/test_apply_mapping_corrections.py -- round-trip
test for scripts/apply_mapping_corrections.py: write a Mapping Review sheet,
simulate a reviewer filling in Correction cells (one previously-unmapped row
gets a valid tag, one already-mapped row gets re-tagged, one row gets an
invalid tag), run the correction script, and check the row shows up
approved in the updated YAML -- and that the invalid one is reported, not
applied.
"""
from __future__ import annotations

import sys
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

for p in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import parse_eguile as pe  # noqa: E402
import configs  # noqa: E402
import mapping as mapping_engine  # noqa: E402
import write_workbook as ww  # noqa: E402
import apply_mapping_corrections as amc  # noqa: E402
import fixture_gen  # noqa: E402

UNMAPPED_GUID = "ca85f6240bb66d5d38a0e4f40d908525"   # Assets/Misc Holding (unmapped)
RETAG_GUID = "11fc5a724efb9eb161ac039825b3e6dd"        # Income/Bank Interest, was OS_INTEREST_BANK
BAD_GUID = "84486cc24c1d61165711e1b881ef1bab"          # Equity/Capital Account


def _find_row(ws, guid):
    for row in ws.iter_rows(values_only=True):
        if row and row[-1] == guid:
            return row
    return None


def test_correction_round_trip(tmp_path):
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    loaded = configs.load_mapping(FIXTURES / "syn_ind.mapping.yaml")
    entries = dict(loaded.entries)
    del entries[UNMAPPED_GUID]
    modified = configs.MappingLoadResult(entries=entries, warnings=[])
    result = mapping_engine.resolve_tree(tree, modified)

    wb = Workbook()
    ww.write_mapping_review_sheet(wb, tree, result.resolved, result.unmapped, entries)
    ws = wb["Mapping Review"]

    # Reviewer fills in three Correction cells.
    for row in ws.iter_rows():
        guid_cell = row[7]
        if guid_cell.value == UNMAPPED_GUID:
            row[6].value = "AL_CASH_BANK"          # previously unmapped -> valid tag
        elif guid_cell.value == RETAG_GUID:
            row[6].value = "OS_INTEREST_SB"         # already-mapped -> re-tagged
        elif guid_cell.value == BAD_GUID:
            row[6].value = "NOT_A_REAL_TAG"         # invalid -> must be rejected

    reviewed_xlsx = tmp_path / "reviewed.xlsx"
    wb.save(reviewed_xlsx)

    # apply_mapping_corrections never touches the committed fixture file --
    # write the "current mapping" copy to tmp_path first.
    mapping_copy = tmp_path / "syn_ind.mapping.yaml"
    mapping_copy.write_text((FIXTURES / "syn_ind.mapping.yaml").read_text(encoding="utf-8"), encoding="utf-8")

    output_yaml = tmp_path / "syn_ind.mapping.updated.yaml"
    applied, invalid, backup_path = amc.apply_corrections(str(mapping_copy), str(reviewed_xlsx), str(output_yaml))

    assert applied == 2
    assert len(invalid) == 1
    assert invalid[0][1] == BAD_GUID
    # An explicit output_yaml is a deliberate dry run: no backup is taken and
    # mapping_copy (the "live" file) must be left untouched.
    assert backup_path is None
    assert mapping_copy.read_text(encoding="utf-8") == (FIXTURES / "syn_ind.mapping.yaml").read_text(encoding="utf-8")

    updated = configs.load_mapping(output_yaml)
    assert updated.entries[UNMAPPED_GUID].tag == "AL_CASH_BANK"
    assert updated.entries[UNMAPPED_GUID].note == amc._APPROVED_NOTE
    assert updated.entries[UNMAPPED_GUID].suggested_by_llm is None
    assert updated.entries[RETAG_GUID].tag == "OS_INTEREST_SB"
    # The invalid correction must not have touched the Equity/Capital Account entry.
    assert updated.entries[BAD_GUID].tag == "EQUITY_CAPITAL"


def test_correction_default_writes_in_place_with_backup(tmp_path):
    """This is the regression test for the root-cause bug: real corrections
    were never reaching the run because the old CLI always required a
    manual review-and-rename step that, in practice, never happened. The
    default (no output_yaml) must now persist straight into mapping_file,
    with a timestamped backup of the pre-correction file, so an approved
    correction is live on the very next run without any manual step."""
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    loaded = configs.load_mapping(FIXTURES / "syn_ind.mapping.yaml")
    entries = dict(loaded.entries)
    del entries[UNMAPPED_GUID]
    modified = configs.MappingLoadResult(entries=entries, warnings=[])
    result = mapping_engine.resolve_tree(tree, modified)

    wb = Workbook()
    ww.write_mapping_review_sheet(wb, tree, result.resolved, result.unmapped, entries)
    ws = wb["Mapping Review"]
    for row in ws.iter_rows():
        if row[7].value == UNMAPPED_GUID:
            row[6].value = "AL_CASH_BANK"

    reviewed_xlsx = tmp_path / "reviewed.xlsx"
    wb.save(reviewed_xlsx)

    mapping_copy = tmp_path / "syn_ind.mapping.yaml"
    original_text = (FIXTURES / "syn_ind.mapping.yaml").read_text(encoding="utf-8")
    mapping_copy.write_text(original_text, encoding="utf-8")

    applied, invalid, backup_path = amc.apply_corrections(str(mapping_copy), str(reviewed_xlsx))

    assert applied == 1
    assert invalid == []
    assert backup_path is not None
    assert Path(backup_path).read_text(encoding="utf-8") == original_text

    # The live mapping file itself now carries the correction -- no manual
    # rename step required.
    reloaded = configs.load_mapping(mapping_copy)
    assert reloaded.entries[UNMAPPED_GUID].tag == "AL_CASH_BANK"
    assert reloaded.entries[UNMAPPED_GUID].note == amc._APPROVED_NOTE

    # And it survives a fresh resolve on the next run, taking precedence
    # over whatever heuristic/inherited tag would otherwise have applied --
    # this is the "approved always outranks heuristic" guarantee, proven
    # end-to-end through the real persistence path rather than asserted in
    # the abstract.
    known_paths = {n.guid: n.path for n in tree.all_nodes() if n.guid}
    reloaded_for_run = configs.load_mapping(mapping_copy, known_paths=known_paths)
    next_run_result = mapping_engine.resolve_tree(tree, reloaded_for_run)
    assert next_run_result.resolved[UNMAPPED_GUID].tag == "AL_CASH_BANK"
