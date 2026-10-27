"""tests/test_cc37_reuse_statement_reads.py -- CC-37: a re-run does not re-read statements that have not
changed. Synthetic data only; the PDFs are placeholder bytes and read_lines is stubbed."""
from __future__ import annotations

import json
from datetime import date

import pytest

from tests.test_cc05_sbi_statements import BLOCK, LEGEND, _head, m
from tests.test_cc30_period_guard import NO_PERIOD

GOOD = _head() + BLOCK + LEGEND
SECRET = "hunter2-synthetic-pw"


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "in" / "SBI-BPCL-Octane"
    d.mkdir(parents=True)
    state = {"reads": 0, "lines": GOOD}

    def fake_read(p):
        state["reads"] += 1
        return state["lines"]
    monkeypatch.setattr(m, "read_lines", fake_read)

    def put(name, body=b"%PDF-1.4 synthetic A"):
        (d / name).write_bytes(body)

    def run(start=None, end=None, cache=True, version=None):
        if version is not None:
            monkeypatch.setattr(m, "PARSER_VERSION", version)
        store = m.StatementReads(tmp_path / "book_cc_statement_reads.json", "book") if cache else None
        return m.run_extraction(tmp_path / "in", tmp_path / "o.xlsx", start, end, "x", reads=store)
    return tmp_path, d, state, put, run


def _sig(res):
    return ([(r.date, r.description, r.amount, r.direction, r.kind, r.statement) for r in res.rows],
            [(t.card, t.period, t.result, t.printed, t.computed, t.difference) for t in res.ties])


def test_second_run_reuses_and_matches_a_fresh_read(env):
    tmp, d, state, put, run = env
    put("a.pdf")
    first = run()
    assert (first.reused, first.read_fresh) == (0, 1) and state["reads"] == 1
    second = run()
    assert (second.reused, second.read_fresh) == (1, 0) and state["reads"] == 1     # not re-read
    fresh = run(cache=False)
    assert _sig(second) == _sig(first) == _sig(fresh) and second.rows
    assert "Statement reads: 1 reused from the earlier run, 0 read fresh" in second.message


def test_changed_file_is_read_fresh(env):                                          # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    put("a.pdf", b"%PDF-1.4 synthetic CHANGED")
    res = run()
    assert (res.reused, res.read_fresh) == (0, 1) and state["reads"] == 2


def test_new_file_is_read_fresh_and_old_one_reused(env):                           # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    d2 = d.parent / "SBI-Other-Card"
    d2.mkdir()
    (d2 / "b.pdf").write_bytes(b"%PDF-1.4 synthetic B")
    res = run()
    assert (res.reused, res.read_fresh) == (1, 1)


def test_parser_version_bump_reads_fresh(env, monkeypatch):                        # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    res = run(version=m.PARSER_VERSION + 1)
    assert (res.reused, res.read_fresh) == (0, 1) and state["reads"] == 2


def test_same_bytes_under_another_name_or_card_is_not_reused(env):                 # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    (d / "a.pdf").rename(d / "renamed.pdf")
    assert run().reused == 0


def test_statement_that_does_not_read_period_is_never_stored(env):                 # NEGATIVE
    tmp, d, state, put, run = env
    state["lines"] = _head() + NO_PERIOD + LEGEND
    put("a.pdf")
    run()
    res = run()
    assert res.reused == 0 and state["reads"] == 2
    assert not (tmp / "book_cc_statement_reads.json").exists()


def test_untied_statement_is_never_stored(env, monkeypatch):                       # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    monkeypatch.setattr(m, "is_pass", lambda r: False)
    run()
    monkeypatch.undo()
    assert not (tmp / "book_cc_statement_reads.json").exists()


def test_unreadable_and_scanned_and_non_statement_are_never_stored(env, monkeypatch):   # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    state["lines"] = []
    res = run()
    assert res.read_fresh == 0 and not (tmp / "book_cc_statement_reads.json").exists()
    def boom(p):
        raise RuntimeError("bad")
    monkeypatch.setattr(m, "read_lines", boom)
    run()
    assert not (tmp / "book_cc_statement_reads.json").exists()


def test_cache_file_holds_no_password_or_page_text(env):                           # NEGATIVE
    tmp, d, state, put, run = env
    state["lines"] = GOOD + [f"synthetic page text marker {SECRET}"]
    put("a.pdf", b"%PDF-1.4 synthetic " + SECRET.encode())
    run()
    raw = (tmp / "book_cc_statement_reads.json").read_bytes()
    assert SECRET.encode() not in raw
    assert b"synthetic page text marker" not in raw
    assert b'"lines"' not in raw and b"password" not in raw.lower()
    for ln in GOOD:
        if len(ln.strip()) > 30:
            assert ln.strip().encode() not in raw


@pytest.mark.parametrize("junk", [b"", b"not json {{", b"[1,2]", b'{"version":1,"files":5}',
                                  b'{"version":1,"book":"book","parser_version":1,"files":{"x":{"bad":1}}}'])
def test_corrupt_cache_falls_back_to_fresh_read(env, junk):                        # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    (tmp / "book_cc_statement_reads.json").write_bytes(junk)
    res = run()
    assert (res.reused, res.read_fresh) == (0, 1) and res.rows


def test_damaged_entry_for_a_real_key_is_read_fresh(env):                          # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    p = tmp / "book_cc_statement_reads.json"
    data = json.loads(p.read_text())
    k = next(iter(data["files"]))
    data["files"][k] = {"bank": "SBI"}
    p.write_text(json.dumps(data))
    res = run()
    assert (res.reused, res.read_fresh) == (0, 1)


def test_other_book_cache_is_ignored(env):                                         # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    other = m.StatementReads(tmp / "book_cc_statement_reads.json", "another-book")
    assert other.get(next(iter(json.loads((tmp / "book_cc_statement_reads.json").read_text())["files"]))) is None


def test_duplicate_check_still_runs_over_reused_results(env):
    tmp, d, state, put, run = env
    put("a.pdf")
    put("b.pdf", b"%PDF-1.4 synthetic B")
    first = run()
    second = run()
    assert second.reused == 2 and second.read_fresh == 0
    assert len(second.duplicates) == len(first.duplicates) == 1 and len(second.rows) == len(first.rows)


def test_date_range_still_applies_to_reused_results(env):
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    inside = run(date(2026, 1, 1), date(2026, 12, 31))
    outside = run(date(2020, 1, 1), date(2020, 12, 31))
    assert inside.reused == 1 and outside.reused == 1
    assert inside.rows and outside.rows == [] and outside.statements == []


def test_no_cache_object_means_no_file_and_no_message_line(env):                   # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    res = run(cache=False)
    assert "Statement reads:" not in res.message and not (tmp / "book_cc_statement_reads.json").exists()


def test_store_sits_beside_the_book_state_and_never_raises(tmp_path, monkeypatch):
    from agents.skill_cc_spend_booking import agent as ccsb
    import agents.skill_gnucash_account_mapper.persistent_rules as pr
    monkeypatch.setattr(pr, "rules_path", lambda g, c=None: tmp_path / "sidecar" / "x_mapping_rules.yaml")
    store = ccsb._reads_store(m, str(tmp_path / "My Book 2026.gnucash"), None)
    assert store.path == tmp_path / "sidecar" / "My Book 2026_cc_statement_reads.json"
    monkeypatch.setattr(pr, "rules_path", lambda g, c=None: (_ for _ in ()).throw(RuntimeError("x")))
    assert ccsb._reads_store(m, str(tmp_path / "b.gnucash"), None) is None           # NEGATIVE: no crash


def test_report_line_only_when_something_was_counted():
    from agents.skill_cc_spend_booking.report import RunReport
    r = RunReport(label="x", threshold=0, ccp_path="p", entity="e", threshold_source="s", cred_max=0, cred_max_source="")
    assert r.statements_reused == 0 and r.statements_fresh == 0
