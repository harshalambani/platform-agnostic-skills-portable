"""BNK-14 -- screens remove their temp copies of the uploaded statements.

The staging folder of the uploads and the run's work folder (both made by the
generic run handler), and the 26AS journal's overrides file, are removed when
the run ends: on success, on failure, on stop and when the stream is closed.
Only what the app made in the temp folder is removed. Synthetic data only.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
for _p in (ROOT.parent / "src", ROOT.parent / "src" / "agents", ROOT.parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import registry  # noqa: E402
from tests.test_bnk04_sbm_parser import ACCT, PW, _rows, _write  # noqa: E402
from ui import _config, _runner  # noqa: E402
from ui.tabs import _generic  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A private temp folder, output folder and download staging folder, so a
    run's leftovers can be listed exactly."""
    temp = tmp_path / "temp"
    temp.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    dl = tmp_path / "dl"
    dl.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    # the suite-wide fixture reroutes pa-skills- folders; here they land in `temp`
    _outer = tempfile.mkdtemp
    monkeypatch.setattr(
        tempfile, "mkdtemp",
        lambda suffix=None, prefix=None, dir=None: _outer(
            suffix=suffix, prefix=prefix, dir=dir or str(temp)))
    monkeypatch.setattr(_config, "output_dir", lambda: out)
    monkeypatch.setattr(_config, "_DOWNLOAD_STAGING_DIR", dl)
    # the picked statement lives OUTSIDE the temp folder
    picked = tmp_path / "picked"
    picked.mkdir()
    xlsx = picked / "stmt.xlsx"
    xlsx = _write(xlsx, _rows())
    return {"temp": temp, "out": out, "picked": picked, "xlsx": Path(xlsx)}


def _left(env):
    return sorted(p.name for p in env["temp"].iterdir())


def _drive(env, *, close_after=None):
    skill = registry.get("skill_bank_skills")
    gen = _generic._make_run_handler(skill)("sbm", [str(env["xlsx"])], "", PW, False, "")
    texts = []
    try:
        for i, t in enumerate(gen):
            texts.append(str(t[0]))
            if close_after is not None and i + 1 >= close_after:
                break
    finally:
        gen.close()
    return " ".join(texts)


# -- each end of a run leaves nothing behind -----------------------------------

def test_success_leaves_nothing_in_temp(env):
    text = _drive(env)
    assert "Done" in text or "Result" in text
    assert _left(env) == []
    assert env["xlsx"].exists()                                  # NEGATIVE: user file kept


def test_a_failing_run_leaves_nothing_in_temp(env, monkeypatch):
    def boom(skill):
        def run(**kw):
            raise RuntimeError("synthetic failure")
        return run
    monkeypatch.setattr("agents.registry.load_run_function", boom)
    text = _drive(env)
    assert "run failed" in text
    assert _left(env) == []
    assert env["xlsx"].exists()


def test_a_stopped_run_leaves_nothing_in_temp(env, monkeypatch):
    def stopped(work, tick_factory, on_event=None, **kw):
        work()
        return "__CANCELLED__"
        yield  # pragma: no cover - makes this a generator
    monkeypatch.setattr(_runner, "run_with_progress", stopped)
    text = _drive(env)
    assert "Cancelled" in text
    assert _left(env) == []
    assert env["xlsx"].exists()


def test_closing_the_stream_early_leaves_nothing_in_temp(env):
    _drive(env, close_after=2)           # staged, then the page goes away
    assert _left(env) == []
    assert env["xlsx"].exists()


def test_an_early_refusal_after_staging_leaves_nothing(env, tmp_path):
    # two uploads with one name: refused after the staging folder was made
    other = tmp_path / "picked2"
    other.mkdir()
    twin = other / "stmt.xlsx"
    twin.write_bytes(env["xlsx"].read_bytes())
    skill = registry.get("skill_bank_skills")
    gen = _generic._make_run_handler(skill)(
        "sbm", [str(env["xlsx"]), str(twin)], "", PW, False, "")
    text = " ".join(str(t[0]) for t in gen)
    assert "both named" in text
    assert _left(env) == []
    assert twin.exists() and env["xlsx"].exists()


# -- the helper itself ----------------------------------------------------------

def test_only_a_folder_the_app_made_in_temp_is_removed(env, tmp_path):   # NEGATIVE
    mine = _generic.make_run_temp("pa-skills-x-uploads-")
    (mine / "a.pdf").write_bytes(b"x")
    # a folder the user picked, a foreign temp folder, a path that climbs out,
    # and an absolute path elsewhere: none of these may be removed
    picked = env["picked"]
    foreign = Path(tempfile.mkdtemp(prefix="pa-skills-not-made-by-a-run-"))
    climbing = mine / ".." / ".." / "picked"
    _generic.remove_run_temp([picked, foreign, climbing, env["xlsx"],
                              str(tmp_path), "relative/dir", None, ""])
    assert picked.is_dir() and env["xlsx"].exists()
    assert foreign.is_dir() and tmp_path.is_dir()
    assert mine.is_dir()                # not asked to remove it yet
    _generic.remove_run_temp([mine])
    assert not mine.exists()
    _generic.remove_run_temp([mine])    # twice is fine
    _generic.remove_run_temp(None)


def test_an_upload_name_cannot_point_outside_the_staging_folder(env, tmp_path):  # NEGATIVE
    # the staging folder is the only thing recorded, so whatever the upload is
    # called, nothing outside it is ever a removal target
    keep = tmp_path / "keep.txt"
    keep.write_text("keep")
    _drive(env)
    assert keep.exists() and env["xlsx"].exists()
    assert _left(env) == []


def test_an_error_while_cleaning_does_not_hide_the_run_result(env, monkeypatch):  # NEGATIVE
    def broken(*a, **k):
        raise PermissionError("synthetic: cannot remove")
    monkeypatch.setattr(_generic.shutil, "rmtree", broken)
    monkeypatch.setattr(_generic.time, "sleep", lambda s: None)
    text = _drive(env)                  # does not raise
    assert "Done" in text or "Result" in text


def test_a_failing_run_still_reports_its_own_error_when_cleaning_fails(env, monkeypatch):  # NEGATIVE
    def boom(skill):
        def run(**kw):
            raise RuntimeError("synthetic failure")
        return run
    monkeypatch.setattr("agents.registry.load_run_function", boom)
    monkeypatch.setattr(_generic.shutil, "rmtree",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr(_generic.time, "sleep", lambda s: None)
    assert "synthetic failure" in _drive(env)


# -- the 26AS journal's overrides file ------------------------------------------

def _journal(env, monkeypatch, script_result):
    from agents.skill_26as_journal import tools
    seen = {}

    def fake_run(args):
        seen["ov"] = args[3]
        assert Path(args[3]).is_file()          # present while the script runs
        if isinstance(script_result, Exception):
            raise script_result
        return script_result
    monkeypatch.setattr(tools, "_run_script", fake_run)
    monkeypatch.setattr(tools, "_verify", lambda p: "verified")
    monkeypatch.setattr(tools, "_gate_ambiguous_overrides",
                        lambda norm, out: (norm, []))
    return tools, seen


def test_journal_overrides_file_removed_on_success(env, monkeypatch):
    tools, seen = _journal(env, monkeypatch, "built")
    out = tools.run_apply("a.xlsx", "b.gnucash", "o.csv", {"1": "Expenses:X"})
    assert out.endswith("verified")
    assert not Path(seen["ov"]).exists()
    assert _left(env) == []


def test_journal_overrides_file_removed_on_script_error_text(env, monkeypatch):
    tools, seen = _journal(env, monkeypatch, "ERROR: bad")
    assert tools.run_apply("a.xlsx", "b.gnucash", "o.csv", {"1": "Expenses:X"}) == "ERROR: bad"
    assert not Path(seen["ov"]).exists()


def test_journal_overrides_file_removed_when_the_script_raises(env, monkeypatch):
    tools, seen = _journal(env, monkeypatch, RuntimeError("synthetic"))
    with pytest.raises(RuntimeError, match="synthetic"):
        tools.run_apply("a.xlsx", "b.gnucash", "o.csv", {"1": "Expenses:X"})
    assert not Path(seen["ov"]).exists()


def test_journal_failure_to_remove_does_not_hide_the_result(env, monkeypatch):   # NEGATIVE
    tools, seen = _journal(env, monkeypatch, "built")

    def deny(p):
        raise PermissionError("synthetic")
    with monkeypatch.context() as m:
        m.setattr(tools.os, "remove", deny)
        out = tools.run_apply("a.xlsx", "b.gnucash", "o.csv", {"1": "Expenses:X"})
    assert out.endswith("verified")
    Path(seen["ov"]).unlink()           # the test's own leftover


def test_the_leftover_check_can_fail(env, monkeypatch):               # NEGATIVE (proves the check)
    monkeypatch.setattr(_generic, "remove_run_temp", lambda folders: None)
    _drive(env)
    assert any(n.startswith("pa-skills-") for n in _left(env))


# -- the download staging folder: the previous run's copies go when the next run starts --

@pytest.fixture()
def clock(monkeypatch):
    """Each run gets its own output file name (the name carries the second)."""
    import datetime as _dt
    state = {"n": 0}

    class _Now(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            state["n"] += 1
            return _dt.datetime(2026, 1, 1, 0, 0, 0) + _dt.timedelta(seconds=state["n"])
    monkeypatch.setattr(_generic, "datetime", _Now)


def _handler():
    return _generic._make_run_handler(registry.get("skill_bank_skills"))


def _go(env, handler, files=None):
    """One run; returns (all text, the final Download button update)."""
    files = [str(env["xlsx"])] if files is None else files
    texts, last = [], None
    for t in handler("sbm", files, "", PW, False, ""):
        texts.append(str(t[0]))
        last = t[1]
    return " ".join(texts), last


def _dl(env):
    return sorted(p.name for p in (env["temp"].parent / "dl").iterdir())


def _button_path(update):
    return Path(update["value"]) if isinstance(update, dict) and update.get("value") else None


def test_run_two_removes_run_ones_copy_and_points_at_its_own(env, clock):
    h = _handler()
    _, b1 = _go(env, h)
    c1 = _button_path(b1)
    assert c1 and c1.is_file()
    _, b2 = _go(env, h)
    c2 = _button_path(b2)
    assert not c1.exists()                   # run 1's copy is gone
    assert c2.is_file() and c2 != c1         # run 2's copy is what the button points at
    assert _dl(env) == [c2.name]


def test_another_tabs_copy_survives_a_run_on_this_tab(env, clock):     # NEGATIVE
    a, b = _handler(), _handler()
    _, ba = _go(env, a)
    _, bb = _go(env, b)
    ca, cb = _button_path(ba), _button_path(bb)
    _go(env, a)                              # a second run on tab A
    assert not ca.exists()
    assert cb.is_file()                      # tab B's copy is untouched


def test_a_refused_run_leaves_the_previous_copy(env, clock):           # NEGATIVE
    h = _handler()
    _, b1 = _go(env, h)
    c1 = _button_path(b1)
    # a folder that does not exist: refused by the input checks, before any run starts
    text = " ".join(str(t[0]) for t in h("hsbc", None, str(env["picked"] / "nope"), "", False, ""))
    assert "does not exist" in text
    assert c1.is_file()
    # a run refused after staging (two uploads, one name) also leaves it
    twin_dir = env["picked"].parent / "twin"
    twin_dir.mkdir()
    twin = twin_dir / env["xlsx"].name
    twin.write_bytes(env["xlsx"].read_bytes())
    text, _ = _go(env, h, files=[str(env["xlsx"]), str(twin)])
    assert "both named" in text
    assert c1.is_file()


def test_the_real_result_in_the_outputs_folder_is_never_removed(env, clock):   # NEGATIVE
    h = _handler()
    _go(env, h)
    _go(env, h)
    outs = sorted(p.name for p in env["out"].glob("*.csv"))
    assert len(outs) == 2                    # both runs' real results are still there
    assert env["xlsx"].exists()


def test_an_error_removing_a_copy_does_not_hide_the_new_result(env, clock, monkeypatch):   # NEGATIVE
    h = _handler()
    _go(env, h)
    with monkeypatch.context() as m:
        def deny(self, *a, **k):
            raise PermissionError("synthetic")
        m.setattr(Path, "unlink", deny)
        text, b2 = _go(env, h)
    assert ("Done" in text or "Result" in text) and _button_path(b2)


def test_only_this_handlers_own_unchanged_copy_inside_staging_is_removed(env, clock, tmp_path):  # NEGATIVE
    h = _handler()
    _, b1 = _go(env, h)
    c1 = _button_path(b1)
    # another tab later wrote a different file under the same name
    c1.write_bytes(c1.read_bytes() + b"another tab's content")
    outside = env["picked"] / "elsewhere.csv"
    outside.write_text("keep")
    entries = [_generic._served_entry(outside)]
    _generic.remove_served_copies(entries)   # a path outside the staging folder
    assert outside.exists() and entries == []
    _go(env, h)                              # run 2: c1 is no longer the copy it wrote
    assert c1.is_file()
    assert env["xlsx"].exists() and outside.exists()


def test_removal_disabled_fails_the_run_one_copy_gone_check(env, clock, monkeypatch):   # mutation proof
    monkeypatch.setattr(_generic, "remove_served_copies", lambda entries: None)
    h = _handler()
    _, b1 = _go(env, h)
    c1 = _button_path(b1)
    _go(env, h)
    assert c1.exists()                       # with removal off, the old copy stays
