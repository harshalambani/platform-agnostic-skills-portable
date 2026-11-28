"""tests/test_ye02_folder_name_fallback.py -- YE-02: a blank year-end Folder name falls back to the
entity key; a filled one overrides it. Synthetic folders and keys only. The existing guard (the
registered book must sit in GnuCash root \ years \ effective folder) is what makes a wrong
fallback safe, so every refusal below also asserts nothing was created or copied."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import configs                                                       # noqa: E402
from test_ye01_year_end_skill import (                                # noqa: E402,F401
    BOOK, FY, OPEN_BOOK, _dirs, _entity, env, go, snapshot,
)
from tests.test_ui35_entities_all_fields import _call, _profiles, _seed   # noqa: E402
import yaml                                                          # noqa: E402


def _key_folders(env, key):
    g = env.gnc / "20252026" / key
    d = env.drive / FY / key
    g.mkdir(parents=True)
    d.mkdir(parents=True)
    return g, d


def test_blank_folder_name_uses_the_entity_key_when_the_book_is_in_that_folder(env):
    g, d = _key_folders(env, "SYN-K")
    book = g / BOOK
    book.write_bytes(OPEN_BOOK)
    env.write_entities({"SYN-K": _entity("Entity K", "", {FY: str(book)})})
    r = go(env, 1, entity="SYN-K")
    assert r.startswith("Step 1 done") or "Step 1 done" in r, r
    assert (d / BOOK).is_file()


def test_blank_folder_name_with_a_key_that_does_not_hold_the_book_stops_and_creates_nothing(env):
    # NEGATIVE: SYN-A's book is in "Syn A"; the key SYN-A names no such folder.
    env.write_entities({"SYN-A": _entity("Entity A", "", {FY: str(env.book)})})
    before, dirs = snapshot(env), _dirs(env.tmp)
    r = go(env, 1)
    assert r.startswith("Stopped") and "not inside the expected GnuCash folder" in r
    assert _dirs(env.tmp) - dirs <= {str(env.out), str(env.out.parent)}
    assert {k: v for k, v in snapshot(env).items() if k != str(env.out)} == before
    assert not (env.ddir / BOOK).exists()
    assert not env.state.exists()


def test_a_filled_folder_name_wins_over_the_key_even_if_a_key_folder_exists(env):
    _key_folders(env, "SYN-A")                     # a decoy folder named like the key
    r = go(env, 1)                                  # folder_name is "Syn A" (fixture default)
    assert "Step 1 done" in r, r
    assert (env.ddir / BOOK).is_file()              # copied to the filled name's folder
    assert not (env.drive / FY / "SYN-A" / BOOK).exists()   # NEGATIVE: not to the key's folder


def test_a_filled_folder_name_that_does_not_hold_the_book_still_stops_not_the_key(env):
    # NEGATIVE: the key's folder holds the book, but the filled name wins and does not -> stop.
    g, _d = _key_folders(env, "SYN-A")
    book = g / BOOK
    book.write_bytes(OPEN_BOOK)
    env.write_entities({"SYN-A": _entity("Entity A", "Elsewhere", {FY: str(book)})})
    dirs = _dirs(env.tmp)
    r = go(env, 1)
    assert r.startswith("Stopped") and "not inside the expected GnuCash folder" in r
    assert _dirs(env.tmp) - dirs <= {str(env.out), str(env.out.parent)}


def test_a_key_that_is_not_a_valid_folder_name_refuses_when_the_name_is_blank(env):
    bad = "SYN:K"
    assert configs.check_folder_name(bad)
    env.write_entities({bad: _entity("Entity K", "", {FY: str(env.book)})})
    dirs = _dirs(env.tmp)
    r = go(env, 1, entity=bad)
    assert r.startswith("Stopped") and "could not be used as a folder name" in r
    assert _dirs(env.tmp) - dirs <= {str(env.out), str(env.out.parent)}
    assert not (env.ddir / BOOK).exists()


def test_the_same_bad_key_is_fine_when_a_valid_folder_name_is_filled_in(env):
    # NEGATIVE of the refusal above: a filled valid name overrides a bad key.
    bad = "SYN:K"
    env.write_entities({bad: _entity("Entity K", "Syn A", {FY: str(env.book)})})
    assert "Step 1 done" in go(env, 1, entity=bad)


def test_a_valid_key_passes_the_folder_name_check():
    assert configs.check_folder_name("SYN-K") is None


def test_saving_a_blank_folder_name_keeps_it_blank_and_never_writes_the_key(tmp_path):
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, folder_name="").startswith("**Saved**")
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "folder_name" not in raw["SYN-P"]
    assert _profiles(p)["SYN-P"].folder_name == ""


def test_saving_a_filled_folder_name_is_still_written_as_given(tmp_path):
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, folder_name="Syn Folder").startswith("**Saved**")
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-P"]["folder_name"] == "Syn Folder"
