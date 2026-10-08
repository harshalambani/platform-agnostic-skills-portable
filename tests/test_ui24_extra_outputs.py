"""
tests/test_ui24_extra_outputs.py -- UI-24: a run's extra output files (the
partner skill's journal CSVs) are offered in the Done panel, from a
structured hand-off, never by scanning a folder.

All fixtures are synthetic: made-up file names, made-up CSV text.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

from agents.outputs import ReplyWithOutputs, extra_output
from agents.registry import SkillExtraOutput
from ui import _config
from ui.tabs import _generic
from ui.tabs._generic import _make_run_handler, _stage_extra_outputs

EXTRAS = (
    SkillExtraOutput(key="journal_csv", label="Monthly journal",
                     download_label="Download journal CSV"),
    SkillExtraOutput(key="accrual_journal_csv", label="Year-end accrual journal",
                     download_label="Download accrual journal CSV"),
)


def _skill(extras=EXTRAS):
    output = SimpleNamespace(type="file", suffix="out", extension=".xlsx",
                             download_label="Download", extra_outputs=tuple(extras))
    inp = SimpleNamespace(name="entity_name", type="text", label="Name", required=False,
                          file_types=None, options=[], default="")
    return SimpleNamespace(
        name="fake_skill", display_name="Fake", description="", inputs=[inp],
        output=output, requires=SimpleNamespace(native_binaries=[], external_tools=[], llm=False),
        run_args={"output_path": "{output_path}"}, mode="direct", entry_point="x:run",
        package="agents", help=None,
    )


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    out = tmp_path / "outputs"
    stage = tmp_path / "stage"
    out.mkdir()
    stage.mkdir()
    monkeypatch.setattr(_config, "output_dir", lambda: out)
    monkeypatch.setattr(_config, "download_staging_dir", lambda: stage)
    return SimpleNamespace(out=out, stage=stage, tmp=tmp_path)


def _write(path: Path, text="Date,Account,Amount\n2026-03-31,Assets:Test,1.00\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _enabled(upd) -> bool:
    return bool(upd.get("interactive")) and bool(upd.get("value"))


def test_both_journals_written_are_listed_staged_and_enabled(dirs):
    m = _write(dirs.out / "ENT-FY2025-26-partner-journal.csv")
    a = _write(dirs.out / "ENT-FY2025-26-partner-accrual-journal.csv")
    reply = ReplyWithOutputs("ok", [extra_output("journal_csv", m), extra_output("accrual_journal_csv", a)])
    r = _stage_extra_outputs(_skill(), reply)
    assert r["block"].startswith("**Journals to import into GnuCash**")
    assert str(Path(m).resolve()) in r["block"] and str(Path(a).resolve()) in r["block"]
    assert "Monthly journal" in r["block"] and "Year-end accrual journal" in r["block"]
    assert all(_enabled(u) for u in r["updates"])
    for u in r["updates"]:  # only the staged COPY is served
        assert Path(u["value"]).parent == dirs.stage.resolve()
    assert r["paths"] == [str(Path(m).resolve()), str(Path(a).resolve())]


def test_old_journal_in_the_folder_is_never_offered_when_this_run_wrote_none(dirs):
    _write(dirs.out / "ENT-FY2025-26-partner-journal.csv")  # an earlier run's file
    reply = ReplyWithOutputs("ok", [
        extra_output("journal_csv", None, "No journal written: the journal path was left blank."),
        extra_output("accrual_journal_csv", None, "No accrual journal written."),
    ])
    r = _stage_extra_outputs(_skill(), reply)
    assert not any(_enabled(u) for u in r["updates"])
    assert r["paths"] == []
    assert "none written" in r["block"] and "journal path was left blank" in r["block"]
    assert list(dirs.stage.iterdir()) == []


def test_a_plain_reply_with_no_structured_outputs_offers_nothing(dirs):
    _write(dirs.out / "stale-journal.csv")
    r = _stage_extra_outputs(_skill(), "ERROR: the run stopped before journals")
    assert not any(_enabled(u) for u in r["updates"])
    assert list(dirs.stage.iterdir()) == []


def test_path_outside_the_output_dir_is_never_offered_or_staged(dirs):
    elsewhere = _write(dirs.tmp / "elsewhere" / "journal.csv")
    reply = ReplyWithOutputs("ok", [extra_output("journal_csv", elsewhere)])
    r = _stage_extra_outputs(_skill(), reply)
    assert not any(_enabled(u) for u in r["updates"])
    assert "no download button" in r["block"]
    assert list(dirs.stage.iterdir()) == []


def test_dotdot_path_that_escapes_the_output_dir_is_not_staged(dirs):
    _write(dirs.tmp / "secret.csv")
    sneaky = str(dirs.out / ".." / "secret.csv")
    r = _stage_extra_outputs(_skill(), ReplyWithOutputs("ok", [extra_output("journal_csv", sneaky)]))
    assert not any(_enabled(u) for u in r["updates"])
    assert list(dirs.stage.iterdir()) == []


def test_reported_file_that_does_not_exist_is_not_offered(dirs):
    ghost = str(dirs.out / "never-written.csv")
    r = _stage_extra_outputs(_skill(), ReplyWithOutputs("ok", [extra_output("journal_csv", ghost)]))
    assert not any(_enabled(u) for u in r["updates"])
    assert "no such file" in r["block"]


def test_monthly_written_accrual_not_only_monthly_is_enabled_and_says_why(dirs):
    m = _write(dirs.out / "ENT-journal.csv")
    reply = ReplyWithOutputs("ok", [
        extra_output("journal_csv", m),
        extra_output("accrual_journal_csv", None, "No accrual journal written: the difference ties."),
    ])
    r = _stage_extra_outputs(_skill(), reply)
    assert [_enabled(u) for u in r["updates"]] == [True, False]
    assert "the difference ties" in r["block"]
    assert r["paths"] == [str(Path(m).resolve())]


def test_skill_declaring_no_extra_outputs_gets_no_block_and_no_buttons(dirs):
    _write(dirs.out / "x-journal.csv")
    skill = _skill(extras=())
    assert _stage_extra_outputs(skill, ReplyWithOutputs("ok", [extra_output("journal_csv", str(dirs.out / "x-journal.csv"))])) is None
    # and the handler keeps its plain 3-element yields
    handler = _make_run_handler(skill)
    first = next(handler("n", "model"))
    assert len(first) == 3


# --- end to end through the real run handler -------------------------------

def _drive(monkeypatch, dirs, run_fn, skill=None):
    skill = skill or _skill()
    import agents.registry as registry
    monkeypatch.setattr(registry, "load_run_function", lambda s: run_fn)
    monkeypatch.setattr(_config, "materialize_legacy_config", lambda active: {})
    monkeypatch.setattr(_generic._runlog, "new_log_path", lambda name: dirs.tmp / "run.log")
    monkeypatch.setattr(_generic._runlog, "write_run_log", lambda *a, **k: None)
    return list(_make_run_handler(skill)("ENT", "model"))


def _fake_run(write_monthly=True, write_accrual=True):
    def run(output_path="", **_kw):
        out = Path(output_path)
        out.write_bytes(b"xlsx")
        folder = out.parent
        entries = []
        m = _write(folder / "ENT-journal.csv") if write_monthly else None
        a = _write(folder / "ENT-accrual.csv") if write_accrual else None
        entries.append(extra_output("journal_csv", m, "" if m else "No journal written: blank."))
        entries.append(extra_output("accrual_journal_csv", a, "" if a else "No accrual journal written: blank."))
        return ReplyWithOutputs("reply text", entries)
    return run


def test_done_panel_order_and_buttons_end_to_end(monkeypatch, dirs):
    ys = _drive(monkeypatch, dirs, _fake_run())
    assert all(len(y) == 3 + len(EXTRAS) + 1 for y in ys)
    # a new run starts with the extra buttons disabled and the path box empty
    first = ys[0]
    assert all(not u.get("interactive") for u in first[3:5]) and first[5].get("value") == ""
    md, main_dl, main_path, j1, j2, jpaths = ys[-1]
    assert md.index("**Saved to:**") < md.index("Journals to import into GnuCash") < md.index("reply text")
    assert _enabled(main_dl) and _enabled(j1) and _enabled(j2)
    assert "ENT-journal.csv" in jpaths["value"] and "ENT-accrual.csv" in jpaths["value"]
    # the extras are never enabled before the final yield
    assert not any(_enabled(u) for y in ys[:-1] for u in y[3:5])


def test_end_to_end_no_journal_written_keeps_buttons_disabled(monkeypatch, dirs):
    _write(dirs.out / "ENT-journal.csv")  # left over from an earlier run
    ys = _drive(monkeypatch, dirs, _fake_run(write_monthly=False, write_accrual=False))
    md, main_dl, _p, j1, j2, jpaths = ys[-1]
    # the earlier run's file was not offered
    assert not _enabled(j1) and not _enabled(j2)
    assert "none written" in md
    assert not jpaths.get("value")


def test_end_to_end_run_that_fails_before_done_leaves_buttons_off(monkeypatch, dirs):
    def boom(**_kw):
        raise RuntimeError("synthetic failure")
    ys = _drive(monkeypatch, dirs, boom)
    assert not any(_enabled(u) for y in ys for u in y[3:5])


def test_reset_clears_the_extra_buttons_and_path_box():
    import gradio as gr
    src = Path(ROOT / "ui" / "tabs" / "_generic.py").read_text(encoding="utf-8")
    # Reset returns a disabled, value-less update for every extra button and a
    # blank one for the path box, in the same order as its outputs list.
    assert "updates.extend(gr.update(interactive=False, value=None) for _ in extra_downloads)" in src
    assert "outputs=[result_md, download, path_tb] + _extra_components + [_c for _c, _fn in reset_specs]" in src
    assert gr  # gradio importable


def test_partner_skill_declares_both_journals_and_the_ui_mounts_them_by_interactive():
    from agents import registry
    skills = {s.name: s for s in registry.discover()}
    s = next(v for v in skills.values() if v.entry_point.startswith("agent:run")
             and "skill_partner_comp_recon" in v.package)
    keys = [x.key for x in s.output.extra_outputs]
    assert keys == ["journal_csv", "accrual_journal_csv"]
    src = Path(ROOT / "ui" / "tabs" / "_generic.py").read_text(encoding="utf-8")
    assert "label=_x.download_label, visible=True, interactive=False" in src
