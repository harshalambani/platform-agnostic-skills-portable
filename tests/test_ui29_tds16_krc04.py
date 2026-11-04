"""UI-29, TDS-16, KRC-04. Synthetic fixtures only."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src"), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------- UI-29
def _tab_parts(skill):
    gr = pytest.importorskip("gradio")
    if not hasattr(gr, "Blocks"):
        pytest.skip("gradio not fully installed")
    from ui.tabs import _generic
    with gr.Blocks() as app:
        _generic.render(skill)
    blocks = list(app.blocks.values())
    # Only the run panel's own button: other tabs (e.g. the KRC review
    # downloads) carry their own labels and are legitimate.
    downloads = [b for b in blocks if type(b).__name__ == "DownloadButton"
                 and getattr(b, "label", None) == skill.output.download_label]
    open_btns = [b for b in blocks
                 if type(b).__name__ == "Button" and getattr(b, "value", None) == "Open output folder"]
    return downloads, open_btns


def _skills(kind):
    from agents import registry
    return [s for s in registry.discover(refresh=True) if s.output.type == kind]


def test_directory_tab_has_one_open_folder_control_and_no_download_button():
    skills = _skills("directory")
    assert skills, "expected at least one directory-output skill"
    for s in skills:
        downloads, open_btns = _tab_parts(s)
        assert len(open_btns) == 1, s.name
        visible = [d for d in downloads if getattr(d, "visible", True)]
        assert visible == [], f"{s.name}: visible download button on a directory tab"


def test_file_tab_still_has_its_download_button():           # NEGATIVE for the fix
    skills = [s for s in _skills("file") if not getattr(s.output, "extra_outputs", ())]
    assert skills
    for s in skills[:5]:
        downloads, open_btns = _tab_parts(s)
        visible = [d for d in downloads if getattr(d, "visible", True)]
        assert len(visible) == 1, s.name
        assert len(open_btns) == 1, s.name


# ---------------------------------------------------------------- TDS-16
def test_part_ii_label_is_one_constant_and_not_respelled():
    from agents.skill_26as_journal.scripts.part_labels import PART_II_LABEL
    assert PART_II_LABEL == "Part II (15G/15H)"
    files = [
        ROOT / "src/agents/skill_26as_journal/scripts/build_tds_journals.py",
        ROOT / "src/agents/skill_26as_journal/tools.py",
        ROOT / "ui/tabs/tds_journal_review.py",
    ]
    spellings = [re.compile(r"Part II \(15G\s*/\s*15H\)"), re.compile(r"15G\s*/\s*15H \(Part II\)")]
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            for rx in spellings:
                assert not rx.search(code), f"{f.name}:{n} spells the Part II label"
    # the screens really use it
    assert "PART_II_LABEL" in files[2].read_text(encoding="utf-8")
    assert "PART_II_LABEL" in files[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------- KRC-04
import test_krc_gnucash_flags_charges as K  # noqa: E402

WRONG_CLOSING_LEDGER = K.GOOD_LEDGER[:-1] + [K.GOOD_LEDGER[-1][:4] + (300.0,) + K.GOOD_LEDGER[-1][5:]]


def _csvs(out):
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(out.glob("*.csv"))}


def test_info_line_shown_on_every_run_and_not_a_red_flag_when_passing(tmp_path):
    r, out = K._run(tmp_path, K.BUY, K.GOOD_LEDGER,
                    ["--entity", "ent1", "--demat-account", "Expenses:Demat Charges"])
    assert r.returncode == 0, r.stdout + r.stderr
    info = [ln for ln in r.stdout.splitlines() if ln.startswith("INFO: broker account closing balance")]
    assert len(info) == 1 and "350.00" in info[0]
    assert "RED FLAG" not in r.stdout                         # NEGATIVE
    assert "INFO" not in "".join(_csvs(out).values())          # NEGATIVE: no CSV row carries it


def test_info_line_also_on_a_difference_and_rows_unchanged(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    extra = ["--entity", "ent1", "--demat-account", "Expenses:Demat Charges"]
    ok, out_ok = K._run(tmp_path / "a", K.BUY, K.GOOD_LEDGER, extra, outname="a-KRC-GnuCash")
    bad, out_bad = K._run(tmp_path / "b", K.BUY, WRONG_CLOSING_LEDGER, extra, outname="b-KRC-GnuCash")
    assert "INFO: broker account closing balance" in bad.stdout
    assert "RED FLAG - Broker account will NOT close" in bad.stdout
    # the ledger only drives the check: every CSV row is identical either way
    assert _csvs(out_ok).keys() == _csvs(out_bad).keys()
    for name, text in _csvs(out_ok).items():
        assert text == _csvs(out_bad)[name], name              # NEGATIVE: rows never altered
