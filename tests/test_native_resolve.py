"""
tests/test_native_resolve.py — Tests for agents._native_resolve, the
absolute-path resolver + identity gate used by the standalone skill scripts
that shell out to pdftotext (Poppler) and qpdf (see the 26AS/Xpdf incident:
Xpdf's pdftotext is a fully functional, exit-0 binary that silently produces
wrong-layout text under a different name).

No PII, no real binaries, no real PDFs — every test uses synthetic fixtures
and monkeypatched probes / filesystem layouts.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import _native_resolve as nr  # noqa: E402
from ui import _native as ui_native  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    """Every test starts with a clean identity cache — the cache is a module
    global and must not leak state between tests (or between the two
    different binaries a single test may probe)."""
    nr._IDENTITY_CACHE.clear()
    yield
    nr._IDENTITY_CACHE.clear()


POPPLER_BANNER = (
    "pdftotext version 25.07.0\n"
    "Copyright 2005-2025 The Poppler Developers\n"
    "Copyright 1996-2011 Glyph & Cog, LLC\n"
)

XPDF_BANNER = (
    "pdftotext version 4.06 [www.xpdfreader.com]\n"
    "Copyright 1996-2025 Glyph & Cog, LLC\n"
)


# ---------------------------------------------------------------------------
# _candidate_roots() drift test — must stay in lockstep with ui/_native.py.
# This module is a SELF-CONTAINED mirror (the scripts using it cannot import
# ui/) of ui/_native.py's resolution order; if the two ever diverge, a
# standalone script and the UI's own health pill would disagree about where
# binaries live.
# ---------------------------------------------------------------------------

def test_candidate_roots_mirrors_ui_native_source_mode(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    ui_popp, ui_qpdf, ui_mode = (
        lambda: (lambda r: (r[1], r[2], r[3]))(ui_native._candidate_roots())
    )()
    nr_popp, nr_qpdf, nr_mode = nr._candidate_roots()
    assert nr_mode == ui_mode == "source"
    assert nr_popp == ui_popp
    assert nr_qpdf == ui_qpdf


def test_candidate_roots_mirrors_ui_native_frozen_mode(monkeypatch, tmp_path):
    fake_exe = tmp_path / "pa_skills.exe"
    fake_exe.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    _, ui_popp, ui_qpdf, ui_mode = ui_native._candidate_roots()
    nr_popp, nr_qpdf, nr_mode = nr._candidate_roots()
    assert nr_mode == ui_mode == "frozen"
    assert nr_popp == ui_popp == fake_exe.parent / "poppler"
    assert nr_qpdf == ui_qpdf == fake_exe.parent / "qpdf"


# ---------------------------------------------------------------------------
# Resolution: vendored copy wins over PATH; PATH is only a fallback.
# ---------------------------------------------------------------------------

def _make_fake_vendor(tmp_path, tool: str, exe_name: str) -> Path:
    bin_dir = tmp_path / "vendor" / tool / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    exe = bin_dir / f"{exe_name}{nr._EXE_SUFFIX}"
    exe.write_text("fake binary\n")
    return exe


def test_resolve_prefers_vendored_over_path(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    vendored = _make_fake_vendor(tmp_path, "poppler", "pdftotext")
    monkeypatch.setattr(nr, "PROJECT_ROOT", tmp_path)
    # Even if shutil.which would also resolve something, the vendored copy
    # must win.
    monkeypatch.setattr(nr.shutil, "which", lambda name: r"C:\Somewhere\Else\pdftotext.exe")
    path = nr._resolve_pdftotext_path()
    assert path == str(vendored)


def test_resolve_falls_back_to_path_when_vendor_absent(monkeypatch, tmp_path):
    """Vendored-binary-ABSENT case: rename/point away from vendor/poppler,
    confirm the resolver falls back to shutil.which() and reports clearly
    when even that fails."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(nr, "PROJECT_ROOT", tmp_path)  # empty tmp_path: no vendor/ dir at all

    # Case 1: nothing on PATH either -> clear FileNotFoundError naming what's missing.
    monkeypatch.setattr(nr.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError) as exc:
        nr._resolve_pdftotext_path()
    assert "pdftotext" in str(exc.value)
    assert "vendor" in str(exc.value) or "PATH" in str(exc.value)

    # Case 2: absent from vendor but present on PATH -> falls back cleanly.
    monkeypatch.setattr(nr.shutil, "which", lambda name: r"C:\mingw64\bin\pdftotext.exe" if name == "pdftotext" else None)
    path = nr._resolve_pdftotext_path()
    assert path == r"C:\mingw64\bin\pdftotext.exe"


def test_resolve_qpdf_prefers_vendored(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    vendored = _make_fake_vendor(tmp_path, "qpdf", "qpdf")
    monkeypatch.setattr(nr, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(nr.shutil, "which", lambda name: None)
    assert nr.resolve_qpdf() == str(vendored)


def test_frozen_layout_resolved(monkeypatch, tmp_path):
    """Exercise the frozen branch end-to-end: a simulated
    <frozen_root>/poppler/bin/pdftotext.exe layout must resolve, without a
    real PyInstaller build."""
    fake_exe = tmp_path / "pa_skills.exe"
    fake_exe.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    bin_dir = tmp_path / "poppler" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / f"pdftotext{nr._EXE_SUFFIX}"
    exe.write_text("fake")
    monkeypatch.setattr(nr.shutil, "which", lambda name: None)
    assert nr._resolve_pdftotext_path() == str(exe)


# ---------------------------------------------------------------------------
# Tier 3 identity gate — testable without either real binary via probe
# injection.
# ---------------------------------------------------------------------------

def test_identity_gate_accepts_poppler():
    nr.verify_pdftotext_is_poppler(r"C:\vendor\poppler\bin\pdftotext.exe",
                                    probe=lambda p: POPPLER_BANNER)


def test_identity_gate_rejects_xpdf():
    with pytest.raises(nr.WrongPdftextFlavourError) as exc:
        nr.verify_pdftotext_is_poppler(r"C:\mingw64\bin\pdftotext.exe",
                                        probe=lambda p: XPDF_BANNER)
    msg = str(exc.value)
    assert "Xpdf" in msg
    assert r"C:\mingw64\bin\pdftotext.exe" in msg


def test_identity_gate_reports_missing_binary_distinctly():
    def _missing(path):
        raise FileNotFoundError(f"[WinError 2] The system cannot find the file specified: '{path}'")

    with pytest.raises(nr.WrongPdftextFlavourError) as exc:
        nr.verify_pdftotext_is_poppler(r"C:\ghost\pdftotext.exe", probe=_missing)
    msg = str(exc.value)
    # Missing-binary wording must be distinguishable from wrong-flavour wording.
    assert "not be executed" in msg or "not found" in msg
    assert "Xpdf" not in msg


def test_identity_gate_rejects_unrecognised_binary():
    with pytest.raises(nr.WrongPdftextFlavourError) as exc:
        nr.verify_pdftotext_is_poppler("/usr/bin/pdftotext", probe=lambda p: "some other tool v1.0\n")
    assert "unrecognised" in str(exc.value)


def test_identity_cache_short_circuits_reprobe():
    calls = []

    def _probe(path):
        calls.append(path)
        return POPPLER_BANNER

    nr.verify_pdftotext_is_poppler("C:/a/pdftotext.exe", probe=_probe)
    nr.verify_pdftotext_is_poppler("C:/a/pdftotext.exe", probe=_probe)
    nr.verify_pdftotext_is_poppler("C:/a/pdftotext.exe", probe=_probe)
    assert calls == ["C:/a/pdftotext.exe"]  # only probed once


def test_identity_cache_keyed_per_path_not_a_single_global_flag():
    """A second, DIFFERENT resolved binary must be probed independently --
    caching must not be a single global "already checked" flag that would
    mask e.g. PATH being mutated mid-batch to point at a different binary."""
    def _probe_good(path):
        return POPPLER_BANNER

    def _probe_bad(path):
        return XPDF_BANNER

    # First path: poppler, accepted and cached.
    nr.verify_pdftotext_is_poppler("C:/good/pdftotext.exe", probe=_probe_good)

    # Second, DIFFERENT path: Xpdf -- must still be rejected even though a
    # different path was just accepted.
    with pytest.raises(nr.WrongPdftextFlavourError):
        nr.verify_pdftotext_is_poppler("C:/bad/pdftotext.exe", probe=_probe_bad)

    # Re-checking the first (good) path again must still succeed from cache.
    nr.verify_pdftotext_is_poppler("C:/good/pdftotext.exe", probe=_probe_good)

    # And re-checking the second (bad) path again must still raise -- a
    # cached False for one path must never bleed into a True for another.
    with pytest.raises(nr.WrongPdftextFlavourError):
        nr.verify_pdftotext_is_poppler("C:/bad/pdftotext.exe", probe=_probe_bad)


def test_resolve_pdftotext_end_to_end_rejects_wrong_flavour(monkeypatch, tmp_path):
    """resolve_pdftotext() wires resolution + verification together: a
    vendored-looking binary that is actually Xpdf must raise, not return."""
    monkeypatch.delattr(sys, "frozen", raising=False)
    exe = _make_fake_vendor(tmp_path, "poppler", "pdftotext")
    monkeypatch.setattr(nr, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(nr, "_probe_pdftotext_banner", lambda p: XPDF_BANNER)
    with pytest.raises(nr.WrongPdftextFlavourError) as exc:
        nr.resolve_pdftotext()
    assert str(exe) in str(exc.value)
    assert "Xpdf" in str(exc.value)


def test_resolve_pdftotext_end_to_end_accepts_poppler(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    exe = _make_fake_vendor(tmp_path, "poppler", "pdftotext")
    monkeypatch.setattr(nr, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(nr, "_probe_pdftotext_banner", lambda p: POPPLER_BANNER)
    assert nr.resolve_pdftotext() == str(exe)
