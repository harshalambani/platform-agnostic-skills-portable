"""tests/test_cc25_sapphiro_filename.py -- CC-25 (reworked by CC-28): card digits in a filename come from the card list."""
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


from agents.cc_cards import Card  # noqa: E402

CARDS = [Card("ICICI", "Sapphiro", ("1111",), "", True), Card("ICICI", "Amazon", ("3333",), "", True)]


def test_no_card_digits_live_in_the_code():                                  # NEGATIVE
    # CC-28: digits come from the card list; a bare digit filename is never filed by a built-in pattern
    assert sort.extract_bank_and_card_type("ICICI_3333XXXXXXXX0000_statement.pdf") == (None, None) or         sort.extract_bank_and_card_type("ICICI_3333XXXXXXXX0000_statement.pdf")[1] != "Sapphiro"
    assert sort.extract_bank_and_card_type("9999_statement.pdf") == (None, None)


def test_a_listed_amazon_number_is_never_filed_as_sapphiro(tmp_path):        # NEGATIVE
    b, c, why = sort.choose_folder_why("ICICI_3333_statement.pdf", str(tmp_path / "x.pdf"), CARDS)
    assert (b, c, why) == ("ICICI", "Amazon", None)


def test_the_sapphiro_word_still_files_without_digits():
    assert sort.extract_bank_and_card_type("icici sapphiro april.pdf") == ("ICICI", "Sapphiro")
