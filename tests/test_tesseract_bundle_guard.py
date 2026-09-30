"""tests/test_tesseract_bundle_guard.py -- HSB-01 guards on what SHIPS.

#262 fixed bundling/refresh_binaries.py and tested the script, but vendor/ was
never regenerated, so v3.10.0 and v3.11.0 shipped without tessdata/configs/tsv.
These tests look at the committed vendor tree and at the build-time check that
runs on the staged output.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bundling import build as pask_build  # noqa: E402

VENDOR_TESS = ROOT / "vendor" / "tesseract"


def _is_real_exe(p: Path) -> bool:
    try:
        return p.read_bytes()[:2] == b"MZ"
    except OSError:
        return False


def _stage(tmp_path: Path, with_configs: bool = True, with_tsv: bool = True) -> Path:
    dest = tmp_path / "tesseract"
    (dest / "tessdata").mkdir(parents=True)
    (dest / "tesseract.exe").write_bytes(b"MZ")
    (dest / "tessdata" / "eng.traineddata").write_bytes(b"x")
    if with_configs:
        (dest / "tessdata" / "configs").mkdir()
        (dest / "tessdata" / "configs" / "txt").write_text("tessedit_create_txt 1\n")
        if with_tsv:
            (dest / "tessdata" / "configs" / "tsv").write_text("tessedit_create_tsv 1\n")
    return dest


# -- (a) the committed vendor tree ------------------------------------------

def test_committed_vendor_tree_has_tessdata_configs_tsv():
    tsv = VENDOR_TESS / "tessdata" / "configs" / "tsv"
    assert tsv.is_file(), "vendor/tesseract/tessdata/configs/tsv is missing (HSB-01)"
    assert tsv.read_text().strip() == "tessedit_create_tsv 1"


def test_configs_are_plain_text_not_lfs_pointers():
    for f in (VENDOR_TESS / "tessdata" / "configs").iterdir():
        head = f.read_bytes()[:40]
        assert not head.startswith(b"version https://git-lfs"), f"{f.name} is an LFS pointer"
        assert b"\r" not in f.read_bytes(), f"{f.name} has CR bytes; configs must be LF"


# -- (b) the build-time check -----------------------------------------------

def test_verify_tesseract_bundle_accepts_complete_tree(tmp_path):
    assert pask_build.verify_tesseract_bundle(_stage(tmp_path)) == []


def test_verify_tesseract_bundle_flags_missing_configs_dir(tmp_path):
    problems = pask_build.verify_tesseract_bundle(_stage(tmp_path, with_configs=False))
    assert problems and any("configs" in p and "tsv" in p for p in problems)


def test_verify_tesseract_bundle_flags_configs_without_tsv(tmp_path):
    problems = pask_build.verify_tesseract_bundle(_stage(tmp_path, with_tsv=False))
    assert problems


def test_verify_tesseract_bundle_flags_empty_tsv(tmp_path):
    dest = _stage(tmp_path)
    (dest / "tessdata" / "configs" / "tsv").write_text("")
    assert pask_build.verify_tesseract_bundle(dest)


def test_step5_exits_nonzero_when_configs_removed(tmp_path, monkeypatch):
    """The build must FAIL (SystemExit), not warn, when the vendor tree has no
    configs/ -- this is the exact v3.10.0 / v3.11.0 shape."""
    fake_root = tmp_path / "proj"
    src = fake_root / "vendor" / "tesseract"
    (src / "tessdata").mkdir(parents=True)
    (src / "tesseract.exe").write_bytes(b"MZ")
    (src / "tessdata" / "eng.traineddata").write_bytes(b"x")
    monkeypatch.setattr(pask_build, "PROJECT_ROOT", fake_root)
    monkeypatch.setattr(pask_build, "STAGING", tmp_path / "staging")

    class _L:
        def __getattr__(self, name):
            return lambda *a, **k: None

    with pytest.raises(SystemExit) as ei:
        pask_build.step5_native_binaries(_L())
    assert ei.value.code not in (0, None)


def test_step5_passes_on_the_real_committed_vendor_tree(tmp_path, monkeypatch):
    """Guard the case that must work: the real vendor tree stages cleanly."""
    if not (VENDOR_TESS / "tesseract.exe").is_file():
        pytest.skip("vendor/tesseract not present")
    monkeypatch.setattr(pask_build, "STAGING", tmp_path / "staging")

    class _L:
        def __getattr__(self, name):
            return lambda *a, **k: None

    pask_build.step5_native_binaries(_L())
    staged = tmp_path / "staging" / "App" / "PASkills" / "tesseract"
    assert (staged / "tessdata" / "configs" / "tsv").is_file()


# -- the bundled binary itself ------------------------------------------------

@pytest.mark.skipif(
    sys.platform != "win32" or not _is_real_exe(VENDOR_TESS / "tesseract.exe"),
    reason="needs the real (non-LFS-pointer) Windows tesseract.exe",
)
def test_bundled_tesseract_with_tsv_config_writes_tsv_and_missing_config_does_not(tmp_path):
    from PIL import Image, ImageDraw

    img = tmp_path / "p.png"
    im = Image.new("RGB", (300, 80), "white")
    ImageDraw.Draw(im).text((10, 20), "HELLO 123", fill="black")
    im.save(img)

    exe = VENDOR_TESS / "tesseract.exe"
    good = tmp_path / "good"
    subprocess.run([str(exe), str(img), str(good), "tsv"], check=True,
                   stderr=subprocess.DEVNULL, cwd=str(VENDOR_TESS))
    assert good.with_suffix(".tsv").is_file() and good.with_suffix(".tsv").stat().st_size > 0

    # NEGATIVE: same binary, tessdata WITHOUT configs/ -> no .tsv (exit 0, .txt
    # fallback). This is the silent failure; the HSB-02 guard exists to catch it.
    bare = tmp_path / "bare_tessdata"
    bare.mkdir()
    shutil.copy2(VENDOR_TESS / "tessdata" / "eng.traineddata", bare / "eng.traineddata")
    bad = tmp_path / "bad"
    subprocess.run([str(exe), "--tessdata-dir", str(bare), str(img), str(bad), "tsv"],
                   check=False, stderr=subprocess.DEVNULL, cwd=str(VENDOR_TESS))
    assert not bad.with_suffix(".tsv").exists()
