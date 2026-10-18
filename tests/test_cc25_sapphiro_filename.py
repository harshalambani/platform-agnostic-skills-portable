"""tests/test_cc25_sapphiro_filename.py -- CC-25: 4315 is the Amazon Visa's first group, never Sapphiro."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SORT_SCRIPT = ROOT / "src" / "agents" / "skill_cc_sort" / "scripts" / "extract_sort_cc_pdfs.py"
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_spec = importlib.util.spec_from_file_location("extract_sort_cc_pdfs_cc25", SORT_SCRIPT)
sort = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sort
_spec.loader.exec_module(sort)


def test_a_filename_with_4315_is_never_filed_as_sapphiro():                  # NEGATIVE
    assert sort.extract_bank_and_card_type("ICICI_4315XXXXXXXX0000_statement.pdf") != ("ICICI", "Sapphiro")
    assert sort.extract_bank_and_card_type("4315_statement.pdf") != ("ICICI", "Sapphiro")


def test_a_filename_with_3747_is_still_sapphiro():
    assert sort.extract_bank_and_card_type("ICICI_3747XXXXXXX000_statement.pdf") == ("ICICI", "Sapphiro")
    assert sort.extract_bank_and_card_type("icici sapphiro april.pdf") == ("ICICI", "Sapphiro")
