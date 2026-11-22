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
