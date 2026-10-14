"""UI-33: CC Transactions and CC Spend Booking offer the CC Sort runs' folders."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents import registry  # noqa: E402
from ui.tabs import _generic  # noqa: E402


def _make_out(tmp_path, monkeypatch, runs):
    for name, has_folder in runs:
        d = tmp_path / name
        d.mkdir()
        if has_folder:
            (d / "Decrypted_PDFs_Correct").mkdir()
    monkeypatch.setattr(_generic._config, "output_dir", lambda: tmp_path)


def test_newest_first_labelled_by_run_name(tmp_path, monkeypatch):
    _make_out(tmp_path, monkeypatch, [("2026-01-01-CC-Sort", True), ("2026-03-01-CC-Sort", True)])
    got = _generic._known_folders("cc_sort_runs")
    assert [lbl for lbl, _ in got] == ["2026-03-01-CC-Sort", "2026-01-01-CC-Sort"]
    assert got[0][1].endswith("Decrypted_PDFs_Correct")
    assert _generic._newest_cc_sort_pdfs() == got[0][1]


def test_run_without_correct_folder_is_never_offered(tmp_path, monkeypatch):
    _make_out(tmp_path, monkeypatch, [("2026-03-01-CC-Sort", False), ("2026-01-01-CC-Sort", True)])
    got = _generic._known_folders("cc_sort_runs")
    assert [lbl for lbl, _ in got] == ["2026-01-01-CC-Sort"]


def test_typed_path_is_never_replaced(tmp_path, monkeypatch):
    _make_out(tmp_path, monkeypatch, [("2026-03-01-CC-Sort", True)])
    upd = _generic._refresh_folder_choice("cc_sort_runs", "D:/my/own/folder")
    assert "value" not in upd           # choices change, value untouched
    assert len(upd["choices"]) == 1
    # only an empty box is filled with the newest run
    assert _generic._refresh_folder_choice("cc_sort_runs", "")["value"].endswith("Decrypted_PDFs_Correct")


def test_no_runs_is_empty_and_quiet(tmp_path, monkeypatch):
    _make_out(tmp_path, monkeypatch, [])
    assert _generic._known_folders("cc_sort_runs") == []
    assert _generic._newest_cc_sort_pdfs() is None
    assert _generic._refresh_folder_choice("cc_sort_runs", "")["value"] is None
    assert _generic._known_folders("no_such_source") == []


def test_output_dir_failure_is_quiet(monkeypatch):
    def boom():
        raise RuntimeError("no config")
    monkeypatch.setattr(_generic._config, "output_dir", boom)
    assert _generic._known_folders("cc_sort_runs") == []


def test_both_skills_declare_the_same_source():
    skills = {s.name: s for s in registry.discover(refresh=True)}
    srcs = {}
    for n in ("CC Transactions", "CC Book Spends"):
        s = skills[n]
        srcs[n] = [i.folders_from for i in s.inputs if i.name == "pdf_dir"]
    assert srcs["CC Transactions"] == srcs["CC Book Spends"] == ["cc_sort_runs"]
