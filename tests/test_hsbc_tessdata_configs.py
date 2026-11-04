"""
tests/test_hsbc_tessdata_configs.py — regression guards for ledger items
HSB-01 / HSB-02.

HSB-01: bundling/refresh_binaries.py::install_tesseract() dropped the whole
tessdata/configs/ directory, keeping only eng.traineddata. tessdata/configs/tsv
is a REQUIRED RUNTIME CONFIG (not language data) -- `tesseract <img> <out> tsv`
in ocr_to_tsv.py passes "tsv" as the name of that config file, not a CLI flag.
Without it, tesseract prints "read_params_file: Can't open tsv" to stderr,
still exits 0, and silently falls back to writing a plain .txt file instead of
the expected .tsv -- a failure that used to run silently through ~270s of OCR
before parse_tsv.py found nothing to glob.

HSB-02: ocr_to_tsv.py trusted tesseract's exit code alone. It now asserts the
expected .tsv file exists and is non-empty after each call, and raises
immediately naming the missing file and the likely cause.

Covers:
  - a packaging test: the staged tesseract tree must contain
    tessdata/configs/tsv (fails if configs/ is dropped again);
  - ocr_pdf() raises on the FIRST page, not after all pages, when tesseract
    exits 0 having written nothing;
  - ocr_pdf() raises the same way when tesseract exits 0 having written a
    .txt file instead of a .tsv (the actual real-world failure mode).
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import bundling.refresh_binaries as refresh_binaries  # noqa: E402

SCRIPTS_DIR = ROOT / "src" / "agents" / "skill_hsbc" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
import ocr_to_tsv  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_tesseract_zip() -> Path:
    """Build an in-memory-then-on-disk zip mimicking the UB-Mannheim layout:
    a single top-level folder containing tesseract.exe, a DLL, and a
    tessdata/ tree with eng.traineddata, some OTHER-language traineddata
    (must be dropped), and a configs/ dir including the required tsv file
    plus another config (must be kept in full)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zw:
        zw.writestr("tesseract/tesseract.exe", b"FAKEEXE")
        zw.writestr("tesseract/libsomething.dll", b"FAKEDLL")
        zw.writestr("tesseract/tessdata/eng.traineddata", b"ENGDATA")
        zw.writestr("tesseract/tessdata/osd.traineddata", b"OSDDATA")
        zw.writestr("tesseract/tessdata/configs/tsv", b"tessedit_create_tsv 1\n")
        zw.writestr("tesseract/tessdata/configs/txt", b"tessedit_create_txt 1\n")
    return buf


# ---------------------------------------------------------------------------
# HSB-01 -- packaging: tessdata/configs/tsv must survive install_tesseract()
# ---------------------------------------------------------------------------

def test_install_tesseract_stages_tessdata_configs_tsv(tmp_path, monkeypatch):
    fake_vendor = tmp_path / "vendor"
    monkeypatch.setattr(refresh_binaries, "VENDOR", fake_vendor)

    zip_path = tmp_path / "tesseract.zip"
    zip_path.write_bytes(_make_fake_tesseract_zip().getvalue())

    work = tmp_path / "work"
    work.mkdir()
    refresh_binaries.install_tesseract({"_local_zip": zip_path}, work)

    dest = fake_vendor / "tesseract"
    assert (dest / "tesseract.exe").exists()
    assert (dest / "tessdata" / "eng.traineddata").exists()
    # eng-only behaviour for language data is preserved.
    assert not (dest / "tessdata" / "osd.traineddata").exists()

    # The regression: configs/ (and specifically configs/tsv) must be staged.
    configs_dir = dest / "tessdata" / "configs"
    assert configs_dir.is_dir(), "tessdata/configs/ was dropped -- this is what broke HSBC OCR"
    assert (configs_dir / "tsv").exists()
    assert (configs_dir / "tsv").read_bytes() == b"tessedit_create_tsv 1\n"
    # The whole configs/ dir is kept (it's a fixed-size runtime config set,
    # not per-language data), so the other config file present in the
    # archive must also survive.
    assert (configs_dir / "txt").exists()


def test_install_tesseract_raises_if_configs_dir_absent_from_archive(tmp_path, monkeypatch):
    """Negative test: if a future Tesseract build ships with no
    tessdata/configs/ at all, install_tesseract() must fail loudly at
    package time rather than silently producing a broken vendor tree."""
    fake_vendor = tmp_path / "vendor"
    monkeypatch.setattr(refresh_binaries, "VENDOR", fake_vendor)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zw:
        zw.writestr("tesseract/tesseract.exe", b"FAKEEXE")
        zw.writestr("tesseract/tessdata/eng.traineddata", b"ENGDATA")
    zip_path = tmp_path / "tesseract_no_configs.zip"
    zip_path.write_bytes(buf.getvalue())

    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(RuntimeError, match="configs"):
        refresh_binaries.install_tesseract({"_local_zip": zip_path}, work)


# ---------------------------------------------------------------------------
# HSB-02 -- ocr_to_tsv.py must not trust tesseract's exit code alone
# ---------------------------------------------------------------------------

class _FakeCompletedProcess:
    returncode = 0


def _patch_binaries(monkeypatch):
    monkeypatch.setattr(ocr_to_tsv, "resolve_pdftoppm", lambda: "FAKE_PDFTOPPM")
    monkeypatch.setattr(ocr_to_tsv, "resolve_tesseract", lambda: "FAKE_TESSERACT")


def test_ocr_pdf_raises_on_first_page_when_tesseract_writes_nothing(tmp_path, monkeypatch):
    """tesseract exits 0 but writes NOTHING (the actual failure mode when
    tessdata/configs/tsv is missing and even the .txt fallback can't be
    written) -- ocr_pdf() must raise on page 1, not silently continue."""
    _patch_binaries(monkeypatch)
    work_dir = tmp_path / "work"

    def fake_run(cmd, check=True, stderr=None):
        if cmd[0] == "FAKE_PDFTOPPM":
            # Simulate pdftoppm producing two rasterised pages.
            out_prefix = Path(cmd[-1])
            out_prefix.parent.mkdir(parents=True, exist_ok=True)
            (out_prefix.parent / f"{out_prefix.name}-1.png").write_bytes(b"PNG1")
            (out_prefix.parent / f"{out_prefix.name}-2.png").write_bytes(b"PNG2")
        elif cmd[0] == "FAKE_TESSERACT":
            pass  # writes nothing at all, exits 0
        else:
            raise AssertionError(f"unexpected binary invoked: {cmd[0]}")
        return _FakeCompletedProcess()

    monkeypatch.setattr(ocr_to_tsv.subprocess, "run", fake_run)

    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    with pytest.raises(RuntimeError, match="did not produce"):
        ocr_to_tsv.ocr_pdf(pdf_path, work_dir)

    # Only page 1 should have been attempted -- page 2's tsv must not exist,
    # proving the failure was raised before continuing to later pages.
    tsv_dir = work_dir / "tsv" / "statement"
    assert not (tsv_dir / "page-2.tsv").exists()


def test_ocr_pdf_raises_when_tesseract_writes_txt_instead_of_tsv(tmp_path, monkeypatch):
    """The real-world failure mode: tesseract exits 0 and writes a .txt file
    (the config-lookup fallback) instead of the requested .tsv. ocr_pdf()
    must still raise on page 1 rather than treating exit code 0 as success."""
    _patch_binaries(monkeypatch)
    work_dir = tmp_path / "work"

    def fake_run(cmd, check=True, stderr=None):
        if cmd[0] == "FAKE_PDFTOPPM":
            out_prefix = Path(cmd[-1])
            out_prefix.parent.mkdir(parents=True, exist_ok=True)
            (out_prefix.parent / f"{out_prefix.name}-1.png").write_bytes(b"PNG1")
        elif cmd[0] == "FAKE_TESSERACT":
            # cmd[2] is the output stem passed to tesseract.
            out_stem = Path(cmd[2])
            out_stem.with_suffix(".txt").write_text("some ocr text, no tsv\n")
        else:
            raise AssertionError(f"unexpected binary invoked: {cmd[0]}")
        return _FakeCompletedProcess()

    monkeypatch.setattr(ocr_to_tsv.subprocess, "run", fake_run)

    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    with pytest.raises(RuntimeError, match="did not produce"):
        ocr_to_tsv.ocr_pdf(pdf_path, work_dir)

    tsv_dir = work_dir / "tsv" / "statement"
    assert not (tsv_dir / "page-1.tsv").exists()
    # The .txt fallback file must exist (proving this is exactly the
    # silent-fallback scenario), while no .tsv was produced.
    assert (tsv_dir / "page-1.txt").exists()


def test_ocr_pdf_raises_when_tesseract_writes_empty_tsv(tmp_path, monkeypatch):
    """Belt-and-braces: an empty .tsv (0 bytes) must also be treated as
    failure, not just a missing file."""
    _patch_binaries(monkeypatch)
    work_dir = tmp_path / "work"

    def fake_run(cmd, check=True, stderr=None):
        if cmd[0] == "FAKE_PDFTOPPM":
            out_prefix = Path(cmd[-1])
            out_prefix.parent.mkdir(parents=True, exist_ok=True)
            (out_prefix.parent / f"{out_prefix.name}-1.png").write_bytes(b"PNG1")
        elif cmd[0] == "FAKE_TESSERACT":
            out_stem = Path(cmd[2])
            out_stem.with_suffix(".tsv").write_text("")  # empty file, exit 0
        else:
            raise AssertionError(f"unexpected binary invoked: {cmd[0]}")
        return _FakeCompletedProcess()

    monkeypatch.setattr(ocr_to_tsv.subprocess, "run", fake_run)

    pdf_path = tmp_path / "statement.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    with pytest.raises(RuntimeError, match="did not produce"):
        ocr_to_tsv.ocr_pdf(pdf_path, work_dir)
