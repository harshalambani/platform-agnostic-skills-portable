"""UI-54 (1/3): the run history. Synthetic data only; every file lands under pytest's tmp."""
# ruff: noqa: F811  (imported fixtures are used by name)
from __future__ import annotations

import json
import re
import sys
import threading
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import registry  # noqa: E402
from agents.outputs import ReplyWithOutputs  # noqa: E402
from tests.test_bnk04_sbm_parser import PW  # noqa: E402
from tests.test_bnk14_temp_cleanup import env  # noqa: E402,F401  (fixture)
from ui import _config, _runhistory, _runlog  # noqa: E402
from ui.tabs import _generic  # noqa: E402


# ------------------------------------------------------------------ helpers ---

@pytest.fixture
def hist(tmp_path, monkeypatch):
    root = tmp_path / "dataroot"
    monkeypatch.setattr(_config, "data_root_dir", lambda: root)
    monkeypatch.setitem(_runhistory._state, "appends", 0)
    monkeypatch.setitem(_runhistory._state, "warned", False)
    return root / "logs" / "run-history.jsonl"


def _rec(i=0, days_old=0, **kw):
    ts = (datetime.now() - timedelta(days=days_old)).isoformat(timespec="seconds")
    base = dict(run=f"r{i}", skill="s", result="ok", ts=ts)
    base.update(kw)
    return _runhistory.make_record(**base)


def _seed(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def _drive(env, *, close_after=None, files=None):
    skill = registry.get("skill_bank_skills")
    gen = _generic._make_run_handler(skill)("sbm", files or [str(env["xlsx"])], "", PW, False, "")
    got = []
    try:
        for i, t in enumerate(gen):
            got.append(t)
            if close_after is not None and i + 1 >= close_after:
                break
    finally:
        gen.close()
    return got


def _stub_run(monkeypatch, fn):
    def load(skill):
        return fn
    monkeypatch.setattr("agents.registry.load_run_function", load)


def _writes_output(text="done"):
    def run(**kw):
        Path(kw["output_path"]).write_text("a,b\n1,2\n", encoding="utf-8")
        return text
    return run


# --------------------------------------------------------- the store itself ---

def test_data_root_is_under_pytest_tmp_by_default(tmp_path_factory):
    """The suite-wide fixture (conftest): nothing resolves to the real data root."""
    base = tmp_path_factory.getbasetemp().resolve()
    assert base in _runhistory.path().resolve().parents


def test_append_then_tail_round_trip(hist):
    assert _runhistory.append(_rec(1, entity="E1", year="FY2025-26", why="because"))
    got = _runhistory.tail(5)
    assert [(r["run"], r["entity"], r["year"], r["why"]) for r in got] == [("r1", "E1", "FY2025-26", "because")]
    assert set(got[0]) == set(_runhistory.FIELDS)
    assert "tab" not in got[0]                                    # NEGATIVE: the tab is never stored


def test_record_is_bounded_and_clean():
    r = _runhistory.make_record(skill="x", result="exploded", why="a\n" * 400, secs="3.6")
    assert r["result"] == "failed"                                # unknown result never stored as-is
    assert len(r["why"]) <= 300 and "\n" not in r["why"]
    assert r["secs"] == 4
    assert set(r) == set(_runhistory.FIELDS)


def test_unknown_fields_are_never_stored(hist):          # NEGATIVE
    _runhistory.append({**_rec(1), "tab": "GnuCash > Banks", "bogus": "x"})
    (r,) = _runhistory.tail(5)
    assert set(r) == set(_runhistory.FIELDS)


def test_tail_skips_corrupt_and_half_written_lines(hist):      # NEGATIVE
    good = [_rec(1), _rec(2)]
    hist.parent.mkdir(parents=True)
    hist.write_text(json.dumps(good[0]) + "\n" + "not json at all\n" + "[1, 2]\n"
                    + '{"skill": "no ts"}\n' + json.dumps(good[1]) + "\n" + '{"ts": "2026-01-01T00:0',
                    encoding="utf-8")
    assert [r["run"] for r in _runhistory.tail(10)] == ["r1", "r2"]


def test_tail_never_raises(hist, tmp_path):
    assert _runhistory.tail(5) == []                              # file missing
    hist.parent.mkdir(parents=True)
    hist.write_bytes(b"\xff\xfe\x00garbage\n")
    assert _runhistory.tail(5) == []
    assert _runhistory.tail(0) == []


def test_tail_returns_the_newest_n_oldest_first(hist):
    for i in range(7):
        _runhistory.append(_rec(i))
    assert [r["run"] for r in _runhistory.tail(3)] == ["r4", "r5", "r6"]


def test_trim_keeps_the_newest_1000(hist):
    _seed(hist, [_rec(i) for i in range(1100)])
    assert _runhistory.trim()
    got = _lines(hist)
    assert len(got) == 1000
    assert json.loads(got[0])["run"] == "r100" and json.loads(got[-1])["run"] == "r1099"


def test_a_record_older_than_90_days_is_dropped_at_trim(hist):  # NEGATIVE
    _seed(hist, [_rec(1, days_old=91), _rec(2, days_old=89), _rec(3, days_old=400), _rec(4)])
    assert _runhistory.trim()
    assert [json.loads(x)["run"] for x in _lines(hist)] == ["r2", "r4"]


def test_trim_leaves_a_clean_file_alone(hist):
    _seed(hist, [_rec(i) for i in range(5)])
    before = hist.stat().st_mtime_ns
    _runhistory.trim()
    assert hist.stat().st_mtime_ns == before                      # nothing to drop: no rewrite


def test_a_crash_mid_trim_keeps_the_old_file(hist, monkeypatch):  # NEGATIVE
    _seed(hist, [_rec(i) for i in range(1100)])
    original = hist.read_text(encoding="utf-8")

    def boom(*a, **k):
        raise OSError("synthetic crash before the swap")
    monkeypatch.setattr(_runhistory.os, "replace", boom)
    assert _runhistory.trim() is False                            # never raises
    assert hist.read_text(encoding="utf-8") == original


def test_the_first_append_of_a_process_trims(hist):
    _seed(hist, [_rec(1, days_old=200), _rec(2)])
    _runhistory.append(_rec(3))
    assert [json.loads(x)["run"] for x in _lines(hist)] == ["r2", "r3"]


def test_concurrent_appends_during_a_trim_lose_nothing(hist, monkeypatch):  # NEGATIVE
    _seed(hist, [_rec(i) for i in range(1100)])
    monkeypatch.setitem(_runhistory._state, "appends", 5)          # appends do not trim on their own here
    real_replace = _runhistory.os.replace

    def slow_replace(a, b):
        time.sleep(0.4)                                           # hold the trim open
        return real_replace(a, b)
    monkeypatch.setattr(_runhistory.os, "replace", slow_replace)

    t_trim = threading.Thread(target=_runhistory.trim)
    t_trim.start()
    time.sleep(0.1)
    results = []
    workers = [threading.Thread(target=lambda n=n: results.append(_runhistory.append(_rec(n, run=f"late{n}"))))
               for n in (1, 2)]
    for w in workers:
        w.start()
    for w in (t_trim, *workers):
        w.join()
    assert results == [True, True]
    parsed = [json.loads(x) for x in _lines(hist)]                # every line is whole JSON
    runs = [r["run"] for r in parsed]
    assert "late1" in runs and "late2" in runs
    assert len(parsed) == 1002                                    # 1000 kept by the trim + the two late ones
    assert len(set(runs)) == len(runs)


def test_many_threads_appending_never_interleave_lines(hist):   # NEGATIVE
    def worker(k):
        for j in range(20):
            _runhistory.append(_rec(j, run=f"t{k}-{j}", why="x" * 250))
    ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    parsed = [json.loads(x) for x in _lines(hist)]
    assert len(parsed) == 160
    assert len({r["run"] for r in parsed}) == 160


def test_a_failed_write_never_raises_and_warns_once(hist, monkeypatch):  # NEGATIVE
    hist.mkdir(parents=True)                                      # a folder where the file should be
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        assert _runhistory.append(_rec(1)) is False
        assert _runhistory.append(_rec(2)) is False
    assert len([x for x in w if "run history not written" in str(x.message)]) == 1


def test_scope_from_inputs():
    f = _runhistory.scope_from_inputs
    assert f(None, {"entity": "E1", "fy": "FY2025-26"}) == ("E1", "FY2025-26")
    assert f(None, {"entities": "E2, E3", "ay": "AY2026-27"}) == ("E2", "AY2026-27")
    assert f(None, {"entity_a": "A", "entity_b": "B"}) == ("A + B", "")
    assert f(None, {}) == ("", "")
    assert f(None, {"entity": "E1", "entities": "E2"})[0] == "E1"


# ------------------------------------------------------------ classification ---

@pytest.mark.parametrize("reply,expect", [
    ("Stopped. Nothing was removed. The folder is missing.", ("stopped", "Stopped. Nothing was removed. The folder is missing.")),
    ("ERROR: select at least one book.", ("failed", "ERROR: select at least one book.")),
    ("All good\nRED FLAG: double booking found", ("attention", "RED FLAG: double booking found")),
    ("2 missing statements", ("attention", "2 missing statements")),
    ("Done. 12 rows written.", None),
    ("", None),
    ("RED FLAG: x\nStopped. y", ("stopped", "Stopped. y")),         # Stopped / ERROR win over attention
])
def test_reply_outcome(reply, expect):
    assert _generic._reply_outcome(reply) == expect


def _st(**kw):
    base = {"completed": True, "started": True}
    base.update(kw)
    return base


def test_run_outcome_table():
    o = _generic._run_outcome
    assert o(_st(), "### Done") == ("ok", "")
    assert o(_st(override=("failed", "RuntimeError: x")), "Error: run failed") == ("failed", "RuntimeError: x")
    assert o(_st(override=("cancelled", "c")), "") == ("cancelled", "c")
    assert o(_st(override=("not_ready", "n")), "")[0] == "not_ready"
    assert o(_st(no_output=True, reply="Stopped. why"), "Error: the run did not finish")[0] == "stopped"
    assert o(_st(no_output=True, reply=""), "Error: the run did not finish")[0] == "failed"
    assert o(_st(started=False), "Warning: please provide: Statement") == ("refused", "please provide: Statement")
    assert o(_st(completed=False), "x")[0] == "interrupted"
    assert o(_st(completed=False, override=("cancelled", "c")), "x")[0] == "cancelled"
    assert o(_st(), "Error: could not stage download file: x")[0] == "failed"


# --------------------------------------------------------------- the real hook ---

def test_a_successful_run_is_recorded_ok(env, hist, monkeypatch):
    _stub_run(monkeypatch, _writes_output("Wrote 1 row."))
    _drive(env)
    (r,) = _runhistory.tail(5)
    assert (r["result"], r["why"], r["skill"]) == ("ok", "", "skill_bank_skills")
    assert r["out"].endswith("Bank-Skills.csv") and r["log"].endswith(".log") and r["run"]


def test_a_failing_run_is_recorded_failed_with_why(env, hist, monkeypatch):
    def run(**kw):
        raise RuntimeError("synthetic failure")
    _stub_run(monkeypatch, run)
    _drive(env)
    (r,) = _runhistory.tail(5)
    assert r["result"] == "failed" and r["why"] == "RuntimeError: synthetic failure"
    assert r["out"] == ""                                         # no output claimed for a failure


def test_a_cancelled_run_is_recorded_cancelled(env, hist, monkeypatch):
    from ui import _runner

    def stopped(work, tick_factory, on_event=None, **kw):
        work()
        return "__CANCELLED__"
        yield  # pragma: no cover
    monkeypatch.setattr(_runner, "run_with_progress", stopped)
    _stub_run(monkeypatch, _writes_output())
    _drive(env)
    assert [r["result"] for r in _runhistory.tail(5)] == ["cancelled"]


def test_a_closed_page_is_recorded_interrupted(env, hist):
    _drive(env, close_after=2)
    assert [r["result"] for r in _runhistory.tail(5)] == ["interrupted"]


def test_an_input_refusal_is_recorded_refused(env, hist, tmp_path):
    other = tmp_path / "picked2"
    other.mkdir()
    twin = other / "stmt.xlsx"
    twin.write_bytes(env["xlsx"].read_bytes())
    _drive(env, files=[str(env["xlsx"]), str(twin)])
    (r,) = _runhistory.tail(5)
    assert r["result"] == "refused" and "both named" in r["why"]
    assert r["log"] == "" and r["out"] == ""                      # never started: no log, no output


def test_a_withheld_output_is_recorded_not_ready(env, hist, monkeypatch):
    def run(**kw):
        Path(kw["output_path"]).write_text("a\n", encoding="utf-8")
        return ReplyWithOutputs("problem", withhold_primary=True)
    _stub_run(monkeypatch, run)
    _drive(env)
    (r,) = _runhistory.tail(5)
    assert r["result"] == "not_ready"


def test_a_stopped_reply_is_recorded_stopped_with_its_reason(env, hist, monkeypatch):
    _stub_run(monkeypatch, lambda **kw: "Stopped. The year-end folder is missing.")   # no output file
    _drive(env)
    (r,) = _runhistory.tail(5)
    assert r["result"] == "stopped" and r["why"] == "Stopped. The year-end folder is missing."


def test_a_red_flag_reply_is_recorded_attention(env, hist, monkeypatch):
    _stub_run(monkeypatch, _writes_output("RED FLAG: balance does not tie\nrest"))
    _drive(env)
    (r,) = _runhistory.tail(5)
    assert r["result"] == "attention" and r["why"].startswith("RED FLAG")
    assert r["out"]                                               # the file was written; it just needs a look


def test_the_password_never_reaches_the_record(env, hist, monkeypatch):  # NEGATIVE
    def run(**kw):
        raise RuntimeError(f"cannot open with {kw['password']}")
    _stub_run(monkeypatch, run)
    _drive(env)
    raw = hist.read_text(encoding="utf-8")
    assert PW not in raw and "***" in raw


def test_a_run_with_no_entity_input_records_blank_scope(env, hist, monkeypatch):
    _stub_run(monkeypatch, _writes_output())
    _drive(env)
    (r,) = _runhistory.tail(5)
    assert (r["entity"], r["year"]) == ("", "")


# ---- the hook changes nothing the user sees -------------------------------------

class _FixedClock:
    """A constant clock so two runs name their files identically."""
    @staticmethod
    def make(real):
        class _Now(real):
            @classmethod
            def now(cls, tz=None):
                return real(2026, 1, 1, 0, 0, 0)
        return _Now


def _norm(text, env):
    text = text.replace(str(env["out"]), "<out>").replace(str(env["temp"]), "<tmp>")
    text = text.replace(str(env["picked"]), "<picked>")
    text = re.sub(r"uploads-[a-z0-9_]{8}", "uploads-X", text)
    text = re.sub(r"[A-Za-z]:[\\/][^\s`'\"<>)]*", "<path>", text)
    return re.sub(r"\d+s\b", "Ns", text)


def _collect(env, monkeypatch, mode):
    monkeypatch.setattr(_generic, "datetime", _FixedClock.make(datetime))
    monkeypatch.setattr(_runlog, "datetime", _FixedClock.make(datetime))
    return [_norm(repr(t), env) for t in _drive(env)]


@pytest.mark.parametrize("how", ["success", "failure", "cancelled"])
def test_hook_on_and_off_yield_identical_statuses_and_outputs(how, env, hist, monkeypatch):  # NEGATIVE
    from ui import _runner
    if how == "success":
        _stub_run(monkeypatch, _writes_output("Wrote 1 row."))
    elif how == "failure":
        def run(**kw):
            raise RuntimeError("synthetic failure")
        _stub_run(monkeypatch, run)
    else:
        def stopped(work, tick_factory, on_event=None, **kw):
            work()
            return "__CANCELLED__"
            yield  # pragma: no cover
        monkeypatch.setattr(_runner, "run_with_progress", stopped)
        _stub_run(monkeypatch, _writes_output())

    monkeypatch.setattr(_runhistory, "ENABLED", False)
    off = _collect(env, monkeypatch, "off")
    assert not hist.exists()                                      # off means off
    monkeypatch.setattr(_runhistory, "ENABLED", True)
    on = _collect(env, monkeypatch, "on")
    assert hist.exists() and len(_lines(hist)) == 1
    assert on == off and len(on) >= 2


def test_a_failing_history_write_changes_nothing_in_the_run(env, hist, monkeypatch):  # NEGATIVE
    _stub_run(monkeypatch, _writes_output("Wrote 1 row."))
    monkeypatch.setattr(_runhistory, "ENABLED", False)
    baseline = _collect(env, monkeypatch, "off")
    monkeypatch.setattr(_runhistory, "ENABLED", True)

    def boom(rec):
        raise OSError("disk full")
    monkeypatch.setattr(_runhistory, "append", boom)
    assert _collect(env, monkeypatch, "on") == baseline

