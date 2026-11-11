"""PIPE-12 negatives: the real-corpus config helper must never turn a missing or broken setting into a pass.
Synthetic names and tmp_path only; runs in CI."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import real_corpus_config as rcc  # noqa: E402

ENV = rcc.ENV_VAR


def _cfg(tmp_path, text, touch=()):
    for name in touch:
        (tmp_path / name).write_text("x", encoding="utf-8")
    f = tmp_path / "corpus.cfg"
    f.write_text(text, encoding="utf-8")
    return {ENV: str(f)}


def test_unset_skips_with_reason_never_returns_pairs():
    for env in ({}, {ENV: ""}, {ENV: "   "}):
        pairs, why = rcc.need_pairs(env)
        assert pairs is None and ENV in why
        r, why2 = rcc.need_richest(env)
        assert r is None and ENV in why2


def test_missing_config_file_skips_with_reason(tmp_path):
    env = {ENV: str(tmp_path / "nope.cfg")}
    pairs, why = rcc.need_pairs(env)
    assert pairs is None and "does not exist" in why
    assert rcc.need_richest(env)[0] is None


def test_listed_book_missing_names_the_book(tmp_path):
    env = _cfg(tmp_path, "pair = Ex.html | Ex.gnucash\n", touch=["Ex.html"])
    pairs, why = rcc.need_pairs(env)
    assert pairs is None
    assert "Ex.gnucash" in why and "Ex.html" not in why


def test_listed_report_missing_names_the_report(tmp_path):
    env = _cfg(tmp_path, "pair = Ex.html | Ex.gnucash\n", touch=["Ex.gnucash"])
    pairs, why = rcc.need_pairs(env)
    assert pairs is None
    assert "Ex.html" in why and "Ex.gnucash" not in why


def test_one_good_pair_and_one_missing_still_skips(tmp_path):
    env = _cfg(tmp_path, "pair = A.html | A.gnucash\npair = B.html | B.gnucash\n",
               touch=["A.html", "A.gnucash", "B.html"])
    pairs, why = rcc.need_pairs(env)
    assert pairs is None and "B.gnucash" in why


def test_richest_missing_file_and_missing_line_skip(tmp_path):
    env = _cfg(tmp_path, "richest = Gone.gnucash\n")
    r, why = rcc.need_richest(env)
    assert r is None and "Gone.gnucash" in why
    env2 = _cfg(tmp_path, "pair = A.html | A.gnucash\n")
    r2, why2 = rcc.need_richest(env2)
    assert r2 is None and "richest" in why2


@pytest.mark.parametrize("text", ["", "# only a comment\n\n"])
def test_empty_pair_list_skips_not_vacuous_pass(tmp_path, text):
    pairs, why = rcc.need_pairs(_cfg(tmp_path, text))
    assert pairs is None
    assert "no 'pair' lines" in why


@pytest.mark.parametrize("line", [
    "pair Ex.html | Ex.gnucash",       # no '='
    "pair = ",                         # empty value
    "pair = Ex.html",                  # no book
    "pair = Ex.html | ",               # empty book
    "pair = a | b | c",                # too many parts
    "bogus = x",                       # unknown key
    "richest = A.gnucash\nrichest = B.gnucash",  # twice
])
def test_malformed_line_raises_never_passes(tmp_path, line):
    env = _cfg(tmp_path, line + "\n", touch=["Ex.html", "Ex.gnucash"])
    with pytest.raises(rcc.ConfigError):
        rcc.need_pairs(env)


def test_good_config_resolves_relative_to_config_folder(tmp_path):
    env = _cfg(tmp_path, "# c\npair = Ex.html | Ex.gnucash  # trailing\nrichest = Ex.gnucash\n",
               touch=["Ex.html", "Ex.gnucash"])
    pairs, why = rcc.need_pairs(env)
    assert why == "" and pairs == [(tmp_path / "Ex.html", tmp_path / "Ex.gnucash")]
    assert rcc.need_richest(env)[0] == tmp_path / "Ex.gnucash"


def test_unset_env_makes_the_real_tests_skip_not_pass():
    import os
    if os.environ.get(ENV):
        pytest.skip("developer has a real config set")
    assert rcc.need_pairs()[0] is None


def test_no_placeholder_person_names_left_in_real_corpus_test():
    src = (HERE / "test_parse_gnucash.py").read_text(encoding="utf-8")
    for bad in ("DaveDoe", "Doe", "_REAL_PAIRS", "GNUCashReports", '"Data"'):
        assert bad not in src, bad
