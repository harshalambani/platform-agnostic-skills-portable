"""UI-54 (2/3): the last Coverage check per book. Synthetic books only, all under pytest's tmp."""
# ruff: noqa: F811  (imported fixtures are used by name)
from __future__ import annotations

import hashlib
import json
import sys
import threading
import warnings
from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src"), str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_gnucash_coverage import agent as cov  # noqa: E402
from tests.test_gnucash_coverage import (  # noqa: E402,F401  (fixtures + helpers)
    _entities_path, _run_scan, _scan, fy2425_book, fy2526_book, scope_book, unregistered_book,
)
from ui import _config, _coverage_last as CL  # noqa: E402

TODAY = date(2026, 5, 1)


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "dataroot"
    monkeypatch.setattr(_config, "data_root_dir", lambda: root)
    monkeypatch.setitem(CL._state, "warned", False)
    return root / "logs" / "coverage-last.json"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _cells(xlsx: Path):
    wb = load_workbook(xlsx)
    return {ws.title: [[c.value for c in row] for row in ws.iter_rows()] for ws in wb.worksheets}


# ------------------------------------------------------------------ the store ---

def test_the_store_lives_under_the_pytest_data_root(tmp_path_factory):
    assert tmp_path_factory.getbasetemp().resolve() in CL.path().resolve().parents


def test_a_run_saves_the_last_check_per_entity_and_year(fy2425_book, tmp_path, store):
    ep = _entities_path(tmp_path, fy2425_book)
    _run_scan([fy2425_book], tmp_path, entities_path=ep)
    entries = CL.load()
    assert list(entries) == ["TestPersonCoverage|2024-25"]
    e = entries["TestPersonCoverage|2024-25"]
    assert (e["entity"], e["year"], e["books"], e["include_other"]) == ("TestPersonCoverage", "2024-25", 1, False)
    assert datetime.fromisoformat(e["checked"])
    assert e["accounts_checked"] >= 2 and e["gap_accounts"] >= 1
    bank = e["accounts"]["Assets:Bank:TestBank"]
    assert "2024-10" in bank["gap_months"]
    assert e["gap_months"] == sum(len(a["gap_months"]) + len(a["trailing_months"])
                                  for a in e["accounts"].values())
    assert CL.last_for("TestPersonCoverage", "2024-25") == e


def test_accounts_without_gaps_are_counted_but_not_listed(scope_book, tmp_path, store):
    _run_scan([scope_book], tmp_path)
    (e,) = CL.load().values()
    scan = _scan([scope_book], tmp_path)
    assert e["accounts_checked"] == len(scan.results)
    assert set(e["accounts"]) == {r.account_path for r in scan.results if r.has_gaps}
    assert e["gap_accounts"] == len(e["accounts"])


def test_a_book_no_entity_owns_is_keyed_by_its_label(unregistered_book, tmp_path, store):
    scan = _scan([unregistered_book], tmp_path)
    (k, e), = CL.entries_from_scan(scan).items()
    assert e["entity"] == scan.results[0].entity and k == f"{e['entity']}|{e['year']}"


def test_a_later_check_replaces_the_earlier_one_and_keeps_other_books(fy2425_book, fy2526_book, tmp_path, store):
    ep = _entities_path(tmp_path, fy2425_book, fy2526_book)
    CL.record_scan(_scan([fy2425_book], tmp_path, entities_path=ep), now=datetime(2026, 1, 1))
    CL.record_scan(_scan([fy2526_book], tmp_path, entities_path=ep), now=datetime(2026, 2, 1))
    assert set(CL.load()) == {"TestPersonCoverage|2024-25", "TestPersonCoverage|2025-26"}
    CL.record_scan(_scan([fy2425_book], tmp_path, entities_path=ep), now=datetime(2026, 3, 1))
    got = CL.load()
    assert got["TestPersonCoverage|2024-25"]["checked"].startswith("2026-03-01")      # replaced
    assert got["TestPersonCoverage|2025-26"]["checked"].startswith("2026-02-01")      # untouched


def test_an_empty_scan_saves_nothing(tmp_path, store):         # NEGATIVE
    scan = cov.scan_books([str(tmp_path / "no-such.gnucash")], tmp_path / "no.yaml", False)
    assert scan.results == [] and CL.record_scan(scan) is False
    assert not store.exists()


def test_load_never_raises(store):
    assert CL.load() == {} and CL.last_for("x", "y") is None
    store.parent.mkdir(parents=True)
    store.write_text("{not json", encoding="utf-8")
    assert CL.load() == {}
    store.write_text('["a"]', encoding="utf-8")
    assert CL.load() == {}


def test_a_crash_before_the_swap_keeps_the_old_file(fy2425_book, tmp_path, store, monkeypatch):  # NEGATIVE
    ep = _entities_path(tmp_path, fy2425_book)
    scan = _scan([fy2425_book], tmp_path, entities_path=ep)
    assert CL.record_scan(scan, now=datetime(2026, 1, 1))
    before = store.read_bytes()

    def boom(a, b):
        raise OSError("synthetic crash")
    monkeypatch.setattr(CL.os, "replace", boom)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        assert CL.record_scan(scan, now=datetime(2026, 6, 1)) is False
        assert CL.record_scan(scan, now=datetime(2026, 6, 2)) is False
    assert store.read_bytes() == before                           # old file byte-identical
    assert sorted(x.name for x in store.parent.iterdir()) == [store.name]   # and no .tmp left beside it
    assert len([x for x in w if "not saved" in str(x.message)]) == 1


def test_a_failed_temp_write_also_leaves_no_temp_file(fy2425_book, tmp_path, store, monkeypatch):  # NEGATIVE
    ep = _entities_path(tmp_path, fy2425_book)
    scan = _scan([fy2425_book], tmp_path, entities_path=ep)
    assert CL.record_scan(scan, now=datetime(2026, 1, 1))
    before = store.read_bytes()
    real = Path.write_text

    def half(self, *a, **k):
        real(self, "partial", encoding="utf-8")
        raise OSError("synthetic: disk full while writing the temp file")
    with monkeypatch.context() as m:
        m.setattr(Path, "write_text", half)
        assert CL.record_scan(scan, now=datetime(2026, 6, 1)) is False
    assert store.read_bytes() == before
    assert sorted(x.name for x in store.parent.iterdir()) == [store.name]


def test_a_test_that_undoes_its_monkeypatch_still_writes_under_pytest_tmp(
        fy2425_book, tmp_path, monkeypatch, tmp_path_factory):  # NEGATIVE
    ep = _entities_path(tmp_path, fy2425_book)
    scan = _scan([fy2425_book], tmp_path, entities_path=ep)
    monkeypatch.setattr(_config, "data_root_dir", lambda: Path("Z:/not-a-real-place"))
    monkeypatch.undo()                                            # lifts every patch this test made
    base = tmp_path_factory.getbasetemp().resolve()
    assert base in _config.data_root_dir().resolve().parents
    assert CL.record_scan(scan, now=datetime(2026, 1, 1)) is True
    assert base in CL.path().resolve().parents and CL.path().exists()


def test_concurrent_saves_lose_no_entry(fy2425_book, fy2526_book, tmp_path, store):  # NEGATIVE
    ep = _entities_path(tmp_path, fy2425_book, fy2526_book)
    s1 = _scan([fy2425_book], tmp_path, entities_path=ep)
    s2 = _scan([fy2526_book], tmp_path, entities_path=ep)
    ts = [threading.Thread(target=CL.record_scan, args=(s,)) for s in (s1, s2) * 6]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert set(json.loads(store.read_text(encoding="utf-8"))["entries"]) == {
        "TestPersonCoverage|2024-25", "TestPersonCoverage|2025-26"}


# ------------------------------------------------- the skill's output is unchanged ---

def test_report_and_workbook_are_identical_with_the_save_on_off_and_failing(fy2425_book, tmp_path, store, monkeypatch):  # NEGATIVE
    ep = _entities_path(tmp_path, fy2425_book)
    results = {}

    def go(tag):
        out = tmp_path / f"out-{tag}.xlsx"
        reply = cov.run(books=str(fy2425_book), output_path=str(out), entities_path=str(ep))
        results[tag] = (reply.replace(str(out), "<out>"), _cells(out))

    go("on")
    assert store.exists()
    store.unlink()

    with monkeypatch.context() as m:                              # scoped: never lifts the data-root redirect
        m.setattr(cov, "_save_last_check", lambda scan: None)
        go("off")
        assert not store.exists()                                 # off means off

    def boom(*a, **k):
        raise OSError("synthetic: cannot save")
    monkeypatch.setattr(CL.os, "replace", boom)
    go("failing")
    assert results["on"] == results["off"] == results["failing"]
    assert not list(store.parent.glob("*.tmp"))                   # a failed save leaves no temp file


def test_a_save_that_raises_anything_still_returns_the_same_reply(fy2425_book, tmp_path, store, monkeypatch):  # NEGATIVE
    ep = _entities_path(tmp_path, fy2425_book)
    good, _ = _run_scan([fy2425_book], tmp_path, entities_path=ep)
    monkeypatch.setattr(CL, "record_scan", lambda scan: (_ for _ in ()).throw(RuntimeError("boom")))
    again, _ = _run_scan([fy2425_book], tmp_path, entities_path=ep)
    assert again == good


def test_an_error_reply_saves_nothing(tmp_path, store):         # NEGATIVE
    reply = cov.run(books="", output_path=str(tmp_path / "o.xlsx"),
                    entities_path=str(tmp_path / "e.yaml"))
    assert reply.startswith("ERROR") and not store.exists()


# ------------------------------------------------------ read-only and no start-up scan ---

def test_books_are_only_ever_opened_read_only(fy2425_book, fy2526_book, tmp_path, store):  # NEGATIVE
    ep = _entities_path(tmp_path, fy2425_book, fy2526_book)
    books = [fy2425_book, fy2526_book]
    before = {b: _sha(b) for b in books} | {ep: _sha(ep)}
    _run_scan(books, tmp_path, entities_path=ep)
    CL.check_now(books, ep, today=TODAY)
    assert {p: _sha(p) for p in before} == before
    # the only file created next to the books is the report the run was asked to write
    names = {p.name for p in fy2425_book.parent.iterdir()} - {"dataroot"}   # the test data root, elsewhere by design
    assert names == {fy2425_book.name, fy2526_book.name, ep.name, "out.xlsx"}


def test_the_save_writes_only_inside_the_data_root(fy2425_book, tmp_path, store):  # NEGATIVE
    ep = _entities_path(tmp_path, fy2425_book)
    _run_scan([fy2425_book], tmp_path, entities_path=ep)
    assert sorted(p.name for p in store.parent.iterdir()) == ["coverage-last.json"]   # no stray temp file


def test_importing_and_building_the_app_scans_nothing(monkeypatch):  # NEGATIVE
    import importlib
    calls = []
    live = importlib.import_module("agents.skill_gnucash_coverage.agent")   # the module the app would use
    real = live.scan_books
    monkeypatch.setattr(live, "scan_books", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    importlib.reload(CL)
    CL.load()
    CL.last_for("e", "y")
    from ui import webui
    webui.build_app(launch=False)
    assert calls == []


def test_check_now_scans_read_only_saves_and_returns_the_scan(fy2425_book, tmp_path, store, monkeypatch):
    import importlib
    ep = _entities_path(tmp_path, fy2425_book)
    calls = []
    live = importlib.import_module("agents.skill_gnucash_coverage.agent")
    real = live.scan_books
    monkeypatch.setattr(live, "scan_books", lambda *a, **k: (calls.append(a[0]), real(*a, **k))[1])
    scan = CL.check_now([fy2425_book], ep, today=TODAY)
    assert calls == [[str(fy2425_book)]]
    assert scan.books_scanned == 1
    assert "TestPersonCoverage|2024-25" in CL.load()


def test_check_now_with_a_failing_save_still_returns_the_scan(fy2425_book, tmp_path, store, monkeypatch):  # NEGATIVE
    ep = _entities_path(tmp_path, fy2425_book)
    monkeypatch.setattr(CL.os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("x")))
    scan = CL.check_now([fy2425_book], ep, today=TODAY)
    assert scan.books_scanned == 1 and not store.exists()
