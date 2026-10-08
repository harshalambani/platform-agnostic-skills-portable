"""Registered GnuCash books are reachable by the review-tab path guard."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ui import _config  # noqa: E402
from ui import _safe_paths as sp  # noqa: E402
from ui.tabs import tds_journal_review as tjr  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    data = tmp_path / "PASk" / "Data"
    (data / "itr").mkdir(parents=True)
    out = tmp_path / "PASk" / "Outputs"
    out.mkdir()
    books = tmp_path / "GnuCash" / "Profile" / "Fam"
    books.mkdir(parents=True)
    other = tmp_path / "GnuCash" / "Elsewhere"
    other.mkdir(parents=True)
    book = books / "fam.gnucash"
    book.write_text("x")
    (books / "notes.txt").write_text("x")
    (other / "stray.gnucash").write_text("x")
    monkeypatch.setattr(_config, "data_root_dir", lambda: data)
    monkeypatch.setattr(_config, "output_dir", lambda: out)
    monkeypatch.setattr(_config, "load_portable_config", lambda: {})
    ent = data / "itr" / "entities.yaml"
    return {"ent": ent, "book": book, "books": books, "other": other}


def _register(w, text=None):
    w["ent"].write_text(text or (
        "fam:\n  name: Fam\n  pan: AAAAA0000A\n  status: Firm\n  books:\n    2025-26: '%s'\n" % str(w["book"]).replace("\\", "/")),
        encoding="utf-8")


def test_registered_book_is_accepted(world):
    _register(world)
    got = sp.resolve_input_file(str(world["book"]), (".gnucash",))
    assert os.path.normcase(str(got)) == os.path.normcase(str(world["book"].resolve()))


def test_unregistered_sibling_folder_still_refused(world):      # NEGATIVE
    _register(world)
    with pytest.raises(sp.UnsafePathError):
        sp.resolve_input_file(str(world["other"] / "stray.gnucash"), (".gnucash",))


def test_malformed_entities_adds_nothing(world):                # NEGATIVE
    _register(world, "fam: [unclosed\n  : : :\n")
    assert world["books"].resolve() not in sp.known_folders()
    with pytest.raises(sp.UnsafePathError):
        sp.resolve_input_file(str(world["book"]), (".gnucash",))


def test_missing_entities_adds_nothing(world):                  # NEGATIVE
    assert world["books"].resolve() not in sp.known_folders()


def test_wrong_extension_in_book_folder_refused(world):         # NEGATIVE
    _register(world)
    with pytest.raises(sp.UnsafePathError):
        sp.resolve_input_file(str(world["books"] / "notes.txt"), (".gnucash",))


def test_review_tab_book_checks_accept_registered_book(world):
    _register(world)
    # BNK-09 check path (gnucash_review) uses exactly this call
    assert sp.resolve_input_file(str(world["book"]), (".gnucash",)).is_file()
    # TDS tab helper
    assert (os.path.normcase(tjr._safe_book(str(world["book"])))
            == os.path.normcase(str(world["book"].resolve())))
    assert tjr._safe_book(str(world["other"] / "stray.gnucash")) == ""   # NEGATIVE
