"""
bundling/refresh_binaries.py — download + verify + extract Tesseract and Poppler
into vendor/, for the Phase 2 air-gapped build flow (spec §10.4).

Usage (from project root):
    python bundling\\refresh_binaries.py --target tesseract
    python bundling\\refresh_binaries.py --target poppler
    python bundling\\refresh_binaries.py --target all

Bootstrap mode:
    If a SHA-256 entry in binaries.toml is still the placeholder
    "REPLACE_WITH_PINNED_SHA256_AT_PHASE_2", the script downloads the file
    once, computes the SHA, prints it for you to paste into binaries.toml,
    and exits without populating vendor/. Re-run after editing the TOML.

What lands where:
    Tesseract zip → extracted, then *.exe / *.dll / *.tessdata.eng.traineddata
                    are copied into vendor/tesseract/  (flat layout).
    Poppler   zip → its Library/bin/* is copied into vendor/poppler/bin/.

Why flat: the build.py step 5/6 then does a single shutil.copytree() per
target into the frozen output, with no further path-mangling.
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BINARIES_TOML = PROJECT_ROOT / "bundling" / "binaries.toml"
VENDOR = PROJECT_ROOT / "vendor"

PLACEHOLDER_SHA = "REPLACE_WITH_PINNED_SHA256_AT_PHASE_2"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _eprint(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _download(url: str, dest: Path) -> None:
    """Stream a URL into a file with a basic progress indicator."""
    _eprint(f"  downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "PA-Skills-refresh-binaries/1.0"})
    with urllib.request.urlopen(req) as resp:  # nosec — pinned URLs in binaries.toml
        total = int(resp.headers.get("Content-Length", 0))
        with dest.open("wb") as out:
            chunk = 1 << 18  # 256 KiB
            seen = 0
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                out.write(buf)
                seen += len(buf)
                if total:
                    pct = 100 * seen // total
                    print(f"\r  {pct:>3}%  ({seen / 1e6:.1f} / {total / 1e6:.1f} MB)",
                          end="", flush=True)
            if total:
                print()  # newline after progress


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_globs(src_root: Path, patterns: Iterable[str], dest: Path) -> int:
    """Copy files matching any of the patterns (relative to src_root) into dest, preserving subpath. Returns count copied."""
    count = 0
    for pattern in patterns:
        for src in src_root.rglob(pattern):
            if not src.is_file():
                continue
            rel = src.relative_to(src_root)
            tgt = dest / rel.name  # flat layout under dest
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tgt)
            count += 1
    return count


# ---------------------------------------------------------------------------
# Per-target installers
# ---------------------------------------------------------------------------

def install_tesseract(meta: dict, tmp: Path) -> None:
    """Extract the Tesseract portable zip, copy executables + DLLs + tessdata into vendor/tesseract/."""
    dest = VENDOR / "tesseract"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "tessdata").mkdir(exist_ok=True)

    extract_root = tmp / "tess_unzipped"
    extract_root.mkdir()
    _eprint(f"  unzipping into {extract_root}")
    with zipfile.ZipFile(meta["_local_zip"]) as zf:
        zf.extractall(extract_root)

    # The UB-Mannheim layout nests everything inside a single top-level folder.
    # Find the first folder containing tesseract.exe and use it as the root.
    candidates = list(extract_root.rglob("tesseract.exe"))
    if not candidates:
        raise RuntimeError("tesseract.exe not found in extracted archive")
    src_root = candidates[0].parent
    _eprint(f"  using source root: {src_root.relative_to(extract_root)}")

    # Copy all .exe and .dll and any tessdata/*.traineddata
    n_exe = _copy_globs(src_root, ["*.exe"], dest)
    n_dll = _copy_globs(src_root, ["*.dll"], dest)
    # Tessdata: keep only eng.traineddata per spec §6.
    eng = next(src_root.rglob("eng.traineddata"), None)
    if eng is None:
        raise RuntimeError("eng.traineddata not found in archive — wrong build?")
    shutil.copy2(eng, dest / "tessdata" / "eng.traineddata")

    _eprint(f"  tesseract: copied {n_exe} exe, {n_dll} dll, 1 traineddata")
    _eprint(f"  final tree: {sum(1 for _ in dest.rglob('*') if _.is_file())} files under vendor/tesseract/")


def install_poppler(meta: dict, tmp: Path) -> None:
    """Extract the Poppler-windows zip, copy bin/* into vendor/poppler/bin/."""
    dest = VENDOR / "poppler"
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "bin").mkdir(parents=True, exist_ok=True)

    extract_root = tmp / "poppler_unzipped"
    extract_root.mkdir()
    _eprint(f"  unzipping into {extract_root}")
    with zipfile.ZipFile(meta["_local_zip"]) as zf:
        zf.extractall(extract_root)

    # poppler-windows ships Library/bin (older releases) or just bin (newer).
    bin_dir = next(extract_root.rglob("pdftoppm.exe"), None)
    if bin_dir is None:
        raise RuntimeError("pdftoppm.exe not found in extracted archive")
    bin_dir = bin_dir.parent
    _eprint(f"  using bin/ source: {bin_dir.relative_to(extract_root)}")

    # Copy the entire bin/ folder verbatim into vendor/poppler/bin/.
    for f in bin_dir.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / "bin" / f.name)

    _eprint(f"  poppler: {sum(1 for _ in (dest / 'bin').iterdir())} files under vendor/poppler/bin/")


def install_qpdf(meta: dict, tmp: Path) -> None:
    """Extract the qpdf-msvc64 zip, copy bin/* into vendor/qpdf/bin/."""
    dest = VENDOR / "qpdf"
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "bin").mkdir(parents=True, exist_ok=True)

    extract_root = tmp / "qpdf_unzipped"
    extract_root.mkdir()
    _eprint(f"  unzipping into {extract_root}")
    with zipfile.ZipFile(meta["_local_zip"]) as zf:
        zf.extractall(extract_root)

    # qpdf-msvc64 ships qpdf-<version>/bin/qpdf.exe (nested one level).
    qpdf_exe = next(extract_root.rglob("qpdf.exe"), None)
    if qpdf_exe is None:
        raise RuntimeError("qpdf.exe not found in extracted archive")
    bin_dir = qpdf_exe.parent
    _eprint(f"  using bin/ source: {bin_dir.relative_to(extract_root)}")

    n = 0
    for f in bin_dir.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / "bin" / f.name)
            n += 1
    _eprint(f"  qpdf: {n} files under vendor/qpdf/bin/")


INSTALLERS = {
    "tesseract": install_tesseract,
    "poppler":   install_poppler,
    "qpdf":      install_qpdf,
}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _load_binaries() -> dict:
    if not BINARIES_TOML.is_file():
        raise FileNotFoundError(f"binaries.toml not found at {BINARIES_TOML}")
    return tomllib.loads(BINARIES_TOML.read_text(encoding="utf-8"))


def refresh_one(name: str, meta: dict) -> bool:
    """Process a single target. Returns True if vendor/ was populated, False if we only printed a bootstrap SHA."""
    print(f"\n[{name}]")
    print(f"  version: {meta.get('version')}")
    print(f"  url:     {meta.get('url')}")
    print(f"  pinned:  {meta.get('sha256')}")

    url = meta["url"]
    expected_sha = meta["sha256"]

    with tempfile.TemporaryDirectory(prefix=f"pa-refresh-{name}-") as raw_tmp:
        tmp = Path(raw_tmp)
        zip_path = tmp / Path(url).name
        _download(url, zip_path)
        actual_sha = _sha256(zip_path)
        print(f"  actual:  {actual_sha}")

        if expected_sha == PLACEHOLDER_SHA:
            print(f"\n  ⚠  binaries.toml has a placeholder SHA for '{name}'.")
            print(f"     Copy this line into binaries.toml under [{name}]:\n")
            print(f"         sha256 = \"{actual_sha}\"\n")
            print(f"     Then re-run: python bundling\\refresh_binaries.py --target {name}")
            return False

        if actual_sha.lower() != expected_sha.lower():
            raise RuntimeError(
                f"SHA-256 mismatch for {name}!\n"
                f"  expected: {expected_sha}\n"
                f"  actual:   {actual_sha}\n"
                "Refusing to extract. Verify upstream URL hasn't changed, or update binaries.toml."
            )

        meta["_local_zip"] = zip_path  # passed to the installer
        installer = INSTALLERS[name]
        installer(meta, tmp)
        print(f"  ok       vendor/{name}/ populated")
        return True


def install_from_local(name: str, src_root: Path) -> bool:
    """
    Skip the download/verify path and copy native binaries from a local install.
    Used when the upstream URL is broken or the user has Tesseract/Poppler
    already installed via an .exe installer or package manager.

    For Tesseract: src_root should be the folder containing tesseract.exe
        (e.g., C:\\Program Files\\Tesseract-OCR after the UB-Mannheim installer).
    For Poppler: src_root should be either a Poppler root containing bin/, or
        the bin/ folder itself.
    """
    print(f"\n[{name}] (local copy)")
    print(f"  source: {src_root}")

    if not src_root.is_dir():
        raise FileNotFoundError(f"{src_root} is not a directory")

    if name == "tesseract":
        # Locate tesseract.exe under src_root (recursively, just in case).
        tess_exe = next(src_root.rglob("tesseract.exe"), None)
        if tess_exe is None:
            raise RuntimeError(f"tesseract.exe not found under {src_root}")
        real_root = tess_exe.parent
        print(f"  found tesseract.exe at {real_root}")

        dest = VENDOR / "tesseract"
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "tessdata").mkdir(exist_ok=True)

        n_exe = _copy_globs(real_root, ["*.exe"], dest)
        n_dll = _copy_globs(real_root, ["*.dll"], dest)

        # Find eng.traineddata anywhere under src_root (UB-Mannheim layout: tessdata/ sibling).
        eng = next(src_root.rglob("eng.traineddata"), None)
        if eng is None:
            raise RuntimeError(f"eng.traineddata not found under {src_root}")
        shutil.copy2(eng, dest / "tessdata" / "eng.traineddata")
        print(f"  copied {n_exe} exe, {n_dll} dll, 1 traineddata")
        return True

    if name == "poppler":
        # Either src_root contains bin/, or src_root IS the bin folder.
        if (src_root / "bin").is_dir():
            bin_dir = src_root / "bin"
        elif (src_root.name == "bin") and (src_root / "pdftoppm.exe").is_file():
            bin_dir = src_root
        else:
            cand = next(src_root.rglob("pdftoppm.exe"), None)
            if cand is None:
                raise RuntimeError(f"pdftoppm.exe not found under {src_root}")
            bin_dir = cand.parent
        print(f"  using bin/ source: {bin_dir}")

        dest = VENDOR / "poppler"
        if dest.exists():
            shutil.rmtree(dest)
        (dest / "bin").mkdir(parents=True, exist_ok=True)
        n = 0
        for f in bin_dir.iterdir():
            if f.is_file():
                shutil.copy2(f, dest / "bin" / f.name)
                n += 1
        print(f"  copied {n} files into vendor/poppler/bin/")
        return True

    if name == "qpdf":
        # Either src_root contains bin/, or src_root IS the bin folder.
        if (src_root / "bin").is_dir():
            bin_dir = src_root / "bin"
        elif (src_root.name == "bin") and (src_root / "qpdf.exe").is_file():
            bin_dir = src_root
        else:
            cand = next(src_root.rglob("qpdf.exe"), None)
            if cand is None:
                raise RuntimeError(f"qpdf.exe not found under {src_root}")
            bin_dir = cand.parent
        print(f"  using bin/ source: {bin_dir}")

        dest = VENDOR / "qpdf"
        if dest.exists():
            shutil.rmtree(dest)
        (dest / "bin").mkdir(parents=True, exist_ok=True)
        n = 0
        for f in bin_dir.iterdir():
            if f.is_file():
                shutil.copy2(f, dest / "bin" / f.name)
                n += 1
        print(f"  copied {n} files into vendor/qpdf/bin/")
        return True

    raise ValueError(f"install_from_local doesn't know how to handle '{name}'")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="refresh_binaries",
        description="Download + verify + extract native binaries into vendor/.",
    )
    parser.add_argument(
        "--target",
        choices=("tesseract", "poppler", "qpdf", "all"),
        default="all",
        help="Which native binary to refresh.",
    )
    parser.add_argument(
        "--from-tesseract",
        metavar="PATH",
        help="Skip Tesseract download; copy from a local install. "
             "Typical: 'C:\\Program Files\\Tesseract-OCR' after running the UB-Mannheim installer.",
    )
    parser.add_argument(
        "--from-poppler",
        metavar="PATH",
        help="Skip Poppler download; copy from a local Poppler tree. "
             "Path should contain a bin/ folder with pdftoppm.exe.",
    )
    parser.add_argument(
        "--from-qpdf",
        metavar="PATH",
        help="Skip qpdf download; copy from a local qpdf install. "
             "Path should contain a bin/ folder with qpdf.exe.",
    )
    args = parser.parse_args(argv)

    config = _load_binaries()

    targets = ("tesseract", "poppler", "qpdf") if args.target == "all" else (args.target,)
    local_from = {
        "tesseract": args.from_tesseract,
        "poppler":   args.from_poppler,
        "qpdf":      args.from_qpdf,
    }

    all_ok = True
    for name in targets:
        if name not in config:
            _eprint(f"  ⚠  {name} not in binaries.toml — skipping")
            continue
        try:
            src_path = local_from.get(name)
            if src_path:
                ok = install_from_local(name, Path(src_path))
            else:
                ok = refresh_one(name, dict(config[name]))
            all_ok = all_ok and ok
        except Exception as e:  # noqa: BLE001
            _eprint(f"  ✗  {name} failed: {e}")
            return 2

    if not all_ok:
        print("\nOne or more targets need a SHA paste; see messages above. Exiting non-zero to flag.")
        return 1

    print("\nDone. vendor/ is ready for the build pipeline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
