"""
SEC-19 -- path injection guard for the review tabs (CodeQL py/path-injection).
Synthetic files only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ui import _safe_paths as sp  # noqa: E402
from ui.tabs import gnucash_review as gr_review  # noqa: E402
from ui.tabs import tds_journal_review as tjr  # noqa: E402

CSV = "Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n" \
      "2025-04-01,SYNTHETIC ROW,Expenses:Misc,,10.00,90.00,high,rule\n"


@pytest.fixture
def folders(tmp_path, monkeypatch):
    inside = tmp_path / "Data"
    outside = tmp_path / "elsewhere"
    inside.mkdir(); outside.mkdir()
    monkeypatch.setattr(sp, "known_folders", lambda: [inside.resolve()])
    return inside, outside


def _csv(d, name="a.csv"):
    p = d / name
    p.write_text(CSV, encoding="utf-8")
    return p


# ------------------------------------------------------------- the helper

def test_normal_csv_resolves(folders):
    inside, _ = folders
    assert sp.resolve_input_file(_csv(inside), (".csv",)) == (inside / "a.csv").resolve()


def test_outside_path_refused(folders):                                   # NEGATIVE
    _, outside = folders
    with pytest.raises(sp.UnsafePathError, match="outside"):
        sp.resolve_input_file(_csv(outside), (".csv",))


def test_dotdot_escape_refused(folders):                                  # NEGATIVE
    inside, outside = folders
    sneaky = str(inside / ".." / "elsewhere" / "a.csv")
    _csv(outside)
    with pytest.raises(sp.UnsafePathError, match="outside"):
        sp.resolve_input_file(sneaky, (".csv",))


@pytest.mark.parametrize("raw", ["", None, "   ", "a\x00.csv"])
def test_empty_or_nul_refused(folders, raw):                              # NEGATIVE
    with pytest.raises(sp.UnsafePathError):
        sp.resolve_input_file(raw, (".csv",))


def test_directory_missing_and_wrong_extension_refused(folders):          # NEGATIVE
    inside, _ = folders
    (inside / "dir.csv").mkdir()
    with pytest.raises(sp.UnsafePathError, match="not a regular file"):
        sp.resolve_input_file(inside / "dir.csv", (".csv",))
    with pytest.raises(sp.UnsafePathError, match="not found"):
        sp.resolve_input_file(inside / "nope.csv", (".csv",))
    (inside / "a.txt").write_text("x")
    with pytest.raises(sp.UnsafePathError, match="expected"):
        sp.resolve_input_file(inside / "a.txt", (".csv",))


@pytest.mark.parametrize("name", ["", ".", "..", "C:evil", "a\x00b"])
def test_staged_name_rejects_traversal_names(name):                       # NEGATIVE
    with pytest.raises(sp.UnsafePathError):
        sp.safe_staged_name(name)


def test_staged_name_strips_directory_parts():
    assert sp.safe_staged_name("../../evil/x.csv") == "x.csv"
    assert sp.safe_staged_name("..\\..\\x.csv") == "x.csv"


def test_stage_copy_lands_inside_staging(tmp_path):
    src = tmp_path / "s.csv"; src.write_text(CSV)
    staging = tmp_path / "stage"
    got = sp.stage_copy(src, staging, folders=[tmp_path])
    assert got.parent == staging.resolve() and got.read_text() == CSV


def test_default_known_folders_are_data_and_outputs(monkeypatch, tmp_path):
    from ui import _config
    monkeypatch.setattr(_config, "data_root_dir", lambda: tmp_path / "D")
    monkeypatch.setattr(_config, "output_dir", lambda: tmp_path / "O")
    monkeypatch.setattr(_config, "load_portable_config", lambda: {})
    got = sp.known_folders()
    assert got == [(tmp_path / "D").resolve(), (tmp_path / "O").resolve()]
    monkeypatch.setattr(_config, "load_portable_config",
                        lambda: {sp.KNOWN_FOLDERS_SETTING: [str(tmp_path / "X")]})
    assert (tmp_path / "X").resolve() in sp.known_folders()


# ------------------------------------------------------------- the tabs

def test_gnucash_load_normal_csv_still_loads(folders):
    inside, _ = folders
    p = _csv(inside)
    html = gr_review._load_review_data(str(p), str(p))
    assert "SYNTHETIC ROW" in html


def test_gnucash_load_outside_path_refused_and_never_opened(folders, monkeypatch):   # NEGATIVE
    _, outside = folders
    p = _csv(outside)
    opened = []
    real = open
    monkeypatch.setattr("builtins.open",
                        lambda f, *a, **k: (opened.append(str(f)), real(f, *a, **k))[1])
    html = gr_review._load_review_data(str(p), str(p))
    assert "outside" in html and "SYNTHETIC ROW" not in html
    assert not any(str(p) in o for o in opened)


def test_tds_load_outside_path_refused(folders):                          # NEGATIVE
    _, outside = folders
    p = _csv(outside)
    assert "outside" in tjr._load_review_data(str(p), "")


def test_gnucash_save_refuses_a_forged_csv_path(folders):                 # NEGATIVE
    _, outside = folders
    target = _csv(outside, "victim.csv")
    before = target.read_text()
    payload = json.dumps({"context": {"csv_path": str(target), "gnucash_file": ""},
                          "changes": [], "all_rows": [{"Description": "x", "Account": "A"}],
                          "excluded_dirty": True, "excluded": []})
    msg, _ = gr_review._save_changes(payload)
    assert "refused" in msg.lower()
    assert target.read_text() == before


def test_tds_stage_normal_journal_stages(folders, tmp_path, monkeypatch):
    inside, _ = folders
    stage = tmp_path / "stage"
    monkeypatch.setattr(tjr._config_mod, "download_staging_dir", lambda: stage)
    out = tjr._stage_for_download(_csv(inside, "journal.csv"))
    assert Path(out).parent == stage.resolve() and Path(out).read_text() == CSV


def test_tds_stage_outside_source_refused_nothing_copied(folders, tmp_path, monkeypatch):   # NEGATIVE
    _, outside = folders
    stage = tmp_path / "stage"
    monkeypatch.setattr(tjr._config_mod, "download_staging_dir", lambda: stage)
    with pytest.raises(sp.UnsafePathError):
        tjr._stage_for_download(_csv(outside))
    assert not stage.exists() or not any(stage.iterdir())


def test_tds_staged_copy_never_leaves_staging_for_odd_names(folders, tmp_path, monkeypatch):   # NEGATIVE
    inside, _ = folders
    stage = tmp_path / "stage"
    monkeypatch.setattr(tjr._config_mod, "download_staging_dir", lambda: stage)
    src = _csv(inside, "x.csv")

    class Odd:                       # a source whose name carries traversal bits
        name = "..\\..\\escape.csv"
        def __fspath__(self): return str(src)
    got = sp.stage_copy(Odd(), stage)
    assert got.parent == stage.resolve() and got.name == "escape.csv"
    assert not (tmp_path / "escape.csv").exists()


def test_sibling_folder_with_the_same_prefix_is_refused(tmp_path, monkeypatch):   # NEGATIVE
    data = tmp_path / "Data"
    sibling = tmp_path / "Data2"
    data.mkdir(); sibling.mkdir()
    monkeypatch.setattr(sp, "known_folders", lambda: [data.resolve()])
    _csv(data, "ok.csv")
    _csv(sibling, "no.csv")
    assert sp.resolve_input_file(data / "ok.csv", (".csv",)).name.lower() == "ok.csv"
    with pytest.raises(sp.UnsafePathError, match="outside"):
        sp.resolve_input_file(sibling / "no.csv", (".csv",))


def test_stage_copy_destination_is_checked_before_any_copy(tmp_path):            # NEGATIVE
    src = tmp_path / "s.csv"; src.write_text(CSV)
    staging = tmp_path / "stage"

    class Odd:
        name = "a/../../escape.csv"
        def __fspath__(self): return str(src)
    got = sp.stage_copy(Odd(), staging, folders=[tmp_path])
    assert got.parent == staging.resolve()
    assert not (tmp_path / "escape.csv").exists()


def test_stage_copy_refuses_a_source_outside_known_folders(tmp_path):              # NEGATIVE
    src = tmp_path / "s.csv"; src.write_text(CSV)
    other = tmp_path / "elsewhere"; other.mkdir()
    staging = tmp_path / "stage"
    with pytest.raises(sp.UnsafePathError, match="outside"):
        sp.stage_copy(src, staging, folders=[other])
    assert not any(staging.iterdir())


def test_stage_copy_refuses_unregistered_and_dotdot_sources(tmp_path):            # NEGATIVE
    known = tmp_path / "known"; known.mkdir()
    secret = tmp_path / "secret.csv"; secret.write_text(CSV)
    staging = tmp_path / "stage"
    with pytest.raises(sp.UnsafePathError):
        sp.stage_copy(secret, staging, folders=[known])
    escaping = known / ".." / "secret.csv"
    with pytest.raises(sp.UnsafePathError):
        sp.stage_copy(escaping, staging, folders=[known])
    assert not any(staging.iterdir())


def test_stage_copy_copies_a_source_inside_a_known_folder(tmp_path):
    known = tmp_path / "known"; known.mkdir()
    ok = known / "ok.csv"; ok.write_text(CSV)
    got = sp.stage_copy(ok, tmp_path / "stage", folders=[known])
    assert got.read_text() == CSV and got.name == "ok.csv"
