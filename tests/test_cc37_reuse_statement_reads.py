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


# ---------------------------------------------------------------------------
# End to end through the spend-booking agent's run(): the REAL _extract and _reads_store are used;
# only the PDF text, the book, the guard and the mapper are replaced. Not covered: real PDF parsing,
# real book reads, the AI mapper.
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    import csv as _csv
    import yaml
    from agents.skill_cc_spend_booking import agent as ccsb
    from agents.skill_cc_spend_booking.bookio import BookView, CCP_PATH
    from agents.gnucash_accounts import TargetGuard
    import agents.skill_cc_transactions.agent as cta
    import agents.skill_gnucash_account_mapper.persistent_rules as pr

    pdfs = tmp_path / "pdfs" / "SBI-BPCL-Octane"
    pdfs.mkdir(parents=True)
    state = {"reads": 0}

    def fake_read(p):
        state["reads"] += 1
        return GOOD
    monkeypatch.setattr(m, "read_lines", fake_read)
    monkeypatch.setattr(cta, "_load_script", lambda: m)          # one module, so the stubs apply
    sidecar = tmp_path / "sidecar"
    monkeypatch.setattr(pr, "rules_path", lambda g, c=None: sidecar / "x_mapping_rules.yaml")
    monkeypatch.setattr(ccsb, "read_book", lambda p: BookView(txns=[], paths={CCP_PATH, "Expenses:Food"}))

    class Guard:
        def blocked_target_reason(self, path):
            return None

        def account_type(self, path):
            return None
    monkeypatch.setattr(TargetGuard, "from_book", classmethod(lambda cls, p: Guard()))

    def fake_mapper(gnucash, cin, cout, config, drawings, default, model=None):
        rows = list(_csv.DictReader(open(cin, encoding="utf-8")))
        for r in rows:
            r["Account"], r["Confidence"], r["MatchReason"] = "Expenses:Food", "high", "fake"
        if rows:
            with open(cout, "w", newline="", encoding="utf-8") as f:
                w = _csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
    monkeypatch.setattr(ccsb, "_run_mapper", fake_mapper)
    ents = tmp_path / "entities.yaml"
    ents.write_text(yaml.safe_dump({"SYN": {"name": "Synthetic", "pan": "AAAAA0000A", "status": "Individual",
                                            "residency": "Resident", "default_regime": "new",
                                            "bank_service_charge_account": "Expenses:Food",
                                            "drawings_accounts": ["Expenses:Food"]}}), encoding="utf-8")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}", encoding="utf-8")

    def go(book="book.gnucash"):
        bk = tmp_path / book
        bk.write_text("x", encoding="utf-8")
        return ccsb.run(str(tmp_path / "pdfs"), "SYN", str(bk), str(tmp_path / "out" / "o.csv"),
                        "Custom date range", "2000-01-01", "2099-12-31", str(ents), str(cfg))
    return tmp_path, pdfs, state, sidecar, go


def _reads_file(sidecar, book="book"):
    return sidecar / f"{book}_cc_statement_reads.json"


def test_run_reuses_unchanged_statements_and_says_so(agent_env):
    tmp, pdfs, state, sidecar, go = agent_env
    (pdfs / "a.pdf").write_bytes(b"%PDF-1.4 synthetic A")
    first = go()
    assert "Statement reads: 0 reused from the earlier run, 1 read fresh" in first
    assert _reads_file(sidecar).is_file() and state["reads"] == 1
    second = go()
    assert "Statement reads: 1 reused from the earlier run, 0 read fresh" in second
    assert state["reads"] == 1                                       # the PDF was not opened again
    assert "0 reused" not in second                                  # NEGATIVE


def test_run_reads_fresh_when_the_file_changes(agent_env):                          # NEGATIVE
    tmp, pdfs, state, sidecar, go = agent_env
    (pdfs / "a.pdf").write_bytes(b"%PDF-1.4 synthetic A")
    go()
    (pdfs / "a.pdf").write_bytes(b"%PDF-1.4 synthetic A changed")
    out = go()
    assert "Statement reads: 0 reused from the earlier run, 1 read fresh" in out and state["reads"] == 2


def test_run_reads_fresh_after_a_parser_version_bump(agent_env, monkeypatch):       # NEGATIVE
    tmp, pdfs, state, sidecar, go = agent_env
    (pdfs / "a.pdf").write_bytes(b"%PDF-1.4 synthetic A")
    go()
    monkeypatch.setattr(m, "PARSER_VERSION", m.PARSER_VERSION + 1)
    out = go()
    assert "Statement reads: 0 reused from the earlier run, 1 read fresh" in out and state["reads"] == 2


def test_run_survives_a_corrupt_cache_and_reads_fresh(agent_env):                   # NEGATIVE
    tmp, pdfs, state, sidecar, go = agent_env
    (pdfs / "a.pdf").write_bytes(b"%PDF-1.4 synthetic A")
    go()
    _reads_file(sidecar).write_bytes(b"not json {{")
    out = go()
    assert "Statement reads: 0 reused from the earlier run, 1 read fresh" in out and state["reads"] == 2
    assert json.loads(_reads_file(sidecar).read_text())["files"]      # and a good file is written back


def test_run_does_not_use_another_books_cache(agent_env):                           # NEGATIVE
    tmp, pdfs, state, sidecar, go = agent_env
    (pdfs / "a.pdf").write_bytes(b"%PDF-1.4 synthetic A")
    go("book.gnucash")
    # the first book's file, presented under the second book's name: the recorded book differs
    sidecar.joinpath("other_cc_statement_reads.json").write_bytes(_reads_file(sidecar).read_bytes())
    out = go("other.gnucash")
    assert "Statement reads: 0 reused from the earlier run, 1 read fresh" in out and state["reads"] == 2


# ---------------------------------------------------------------------------
# Pruning: entries for files that are gone or superseded are dropped on save
# ---------------------------------------------------------------------------

def _entries(tmp):
    p = tmp / "book_cc_statement_reads.json"
    return json.loads(p.read_text())["files"] if p.exists() else {}


def test_deleted_file_entry_is_gone_after_the_next_save(env):
    tmp, d, state, put, run = env
    put("a.pdf")
    put("b.pdf", b"%PDF-1.4 synthetic B")
    run()
    assert len(_entries(tmp)) == 2
    (d / "b.pdf").unlink()
    d2 = d.parent / "SBI-Other-Card"                               # another card, so not a duplicate
    d2.mkdir()
    (d2 / "c.pdf").write_bytes(b"%PDF-1.4 synthetic C")
    run()
    paths = sorted(Path(e["path"]).name for e in _entries(tmp).values())
    assert paths == ["a.pdf", "c.pdf"]                             # b.pdf's entry gone


def test_changed_file_old_entry_is_gone_and_new_one_kept(env):
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    old = set(_entries(tmp))
    put("a.pdf", b"%PDF-1.4 synthetic CHANGED")
    run()
    new = set(_entries(tmp))
    assert len(new) == 1 and not (old & new)                       # stale key not left behind
    assert run().reused == 1                                       # and the new one is used


def test_unchanged_file_entry_is_not_dropped(env):                 # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    before = _entries(tmp)
    run()
    assert _entries(tmp) == before and len(before) == 1


def test_entry_for_a_file_outside_this_runs_folder_is_not_dropped(env, tmp_path):   # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    other = tmp / "elsewhere"
    other.mkdir()
    (other / "x.pdf").write_bytes(b"%PDF-1.4 synthetic X")
    store = m.StatementReads(tmp / "book_cc_statement_reads.json", "book")
    k = next(iter(store._files))
    store._files["outside"] = dict(store._files[k], path=str(other / "x.pdf"))
    store._dirty = True
    store.save()
    (d / "new.pdf").write_bytes(b"%PDF-1.4 synthetic NEW")        # the next run only looks at the folder d
    run()
    assert "outside" in _entries(tmp)
    (other / "x.pdf").unlink()                                     # but once that file is gone it goes
    (d / "new2.pdf").write_bytes(b"%PDF-1.4 synthetic NEW2")
    run()
    assert "outside" not in _entries(tmp)


def test_entry_without_a_path_is_dropped_once_never_kept_forever(env):
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    p = tmp / "book_cc_statement_reads.json"
    data = json.loads(p.read_text())
    for e in data["files"].values():
        e.pop("path")
    p.write_text(json.dumps(data))
    res = run()
    assert (res.reused, res.read_fresh) == (0, 1)
    assert all("path" in e for e in _entries(tmp).values()) and len(_entries(tmp)) == 1


def test_a_prune_error_does_not_fail_the_run(env, monkeypatch):    # NEGATIVE
    tmp, d, state, put, run = env
    put("a.pdf")
    run()
    monkeypatch.setattr(m.os.path, "exists", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    (d / "b.pdf").write_bytes(b"%PDF-1.4 synthetic B")
    res = run()
    assert res.rows and res.read_fresh == 1


# ---------------------------------------------------------------------------
# PARSER_VERSION guard: the parser source must not change without a version bump
# ---------------------------------------------------------------------------
import hashlib                                                      # noqa: E402
from pathlib import Path                                            # noqa: E402

_SCRIPT = (Path(__file__).resolve().parent.parent / "src" / "agents" / "skill_cc_transactions"
           / "scripts" / "create_cc_transaction_list.py")

# What is fingerprinted (everything that turns PDF text into a parsed Statement):
#   - the parse constants AMT, NUM, MON3, MONTHS, DATE_START, TOL_STRICT, TOL_LOOSE;
#   - every definition from `num` through `parse_statement`: num, signed, norm_dir, squash, tokens,
#     explicit_emi_kind, classify, the Row / Loan / Parsed / Statement dataclasses, _mk_row, _dmy,
#     _unparsed, parse_axis, parse_hdfc_old, strip_emi_marker, parse_hdfc_new, parse_icici, _hsbc_year,
#     _merchant_tokens, merchants_match, hsbc_instalment_kind, read_loan_summary, loans_booked_in_period,
#     parse_hsbc, the SBM helpers and parsers, resolve_sbm_roles, sbi_date, is_sbi_row, parse_sbi,
#     read_yes_positional, parse_yes, read_lines, get_bank_and_card_type, detect_period,
#     non_statement_reason, parse_statement.
# NOT fingerprinted: is_pass and the RESULT_* names, TieOut, find_prior, tie_out, gap_report, fees,
# the cache code, run_extraction, Excel writing: those run fresh on reused reads.


def parser_fingerprint(source: str) -> str:
    source = source.replace("\r\n", "\n")
    a, b = source.index('AMT = r"'), source.index("RESULT_PASS =")
    c, e = source.index("def num("), source.index("@dataclass\nclass TieOut")
    text = source[a:b] + "\n#--\n" + source[c:e]
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Append-only: one entry per parser state. Never edit an old entry; a new parser state gets a new version.
KNOWN_PARSER_FINGERPRINTS = {
    1: "3e4576689854c447ff8e4260ca78a24ec6ebfdc7ba5730a8feca6d21a39c2970",
}


def _current():
    return parser_fingerprint(_SCRIPT.read_text(encoding="utf-8"))


def test_parser_source_matches_its_parser_version():
    now = _current()
    v = m.PARSER_VERSION
    assert KNOWN_PARSER_FINGERPRINTS.get(v) == now, (
        f"parser source changed: bump PARSER_VERSION in create_cc_transaction_list.py and add "
        f"{{{v + 1}: '{now}'}} to KNOWN_PARSER_FINGERPRINTS (if this is the first run for version {v}, "
        f"add {{{v}: '{now}'}} instead)")


def test_every_known_fingerprint_is_distinct():
    vals = list(KNOWN_PARSER_FINGERPRINTS.values())
    assert len(set(vals)) == len(vals), "a new parser state must not reuse an old fingerprint"
    assert max(KNOWN_PARSER_FINGERPRINTS) == m.PARSER_VERSION, "PARSER_VERSION is behind the newest known entry"


def test_a_parser_change_changes_the_fingerprint_but_a_tie_out_change_does_not():   # NEGATIVE
    src = _SCRIPT.read_text(encoding="utf-8")
    base = parser_fingerprint(src)
    assert parser_fingerprint(src.replace("def parse_sbi(lines", "def parse_sbi(lines ", 1)) != base
    assert parser_fingerprint(src.replace('AMT = r"\d', 'AMT = r"\s?\d', 1)) != base
    assert parser_fingerprint(src.replace("def tie_out(st", "def tie_out(st ", 1)) == base
    assert parser_fingerprint(src.replace("def run_extraction(", "def run_extraction( ", 1)) == base
    assert parser_fingerprint(src.replace("\n", "\r\n")) == base          # CRLF checkout gives the same hash
