"""SEC-21: the bank corpus file is found by pattern, never named by digits in the source.
Synthetic files only."""
from __future__ import annotations

import re
from pathlib import Path

from tests.test_bank_skills import _find_bob_pdf, _present

GOOD = "11112222333344"


def _touch(folder: Path, name: str):
    (folder / name).write_bytes(b"x")


def test_the_good_case_returns_the_one_file(tmp_path):
    _touch(tmp_path, GOOD + ".pdf")
    _touch(tmp_path, "notes.pdf")
    assert _find_bob_pdf(tmp_path) == tmp_path / (GOOD + ".pdf")


def test_an_empty_or_missing_folder_gives_none(tmp_path):
    assert _find_bob_pdf(tmp_path) is None
    assert _find_bob_pdf(tmp_path / "absent") is None


def test_wrong_length_names_give_none(tmp_path):
    _touch(tmp_path, "1111222233334.pdf")       # 13 digits
    _touch(tmp_path, "111122223333445.pdf")     # 15 digits
    assert _find_bob_pdf(tmp_path) is None


def test_names_with_letters_give_none(tmp_path):
    _touch(tmp_path, "A1112222333344.pdf")
    _touch(tmp_path, "1111222233334a.pdf")
    assert _find_bob_pdf(tmp_path) is None


def test_a_non_pdf_with_a_matching_stem_gives_none(tmp_path):
    _touch(tmp_path, GOOD + ".xls")
    _touch(tmp_path, GOOD + ".txt")
    assert _find_bob_pdf(tmp_path) is None


def test_two_matching_pdfs_give_none(tmp_path):
    _touch(tmp_path, GOOD + ".pdf")
    _touch(tmp_path, "55556666777788.pdf")
    assert _find_bob_pdf(tmp_path) is None


def test_none_is_treated_as_absent():
    assert _present(None) is False


def test_the_bank_test_source_holds_no_long_digit_run():
    src = (Path(__file__).resolve().parent / "test_bank_skills.py").read_text(encoding="utf-8")
    assert re.search(r"\d{11,}", src) is None
