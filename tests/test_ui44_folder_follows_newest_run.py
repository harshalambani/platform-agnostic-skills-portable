"""UI-44: an auto-picked CC Sort folder follows the newest run; a typed one never moves."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from ui.tabs import _generic  # noqa: E402


def _run(tmp_path, name):
    (tmp_path / name / "Decrypted_PDFs_Correct").mkdir(parents=True)
    return str(tmp_path / name / "Decrypted_PDFs_Correct")


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(_generic._config, "output_dir", lambda: tmp_path)


def test_auto_picked_value_moves_to_the_newest_run(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    old = _run(tmp_path, "2026-01-01-CC-Sort")
    first = _generic._refresh_folder_tracked("cc_sort_runs", None, "")
    assert first[0]["value"] == old and first[1] == old
    new = _run(tmp_path, "2026-03-01-CC-Sort")
    upd, auto = _generic._refresh_folder_tracked("cc_sort_runs", old, old)
    assert upd["value"] == new and auto == new


def test_typed_path_survives_refresh(tmp_path, monkeypatch):              # NEGATIVE
    _setup(tmp_path, monkeypatch)
    old = _run(tmp_path, "2026-01-01-CC-Sort")
    _run(tmp_path, "2026-03-01-CC-Sort")
    upd, auto = _generic._refresh_folder_tracked("cc_sort_runs", "D:/my/own/folder", old)
    assert "value" not in upd
    assert auto == old                  # the auto marker is not hijacked by a typed path


def test_an_older_run_chosen_from_the_list_is_kept(tmp_path, monkeypatch):    # NEGATIVE
    _setup(tmp_path, monkeypatch)
    old = _run(tmp_path, "2026-01-01-CC-Sort")
    new = _run(tmp_path, "2026-03-01-CC-Sort")
    upd, auto = _generic._refresh_folder_tracked("cc_sort_runs", old, new)   # auto-picked was the newest
    assert "value" not in upd and auto == new


def test_typed_path_equal_to_nothing_known_stays_after_a_new_run_appears(tmp_path, monkeypatch):   # NEGATIVE
    _setup(tmp_path, monkeypatch)
    typed = str(tmp_path / "elsewhere")
    _run(tmp_path, "2026-03-01-CC-Sort")
    upd, _ = _generic._refresh_folder_tracked("cc_sort_runs", typed, "")
    assert "value" not in upd


def test_old_two_argument_call_still_keeps_any_value(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    old = _run(tmp_path, "2026-01-01-CC-Sort")
    _run(tmp_path, "2026-03-01-CC-Sort")
    assert "value" not in _generic._refresh_folder_choice("cc_sort_runs", old)
