"""
agents/_native_resolve.py — resolves vendored native binaries (poppler's
pdftotext, qpdf) to ABSOLUTE paths for standalone skill scripts.

Why this exists (see the 26AS/Xpdf incident): scripts under
src/agents/skill_*/scripts/ run as standalone subprocesses
(``subprocess.run([sys.executable, script, ...])``), including inside a
frozen PyInstaller build. They cannot import ``ui._native`` (the UI package
isn't on their sys.path, and mustn't be a dependency of the headless
extractor layer). Historically they shelled out to a bare binary name
(``pdftotext``, ``qpdf``) and trusted PATH — on a machine where PATH
resolved ``pdftotext`` to Xpdf instead of Poppler, every regex in the 26AS
parser silently missed and produced a "0 deductors" result with no error.

This module:
  * mirrors ui/_native.py's ``_candidate_roots()`` resolution order (frozen
    root siblings, then vendor/<tool>/bin/ in source mode) — see
    tests/test_native_resolve.py::test_candidate_roots_mirrors_ui_native for
    the drift test that keeps the two in lockstep;
  * resolves an ABSOLUTE path to the vendored binary, only falling back to
    ``shutil.which()`` if the vendored copy is missing;
  * (pdftotext only) verifies the resolved binary is actually Poppler, not
    Xpdf or any other tool that also happens to be named "pdftotext" —
    both print a version banner to STDERR that is the only reliable way to
    tell them apart. See ``verify_pdftotext_is_poppler``.

Public surface:
    resolve_pdftotext(verify: bool = True) -> str
    resolve_pdftoppm() -> str
    resolve_qpdf() -> str
    resolve_tesseract() -> str
    verify_pdftotext_is_poppler(path, *, probe=None) -> None   (raises on mismatch)
    WrongPdftextFlavourError
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# scripts/ -> skill_X -> agents -> src : this file lives at src/agents/_native_resolve.py
# so PROJECT_ROOT is two parents up (agents/ -> src/ -> project root).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_EXE_SUFFIX = ".exe" if os.name == "nt" else ""


class WrongPdftextFlavourError(RuntimeError):
    """Raised when the resolved `pdftotext` binary is not Poppler."""


def _frozen_root() -> Path | None:
    """If running under PyInstaller --onedir, the folder containing the exe."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return None


def _candidate_roots() -> tuple[Path, Path, str]:
    """
    Return (poppler_root, qpdf_root, mode) — mirrors
    ui/_native.py::_candidate_roots() exactly (poppler_root and qpdf_root
    each contain a bin/ subfolder). See _tesseract_root() below for Tesseract
    (added once skill_hsbc/scripts/ocr_to_tsv.py was found to shell out to a
    bare "pdftoppm"/"tesseract" too — same bug class as pdftotext/qpdf).
    """
    frozen = _frozen_root()
    if frozen is not None:
        return (frozen / "poppler", frozen / "qpdf", "frozen")
    return (PROJECT_ROOT / "vendor" / "poppler", PROJECT_ROOT / "vendor" / "qpdf", "source")


def _tesseract_root() -> Path:
    """Root folder containing tesseract.exe directly (no bin/ subfolder) —
    mirrors ui/_native.py::_candidate_roots()'s tesseract_root."""
    frozen = _frozen_root()
    if frozen is not None:
        return frozen / "tesseract"
    return PROJECT_ROOT / "vendor" / "tesseract"


# Well-known system install paths for Tesseract — same fallback list as
# ui/_native.py::_SYSTEM_TESS_PATHS, checked when vendor/ is empty (dev
# machines without the vendored copy).
_SYSTEM_TESS_PATHS = [
    Path(r"C:\Program Files\Tesseract-OCR"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR"),
]


def _vendored_exe(root: Path, exe_name: str) -> str | None:
    candidate = root / "bin" / f"{exe_name}{_EXE_SUFFIX}"
    return str(candidate) if candidate.is_file() else None


def _resolve_binary(exe_name: str, root: Path) -> str:
    """Vendored copy first; shutil.which() only if the vendored copy is absent."""
    vendored = _vendored_exe(root, exe_name)
    if vendored is not None:
        return vendored
    found = shutil.which(exe_name)
    if found is not None:
        return found
    raise FileNotFoundError(
        f"{exe_name} not found: no vendored copy at {root / 'bin'} "
        f"and none on PATH."
    )


def resolve_qpdf() -> str:
    """Absolute path to qpdf — vendored copy first, PATH fallback."""
    _, qpdf_root, _ = _candidate_roots()
    return _resolve_binary("qpdf", qpdf_root)


def resolve_pdftoppm() -> str:
    """Absolute path to pdftoppm (poppler) — vendored copy first, PATH fallback."""
    poppler_root, _, _ = _candidate_roots()
    return _resolve_binary("pdftoppm", poppler_root)


def resolve_tesseract() -> str:
    """
    Absolute path to tesseract — vendored copy first (tesseract.exe directly
    under vendor/tesseract/, no bin/ subfolder), then well-known system
    install paths, then PATH.
    """
    root = _tesseract_root()
    candidate = root / f"tesseract{_EXE_SUFFIX}"
    if candidate.is_file():
        return str(candidate)
    for sys_path in _SYSTEM_TESS_PATHS:
        candidate = sys_path / f"tesseract{_EXE_SUFFIX}"
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("tesseract")
    if found is not None:
        return found
    raise FileNotFoundError(
        f"tesseract not found: no vendored copy at {root}, no system install "
        f"under {[str(p) for p in _SYSTEM_TESS_PATHS]}, and none on PATH."
    )


def _resolve_pdftotext_path() -> str:
    poppler_root, _, _ = _candidate_roots()
    return _resolve_binary("pdftotext", poppler_root)


# ---------------------------------------------------------------------------
# Tier 3 — identity gate: presence + exit 0 is not enough. Xpdf's pdftotext
# is a fully functional binary that runs successfully and emits text; only
# the version banner (stderr, both tools) distinguishes it from Poppler's.
# ---------------------------------------------------------------------------

def _probe_pdftotext_banner(path: str) -> str:
    """Run `<path> -v` and return stdout+stderr. Both Poppler and Xpdf print
    their version banner to stderr; some builds may also echo to stdout, so
    concatenate both rather than assume one stream."""
    result = subprocess.run(
        [path, "-v"], capture_output=True, text=True, timeout=15,
    )
    return f"{result.stdout or ''}\n{result.stderr or ''}"


# Cache keyed by the resolved absolute path string, so verifying two
# different resolved binaries in the same process (e.g. a test that swaps
# PATH mid-run) never shares a cache entry — see
# tests/test_native_resolve.py::test_identity_cache_keyed_per_path.
_IDENTITY_CACHE: dict[str, tuple[bool, str]] = {}


def verify_pdftotext_is_poppler(path: str, *, probe=None) -> None:
    """
    Raise WrongPdftextFlavourError unless `path` is Poppler's pdftotext.

    `probe` is an injection point for tests: a callable(path) -> banner_text,
    so this is fully testable without either binary installed. Result is
    cached per resolved path so a multi-file batch run only probes once per
    distinct binary.
    """
    cached = _IDENTITY_CACHE.get(path)
    if cached is not None:
        ok, detail = cached
        if ok:
            return
        raise WrongPdftextFlavourError(detail)

    prober = probe or _probe_pdftotext_banner
    try:
        banner = prober(path)
    except FileNotFoundError as e:
        detail = (
            f"pdftotext was resolved to '{path}' but that file could not be "
            f"executed (not found / not runnable): {e}"
        )
        _IDENTITY_CACHE[path] = (False, detail)
        raise WrongPdftextFlavourError(detail) from e
    except Exception as e:  # noqa: BLE001 — surface any probe failure loudly
        detail = f"could not determine the version of pdftotext at '{path}': {e}"
        _IDENTITY_CACHE[path] = (False, detail)
        raise WrongPdftextFlavourError(detail) from e

    low = banner.lower()
    if "poppler" in low:
        _IDENTITY_CACHE[path] = (True, "")
        return

    if "xpdf" in low or "glyph" in low or "cog" in low:
        flavour = "Xpdf (Glyph & Cog)"
    else:
        flavour = "an unrecognised pdftotext build"

    detail = (
        f"Wrong pdftotext found at '{path}': this is {flavour}, not Poppler. "
        f"Version banner: {banner.strip()!r}. "
        "Poppler's pdftotext -layout is required — a different pdftotext "
        "earlier on PATH (e.g. Xpdf) parses PDFs with different column "
        "spacing and silently produces wrong/empty extraction results. "
        "Fix PATH so the vendored Poppler build "
        f"(under {PROJECT_ROOT / 'vendor' / 'poppler' / 'bin'}) resolves first, "
        "or remove/rename the conflicting binary."
    )
    _IDENTITY_CACHE[path] = (False, detail)
    raise WrongPdftextFlavourError(detail)


def resolve_pdftotext(verify: bool = True) -> str:
    """
    Absolute path to pdftotext — vendored Poppler copy first, PATH fallback.

    When verify=True (the default; scripts should never disable this in
    production code — it exists only so tests can resolve without probing),
    raises WrongPdftextFlavourError loudly if the resolved binary is not
    Poppler, naming what was found and where it resolved from. Never falls
    back silently to a different binary.
    """
    path = _resolve_pdftotext_path()
    if verify:
        verify_pdftotext_is_poppler(path)
    return path
