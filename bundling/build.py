"""
bundling/build.py — PA Skills Portable build orchestrator.

Phase 1 scope: steps 1–4 of spec §10.2.
    1. Resolve version (from latest Git tag).
    2. Wipe staging/ and recreate the PortableApps.com folder skeleton.
    3. Create a clean Python venv in build_pyinstaller/venv/, pip-install
       requirements.txt (plus pyinstaller).
    4. Run PyInstaller against ui/webui.py via bundling/paskills.spec.
       Output lands in staging/App/PASkills/.

Phase 2+ steps (5–11 of §10.2) are stubbed with TODO markers but no-op.

Invocation (from repo root):
    python bundling\\build.py
    python bundling\\build.py --version 0.1.0
    python bundling\\build.py --allow-dirty
    python bundling\\build.py --skip-venv     # reuse existing build venv
    python bundling\\build.py --skip-pull     # don't re-pull agents/

Exit codes:
    0  success
    1  precondition failed (dirty tree, missing tool, etc.)
    2  subprocess (venv / pip / pyinstaller / agents-pull) failed
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STAGING       = PROJECT_ROOT / "staging"
DIST          = PROJECT_ROOT / "dist"
BUILD_VENV_DIR = PROJECT_ROOT / "build_pyinstaller"
BUILD_VENV    = BUILD_VENV_DIR / "venv"

SRC_AGENTS    = PROJECT_ROOT / "src" / "agents"
UI_DIR        = PROJECT_ROOT / "ui"
BUILDINFO_PY  = UI_DIR / "_buildinfo.py"
PASKILLS_SPEC = PROJECT_ROOT / "bundling" / "paskills.spec"
REQUIREMENTS  = PROJECT_ROOT / "requirements.txt"
SOURCES_TOML  = PROJECT_ROOT / "bundling" / "sources.toml"


# ---------------------------------------------------------------------------
# Pretty logging.
# ---------------------------------------------------------------------------

class _Log:
    """Tiny, dependency-free coloured logger."""
    BLUE   = "\033[94m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"
    DIM    = "\033[2m"
    RESET  = "\033[0m"

    def __init__(self, use_color: bool):
        self.use_color = use_color and sys.stdout.isatty()

    def _c(self, code: str, text: str) -> str:
        return f"{code}{text}{self.RESET}" if self.use_color else text

    def step(self, n: int | str, msg: str) -> None:
        print(self._c(self.BLUE, f"[step {n}] ") + msg, flush=True)

    def info(self, msg: str) -> None:
        print(self._c(self.DIM, "       ") + msg, flush=True)

    def ok(self, msg: str) -> None:
        print(self._c(self.GREEN, "  ok   ") + msg, flush=True)

    def warn(self, msg: str) -> None:
        print(self._c(self.YELLOW, " warn  ") + msg, flush=True)

    def err(self, msg: str) -> None:
        print(self._c(self.RED, " err   ") + msg, flush=True, file=sys.stderr)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _run(cmd: Sequence[str | Path], *, cwd: Path | None = None, env: dict | None = None,
         check: bool = True, log: _Log) -> subprocess.CompletedProcess:
    """Run a subprocess with consistent logging."""
    cmd_str = [str(c) for c in cmd]
    log.info("$ " + " ".join(cmd_str))
    res = subprocess.run(cmd_str, cwd=cwd, env=env, check=False)
    if check and res.returncode != 0:
        log.err(f"command failed (exit {res.returncode})")
        sys.exit(2)
    return res


def _rmtree(path: Path) -> None:
    """rmtree that tolerates missing paths and read-only files on Windows."""
    if not path.exists():
        return

    def _on_error(func, p, exc_info):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            raise

    shutil.rmtree(path, onerror=_on_error)


def _venv_python(venv: Path) -> Path:
    if platform.system() == "Windows":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _git_describe(log: _Log) -> tuple[str, str, bool]:
    """
    Returns (version_string, commit_sha, dirty).
    Falls back to ('0.0.0+local', 'unknown', True) if git is unavailable
    or the working tree is not a repo.
    """
    git = shutil.which("git")
    if not git:
        log.warn("git not on PATH; using fallback version 0.0.0+local.")
        return "0.0.0+local", "unknown", True

    try:
        sha = subprocess.check_output(
            [git, "rev-parse", "--short=7", "HEAD"], cwd=PROJECT_ROOT
        ).decode().strip()
    except subprocess.CalledProcessError:
        log.warn("no git history yet; using fallback version 0.0.0+local.")
        return "0.0.0+local", "unknown", True

    dirty = bool(
        subprocess.run(
            [git, "diff-index", "--quiet", "HEAD", "--"], cwd=PROJECT_ROOT
        ).returncode
    )

    try:
        tag = subprocess.check_output(
            [git, "describe", "--tags", "--abbrev=0", "--match", "v*"], cwd=PROJECT_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        version = tag.lstrip("v")
    except subprocess.CalledProcessError:
        version = f"0.0.0+{sha}"

    if dirty:
        version = f"{version}+dirty"

    return version, sha, dirty


# ---------------------------------------------------------------------------
# Steps.
# ---------------------------------------------------------------------------

def step1_resolve_version(args: argparse.Namespace, log: _Log) -> tuple[str, str, bool]:
    log.step(1, "Resolve version")

    if args.version:
        version = args.version
        sha = "manual"
        dirty = False
        log.info(f"version forced via --version: {version}")
    else:
        version, sha, dirty = _git_describe(log)
        log.info(f"git: tag-derived version={version}  sha={sha}  dirty={dirty}")

    if dirty and not args.allow_dirty:
        log.err("working tree is dirty. Commit or pass --allow-dirty.")
        sys.exit(1)

    # Write _buildinfo.py for the UI's About panel.
    BUILDINFO_PY.write_text(
        '"""Generated by bundling/build.py. Do not edit by hand."""\n'
        f'VERSION: str = {version!r}\n'
        f'COMMIT_SHA: str = {sha!r}\n'
        f'BUILD_DIRTY: bool = {dirty!r}\n'
        f'BUILD_TIMESTAMP: str = {datetime.utcnow().isoformat(timespec="seconds")+"Z"!r}\n',
        encoding="utf-8",
    )
    log.ok(f"wrote {BUILDINFO_PY.relative_to(PROJECT_ROOT)} ({version})")
    return version, sha, dirty


def step2_reset_staging(log: _Log) -> None:
    log.step(2, "Reset staging tree")
    _rmtree(STAGING)

    skeleton = [
        STAGING,
        STAGING / "App",
        STAGING / "App" / "AppInfo",
        STAGING / "App" / "AppInfo" / "Launcher",
        STAGING / "App" / "PASkills",
        STAGING / "App" / "DefaultData",
        STAGING / "App" / "DefaultData" / "settings",
        STAGING / "Data",
        STAGING / "Other",
        STAGING / "Other" / "Source",
        STAGING / "Other" / "Help",
    ]
    for d in skeleton:
        d.mkdir(parents=True, exist_ok=True)
    DIST.mkdir(parents=True, exist_ok=True)
    log.ok(f"staging/ recreated with {len(skeleton)} folders")


def step3_create_venv(args: argparse.Namespace, log: _Log) -> Path:
    log.step(3, "Build venv + pip install")
    py = _venv_python(BUILD_VENV)

    if args.skip_venv and py.exists():
        log.info(f"reusing existing venv: {BUILD_VENV}")
        return py

    _rmtree(BUILD_VENV)
    BUILD_VENV_DIR.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, "-m", "venv", BUILD_VENV], log=log)

    py = _venv_python(BUILD_VENV)
    if not py.exists():
        log.err(f"venv python not found at {py}")
        sys.exit(2)

    _run([py, "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"], log=log)
    _run([py, "-m", "pip", "install", "-r", REQUIREMENTS], log=log)
    _run([py, "-m", "pip", "install", "pyinstaller>=6.10"], log=log)
    log.ok(f"venv ready at {BUILD_VENV.relative_to(PROJECT_ROOT)}")
    return py


def step4_run_pyinstaller(py: Path, log: _Log) -> Path:
    log.step(4, "PyInstaller --onedir against paskills.spec")

    if not PASKILLS_SPEC.exists():
        log.err(f"spec not found: {PASKILLS_SPEC}")
        sys.exit(1)

    # PyInstaller writes to ./build and ./dist by default. Override both so
    # we don't conflict with the project's dist/ release folder.
    work = PROJECT_ROOT / "build_pyinstaller" / "work"
    pyi_dist = PROJECT_ROOT / "build_pyinstaller" / "dist"
    _rmtree(work)
    _rmtree(pyi_dist)

    _run(
        [py, "-m", "PyInstaller",
         "--noconfirm", "--clean",
         "--workpath", work,
         "--distpath", pyi_dist,
         PASKILLS_SPEC],
        cwd=PROJECT_ROOT,
        log=log,
    )

    src_dir = pyi_dist / "pa_skills"
    if not src_dir.is_dir():
        log.err(f"PyInstaller output missing: {src_dir}")
        sys.exit(2)

    # Copy into staging\App\PASkills\.
    dest = STAGING / "App" / "PASkills"
    if dest.exists():
        _rmtree(dest)
    shutil.copytree(src_dir, dest)
    log.ok(f"frozen build copied to {dest.relative_to(PROJECT_ROOT)}")
    return dest


# ---------------------------------------------------------------------------
# Phase 2+ stubs.
# ---------------------------------------------------------------------------

def step5_native_binaries(log: _Log) -> None:
    log.step(5, "Native binaries (Tesseract, Poppler) — Phase 2, skipped")


def step6_poppler(log: _Log) -> None:
    log.step(6, "Poppler — Phase 2, skipped (covered by step 5 stub)")


def step7_pull_agents(args: argparse.Namespace, log: _Log) -> None:
    log.step(7, "Pull agents/ from upstream")
    if args.skip_pull:
        log.info("--skip-pull set; agents/ not refreshed")
        return
    # In Phase 1 we assume src/agents/ has already been mirrored locally.
    # Real pull (from sibling folder or git URL per sources.toml) lands here.
    log.info(f"using already-mirrored src/agents/  ({sum(1 for _ in SRC_AGENTS.rglob('*.py'))} .py files)")


def step8_render_inis(log: _Log) -> None:
    log.step(8, "Render appinfo.ini + Launcher INI — Phase 2, skipped")


def step9_copy_defaults(log: _Log) -> None:
    log.step(9, "Copy DefaultData → staging — Phase 2, skipped")


def step10_launcher_gen(log: _Log) -> None:
    log.step(10, "Invoke PortableApps Launcher Generator — Phase 2, skipped")


def step11_zip(log: _Log) -> None:
    log.step(11, "Zip staging → dist — Phase 2, skipped")


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="build.py", description=__doc__.splitlines()[1] if __doc__ else "")
    p.add_argument("--version", help="Force a specific version string; otherwise derived from git tag.")
    p.add_argument("--allow-dirty", action="store_true", help="Allow a dirty working tree.")
    p.add_argument("--skip-venv", action="store_true", help="Reuse the existing build venv if present.")
    p.add_argument("--skip-pull", action="store_true", help="Don't refresh src/agents/ from upstream.")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colour output.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log = _Log(use_color=not args.no_color)

    log.info(f"project root: {PROJECT_ROOT}")
    log.info(f"python:       {sys.executable} ({sys.version.split()[0]})")
    log.info(f"platform:     {platform.system()} {platform.release()}")

    version, sha, dirty = step1_resolve_version(args, log)
    step2_reset_staging(log)
    step7_pull_agents(args, log)               # logical step 7, but cheap to run early
    py = step3_create_venv(args, log)
    step4_run_pyinstaller(py, log)

    # Phase 2+ stubs (noisy "skipped" markers so the build script's shape
    # matches the spec even before those phases are implemented).
    step5_native_binaries(log)
    step6_poppler(log)
    step8_render_inis(log)
    step9_copy_defaults(log)
    step10_launcher_gen(log)
    step11_zip(log)

    log.ok(f"Phase 1 build complete (version {version}, sha {sha})")
    log.info(f"Frozen output: {STAGING / 'App' / 'PASkills' / 'pa_skills.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
