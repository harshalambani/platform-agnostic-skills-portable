"""
tests/test_gnc_switch_readers.py -- GNC-01 PR 2: the readers use the shared opener.

Synthetic books only, built in tmp_path. The expected output
(tests/gnc_golden_expected.json) was produced by running the OLD readers on
main 95ca407 against the same synthetic book; every switched reader must still
give the identical result on a gzip book, must now also work on a plain book,
must not mutate the shared cached tree, and must raise GncBookUnreadable (never
return found=False / None / an empty result) on an unreadable book.
"""
from __future__ import annotations

import gzip
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for _p in (SRC,
           SRC / "agents" / "skill_itr_workbook" / "scripts",
           SRC / "agents" / "skill_26as_journal" / "scripts",
           SRC / "agents" / "skill_gnucash_intercompany" / "scripts",
           Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gnc_golden  # noqa: E402
from test_gnc_book import _book_xml  # noqa: E402

from agents.gnc_book import GncBookUnreadable, clear_cache, read_xml_root  # noqa: E402

EXPECTED = json.loads((Path(__file__).parent / "gnc_golden_expected.json")
                      .read_text(encoding="utf-8"))
G1 = {k: v for k, v in EXPECTED.items() if k in (
    "gnucash_accounts:load_accounts", "gnucash_accounts:read_account_activity",
    "gnucash_accounts:TargetGuard.from_book",
    "skill_gnucash_coverage.agent:_read_root+_collect_account_dates",
    "skill_year_end_close.closing_check:check_closed",
    "skill_26as_journal.tools:_existing_account_paths")}
G2 = {k: v for k, v in EXPECTED.items() if k not in G1}


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


def _make(tmp_path, kind):
    d = tmp_path / kind
    d.mkdir()
    p = d / "gncsynth.gnucash"
    xml = _book_xml()
    if kind == "gz":
        p.write_bytes(gzip.compress(xml.encode("utf-8")))
    else:
        p.write_text(xml, encoding="utf-8")
    return p


def test_expected_files_cover_every_switched_reader():
    assert len(G1) == 6 and len(G2) == 11


# ---- group 1: readers that already sniffed ---------------------------------

def test_group1_gzip_output_identical_to_main(tmp_path):
    assert gnc_golden.collect_group1(_make(tmp_path, "gz")) == G1


def test_group1_plain_book_works_and_gives_the_same_output(tmp_path):
    assert gnc_golden.collect_group1(_make(tmp_path, "plain")) == G1


# ---- group 2: gzip-only readers now accept plain ----------------------------

def test_group2_gzip_output_identical_to_main(tmp_path):
    assert gnc_golden.collect_group2(_make(tmp_path, "gz")) == G2


def test_group2_plain_book_now_works_and_gives_the_same_output(tmp_path):
    assert gnc_golden.collect_group2(_make(tmp_path, "plain")) == G2


# ---- the shared tree is never mutated by a reader ---------------------------

@pytest.mark.parametrize("kind", ["gz", "plain"])
def test_no_reader_mutates_the_cached_tree(tmp_path, kind):
    p = _make(tmp_path, kind)
    before = ET.tostring(read_xml_root(p))
    gnc_golden.collect_group1(p)
    gnc_golden.collect_group2(p)
    after = ET.tostring(read_xml_root(p))
    assert before == after
    assert read_xml_root(p) is read_xml_root(p)


# ---- negatives: an unreadable book raises, never an empty/None/found=False ---

def _bad_books(tmp_path):
    out = {}
    out["missing"] = tmp_path / "missing.gnucash"
    e = tmp_path / "empty.gnucash"
    e.write_bytes(b"")
    out["empty"] = e
    j = tmp_path / "junk.gnucash"
    j.write_bytes(b"not xml")
    out["junk"] = j
    good = gzip.compress(_book_xml().encode())
    t = tmp_path / "trunc.gnucash"
    t.write_bytes(good[: len(good) // 2])
    out["truncated"] = t
    return out


def _readers():
    from agents import gnucash_accounts as ga
    from agents.skill_gnucash_coverage import agent as cov
    from agents.skill_26as_journal import tools as t26
    from agents.skill_year_end_close import closing_check as cc
    import reconcile_intercompany as ric
    import parse_gnucash as pg
    import build_tds_journals as btj
    from agents.skill_gnucash_pipeline import agent as pipe
    from agents.skill_gnucash_reconciler import agent as rec
    from agents.skill_gnucash_xml_extractor import agent as ext
    return {
        "ga.load_accounts": lambda p: ga.load_accounts(p),
        "ga.read_account_activity": lambda p: ga.read_account_activity(p),
        "ga.TargetGuard.from_book": lambda p: ga.TargetGuard.from_book(p),
        "cov._read_root": lambda p: cov._read_root(Path(p)),
        "cc._read_book": lambda p: cc._read_book(Path(p)),
        "t26._existing_account_paths": lambda p: t26._existing_account_paths(str(p)),
        "ric.load_book": lambda p: ric.load_book(str(p)),
        "pipe._get_gnucash_account_balance": lambda p: pipe._get_gnucash_account_balance(str(p), "Demo Bank"),
        "rec.parse_gnucash_for_reconcile": lambda p: rec.parse_gnucash_for_reconcile(str(p)),
        "ext.parse_gnucash_file": lambda p: ext.parse_gnucash_file(str(p)),
        "pg.parse_book": lambda p: pg.parse_book(Path(p)),
        "btj.load_accounts": lambda p: btj.load_accounts(Path(p)),
        "btj.load_account_balances": lambda p: btj.load_account_balances(Path(p)),
        "btj.load_book_journals_by_num": lambda p: btj.load_book_journals_by_num(Path(p)),
        "btj.load_book_txns": lambda p: btj.load_book_txns(Path(p)),
        "btj.monthly_debits_for_account": lambda p: btj.monthly_debits_for_account(Path(p), "Assets:Bank:Demo Bank Savings"),
    }


@pytest.mark.parametrize("case", ["missing", "empty", "junk", "truncated"])
def test_every_switched_reader_raises_on_an_unreadable_book(tmp_path, case):
    bad = _bad_books(tmp_path)[case]
    for name, fn in _readers().items():
        try:
            res = fn(bad)
        except GncBookUnreadable:
            continue
        except FileNotFoundError:
            # the intercompany/extractor existence checks keep their own
            # "File not found" for a path that does not exist
            assert case == "missing", name
            continue
        pytest.fail(f"{name} returned {res!r} for an unreadable book ({case})")


def test_pipeline_balance_never_reports_found_false_for_an_unreadable_book(tmp_path):
    from agents.skill_gnucash_pipeline import agent as pipe
    bad = _bad_books(tmp_path)["junk"]
    with pytest.raises(GncBookUnreadable):
        pipe._get_gnucash_account_balance(str(bad), "Demo Bank")


def test_closing_check_still_gives_its_own_clear_verdict_on_an_unreadable_book(tmp_path):
    from agents.skill_year_end_close import closing_check as cc
    res = cc.check_closed(_bad_books(tmp_path)["junk"], 2026)
    assert res.ok is False
    assert "could not be read" in " ".join(res.problems if hasattr(res, "problems") else [str(res)])
