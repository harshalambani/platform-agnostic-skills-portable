"""
tests/test_filedialog.py — per-box path memory + pick validation (ui/_filedialog.py).

The native Win32 dialog (`_native_open_dialog`) is modal and not headless-
testable, so it is monkeypatched. What we test is the layer around it: the
per-box folder memory persisted to config.yaml, the extension/size validation
re-applied to a picked path (the browser filter and upload caps are bypassed by
a server-side pick), and the OPENFILENAME buffer parsing.

Run with:
    cd src && python -m pytest ../tests/test_filedialog.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ui import _filedialog  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    """A throwaway config.yaml seeded with an unrelated key, so we can prove the
    per-box writes don't clobber the rest of the file."""
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"active_endpoint": "local"}), encoding="utf-8")
    return p


def _make_pdf(dir_: Path, name: str, size: int = 10) -> Path:
    f = dir_ / name
    f.write_bytes(b"%PDF-1.4\n" + b"0" * size)
    return f


# ---------------------------------------------------------------------------
# Per-box folder memory
# ---------------------------------------------------------------------------

def test_remember_and_recall_roundtrip(cfg_path, tmp_path):
    folder = str(tmp_path)
    _filedialog.remember_dir("KRC Reconcile.cn_dir", folder, path=cfg_path)
    assert _filedialog.last_dir_for("KRC Reconcile.cn_dir", path=cfg_path) == folder
    # The unrelated key must survive the write.
    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["active_endpoint"] == "local"
    assert saved["last_dirs"]["KRC Reconcile.cn_dir"] == folder


def test_boxes_are_remembered_independently(cfg_path, tmp_path):
    a = tmp_path / "ledgers"
    a.mkdir()
    b = tmp_path / "notes"
    b.mkdir()
    _filedialog.remember_dir("KRC Reconcile.ledger_xlsx", str(a), path=cfg_path)
    _filedialog.remember_dir("KRC Reconcile.cn_dir", str(b), path=cfg_path)
    assert _filedialog.last_dir_for("KRC Reconcile.ledger_xlsx", path=cfg_path) == str(a)
    assert _filedialog.last_dir_for("KRC Reconcile.cn_dir", path=cfg_path) == str(b)


def test_recall_ignores_a_folder_that_no_longer_exists(cfg_path, tmp_path):
    gone = tmp_path / "was-here"
    gone.mkdir()
    _filedialog.remember_dir("s.box", str(gone), path=cfg_path)
    gone.rmdir()
    assert _filedialog.last_dir_for("s.box", path=cfg_path) is None


def test_unknown_box_recalls_none(cfg_path):
    assert _filedialog.last_dir_for("never.seen", path=cfg_path) is None


# ---------------------------------------------------------------------------
# pick_files — validation + remember (native dialog monkeypatched)
# ---------------------------------------------------------------------------

def test_pick_remembers_parent_and_returns_path(cfg_path, tmp_path, monkeypatch):
    pdf = _make_pdf(tmp_path, "note.pdf")
    monkeypatch.setattr(_filedialog, "_native_open_dialog", lambda **k: [str(pdf)])
    valid, warnings = _filedialog.pick_files(
        "KRC Reconcile.cn_dir", multiple=False, file_types=(".pdf",),
        max_size_bytes=100 * 1024 * 1024, path=cfg_path,
    )
    assert valid == [str(pdf)]
    assert warnings == []
    # The folder is now remembered for next time.
    assert _filedialog.last_dir_for("KRC Reconcile.cn_dir", path=cfg_path) == str(tmp_path)


def test_pick_opens_at_remembered_folder(cfg_path, tmp_path, monkeypatch):
    _filedialog.remember_dir("s.box", str(tmp_path), path=cfg_path)
    seen = {}
    def _spy(**kwargs):
        seen.update(kwargs)
        return []
    monkeypatch.setattr(_filedialog, "_native_open_dialog", _spy)
    _filedialog.pick_files(
        "s.box", multiple=False, file_types=(".pdf",),
        max_size_bytes=100 * 1024 * 1024, path=cfg_path,
    )
    assert seen["initialdir"] == str(tmp_path)


def test_pick_rejects_wrong_extension(cfg_path, tmp_path, monkeypatch):
    txt = tmp_path / "notes.txt"
    txt.write_text("nope")
    monkeypatch.setattr(_filedialog, "_native_open_dialog", lambda **k: [str(txt)])
    valid, warnings = _filedialog.pick_files(
        "s.box", multiple=False, file_types=(".pdf",),
        max_size_bytes=100 * 1024 * 1024, path=cfg_path,
    )
    assert valid == []
    assert any("unsupported type" in w for w in warnings)
    # A fully-rejected pick must not poison the remembered folder.
    assert _filedialog.last_dir_for("s.box", path=cfg_path) is None


def test_pick_rejects_oversized_file(cfg_path, tmp_path, monkeypatch):
    big = _make_pdf(tmp_path, "big.pdf", size=2000)
    monkeypatch.setattr(_filedialog, "_native_open_dialog", lambda **k: [str(big)])
    valid, warnings = _filedialog.pick_files(
        "s.box", multiple=False, file_types=(".pdf",),
        max_size_bytes=1024, path=cfg_path,  # 1 KB cap
    )
    assert valid == []
    assert any("too large" in w for w in warnings)


def test_pick_multiple_keeps_valid_drops_invalid(cfg_path, tmp_path, monkeypatch):
    good = _make_pdf(tmp_path, "a.pdf")
    bad = tmp_path / "b.txt"
    bad.write_text("x")
    good2 = _make_pdf(tmp_path, "c.pdf")
    monkeypatch.setattr(
        _filedialog, "_native_open_dialog",
        lambda **k: [str(good), str(bad), str(good2)],
    )
    valid, warnings = _filedialog.pick_files(
        "s.box", multiple=True, file_types=(".pdf",),
        max_size_bytes=100 * 1024 * 1024, path=cfg_path,
    )
    assert valid == [str(good), str(good2)]
    assert len(warnings) == 1


def test_pick_cancel_returns_empty(cfg_path, monkeypatch):
    monkeypatch.setattr(_filedialog, "_native_open_dialog", lambda **k: [])
    valid, warnings = _filedialog.pick_files(
        "s.box", multiple=False, file_types=(".pdf",),
        max_size_bytes=100 * 1024 * 1024, path=cfg_path,
    )
    assert valid == [] and warnings == []


# ---------------------------------------------------------------------------
# OPENFILENAME buffer parsing + filter building
# ---------------------------------------------------------------------------

def test_parse_single_selection():
    assert _filedialog._parse_ofn_buffer("C:\\docs\\a.pdf\x00") == ["C:\\docs\\a.pdf"]


def test_parse_multi_selection():
    raw = "C:\\docs\x00a.pdf\x00b.pdf\x00\x00"
    assert _filedialog._parse_ofn_buffer(raw) == [
        str(Path("C:\\docs") / "a.pdf"),
        str(Path("C:\\docs") / "b.pdf"),
    ]


def test_parse_empty():
    assert _filedialog._parse_ofn_buffer("\x00\x00") == []


def test_build_filter_has_supported_and_all():
    f = _filedialog._build_filter((".pdf", ".csv"))
    assert "*.pdf;*.csv" in f
    assert f.endswith("*.*\x00")
