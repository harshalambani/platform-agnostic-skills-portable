"""
ui/_native.py — resolves and registers the bundled native binaries
(Tesseract OCR, Poppler, qpdf) so subprocess calls inside the skills find
them without the user touching PATH.

Resolution layers:
    Frozen build:   pa_skills.exe lives at  staging/App/PASkills/pa_skills.exe
                    Tesseract:              .../PASkills/tesseract/tesseract.exe
                    Poppler:                .../PASkills/poppler/bin/pdftoppm.exe
                    qpdf:                   .../PASkills/qpdf/bin/qpdf.exe
                    (i.e., siblings of pa_skills.exe, not inside _internal/)

    Source mode:    vendor/tesseract/tesseract.exe
                    vendor/poppler/bin/pdftoppm.exe
                    vendor/qpdf/bin/qpdf.exe

Public surface:
    ensure_native_path() -> NativeStatus
        Prepends both folders to os.environ['PATH'], sets TESSDATA_PREFIX,
        configures pytesseract if importable. Idempotent — safe to call
        multiple times from different tabs.

    native_status()      -> NativeStatus
        Pure inspection; no side effects. Used by the Home tab to display
        a quick health pill.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class NativeStatus:
    tesseract_exe: Path | None
    tessdata_dir: Path | None
    poppler_bin:  Path | None      # the bin/ folder, not pdftoppm.exe itself
    pdftoppm_exe: Path | None
    qpdf_bin:     Path | None      # the bin/ folder containing qpdf.exe
    qpdf_exe:     Path | None
    mode: str                      # "frozen" or "source"

    @property
    def ok(self) -> bool:
        return all((self.tesseract_exe, self.tessdata_dir, self.pdftoppm_exe))

    def summary(self) -> str:
        parts = []
        if self.tesseract_exe and self.tesseract_exe.is_file():
            parts.append(f"tesseract={self.tesseract_exe.name}")
        else:
            parts.append("tesseract=MISSING")
        if self.pdftoppm_exe and self.pdftoppm_exe.is_file():
            parts.append(f"pdftoppm={self.pdftoppm_exe.name}")
        else:
            parts.append("pdftoppm=MISSING")
        if self.qpdf_exe and self.qpdf_exe.is_file():
            parts.append(f"qpdf={self.qpdf_exe.name}")
        else:
            parts.append("qpdf=MISSING")
        return f"native ({self.mode}): " + ", ".join(parts)


def _frozen_root() -> Path | None:
    """If running under PyInstaller --onedir, return the folder containing pa_skills.exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return None


def _candidate_roots() -> tuple[Path | None, Path | None, Path | None, str]:
    """
    Return (tesseract_root, poppler_root, qpdf_root, mode).
    tesseract_root contains tesseract.exe; poppler_root and qpdf_root contain bin/ subfolders.
    """
    frozen = _frozen_root()
    if frozen is not None:
        return (frozen / "tesseract", frozen / "poppler", frozen / "qpdf", "frozen")
    return (PROJECT_ROOT / "vendor" / "tesseract", PROJECT_ROOT / "vendor" / "poppler", PROJECT_ROOT / "vendor" / "qpdf", "source")


def native_status() -> NativeStatus:
    """Inspect-only resolver. Does not mutate os.environ."""
    tess_root, popp_root, qpdf_root, mode = _candidate_roots()
    tess_exe = (tess_root / "tesseract.exe") if tess_root else None
    if tess_exe is not None and not tess_exe.is_file():
        tess_exe = None
    tessdata = (tess_root / "tessdata") if tess_root else None
    if tessdata is not None and not tessdata.is_dir():
        tessdata = None
    pop_bin = (popp_root / "bin") if popp_root else None
    if pop_bin is not None and not pop_bin.is_dir():
        pop_bin = None
    pdftoppm = (pop_bin / "pdftoppm.exe") if pop_bin else None
    if pdftoppm is not None and not pdftoppm.is_file():
        pdftoppm = None
    qpdf_bin = (qpdf_root / "bin") if qpdf_root else None
    if qpdf_bin is not None and not qpdf_bin.is_dir():
        qpdf_bin = None
    qpdf_exe = (qpdf_bin / "qpdf.exe") if qpdf_bin else None
    if qpdf_exe is not None and not qpdf_exe.is_file():
        qpdf_exe = None
    return NativeStatus(
        tesseract_exe=tess_exe,
        tessdata_dir=tessdata,
        poppler_bin=pop_bin,
        pdftoppm_exe=pdftoppm,
        qpdf_bin=qpdf_bin,
        qpdf_exe=qpdf_exe,
        mode=mode,
    )


_REGISTERED = False


def ensure_native_path() -> NativeStatus:
    """
    Make Tesseract + Poppler discoverable to subprocess calls and to
    `pytesseract` (if installed). Idempotent.

    Returns the same NativeStatus dataclass as native_status().
    """
    global _REGISTERED
    status = native_status()

    if _REGISTERED:
        return status

    parts = []
    if status.tesseract_exe is not None:
        parts.append(str(status.tesseract_exe.parent))
    if status.poppler_bin is not None:
        parts.append(str(status.poppler_bin))
    if status.qpdf_bin is not None:
        parts.append(str(status.qpdf_bin))

    if parts:
        existing = os.environ.get("PATH", "")
        # Prepend; preserve original entries.
        os.environ["PATH"] = os.pathsep.join(parts + ([existing] if existing else []))

    if status.tessdata_dir is not None:
        # TESSDATA_PREFIX should point to the folder *containing* tessdata/, per Tesseract
        # convention. Some forks expect the folder itself — set both for safety.
        os.environ["TESSDATA_PREFIX"] = str(status.tessdata_dir.parent)

    # Configure pytesseract too, in case any skill code uses it.
    if status.tesseract_exe is not None:
        try:
            import pytesseract  # type: ignore[import-not-found]
            pytesseract.pytesseract.tesseract_cmd = str(status.tesseract_exe)
        except Exception:  # noqa: BLE001
            pass

    _REGISTERED = True
    return status


def shutil_which(name: str) -> str | None:
    """Convenience: shutil.which() but always after ensure_native_path()."""
    ensure_native_path()
    return shutil.which(name)
