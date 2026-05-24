"""
bundling/build.py — PA Skills Portable build orchestrator.

Phase 1 scope: steps 1–4 of spec §10.2.
    1. Resolve version (from latest Git tag).
    2. Wipe staging/ and recreate the PortableApps.com folder skeleton.
    3. Create a clean Python venv in build_pyinstaller/venv/, pip-install
       requirements.txt (plus pyinstaller).
    4. Run PyInstaller against ui/webui.py via bundling/paskills.spec.
       Output lands in staging/App/PASkills/.

Phase 2a added steps 5–7 (native binary vendoring + agent pull).
    Step 7 reads bundling/sources.toml to decide where to pull agents/
    from: "local" (sibling folder) or "git" (clone from upstream URL).
    Git clones are cached in build_pyinstaller/.agents_cache/.
    Use --skip-pull to skip this step entirely.

Phase 2b adds steps 8–11:
    8.  Render appinfo.ini + Launcher INI from bundling/templates/, and
        ensure staging/App/AppInfo/ has icon files (placeholder if real
        artwork is absent — the Launcher Generator aborts otherwise).
    9.  Copy bundling/templates/DefaultData → staging/App/DefaultData.
    10. Invoke the PortableApps.com Launcher Generator (Windows-only,
        warn-and-skip if not installed).
    11. Build a deterministic dist/PASkillsPortable_<version>.zip.

Invocation (from repo root):
    python bundling\\build.py
    python bundling\\build.py --version 0.1.0
    python bundling\\build.py --allow-dirty
    python bundling\\build.py --skip-venv     # reuse existing build venv
    python bundling\\build.py --skip-pull     # don't re-pull agents/
    python bundling\\build.py --skip-launcher # skip step10 + step11
    python bundling\\build.py --launcher-gen <path-to-generator.exe>

Exit codes:
    0  success
    1  precondition failed (dirty tree, missing tool, etc.)
    2  subprocess (venv / pip / pyinstaller / agents-pull) failed
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import platform
import re
import shutil
import string
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

PROJECT_ROOT = Path(__file__).resolve().parent.parent

STAGING       = PROJECT_ROOT / "staging"
DIST          = PROJECT_ROOT / "dist"
BUILD_VENV_DIR = PROJECT_ROOT / "build_pyinstaller"
BUILD_VENV    = BUILD_VENV_DIR / "venv"

SRC_AGENTS    = PROJECT_ROOT / "src" / "agents"
AGENTS_CACHE  = BUILD_VENV_DIR / ".agents_cache"
UI_DIR        = PROJECT_ROOT / "ui"
BUILDINFO_PY  = UI_DIR / "_buildinfo.py"
PASKILLS_SPEC = PROJECT_ROOT / "bundling" / "paskills.spec"
REQUIREMENTS  = PROJECT_ROOT / "requirements.txt"
SOURCES_TOML  = PROJECT_ROOT / "bundling" / "sources.toml"

TEMPLATES_DIR    = PROJECT_ROOT / "bundling" / "templates"
APPINFO_TMPL     = TEMPLATES_DIR / "appinfo.ini.tmpl"
LAUNCHER_TMPL    = TEMPLATES_DIR / "PASkillsPortable.ini.tmpl"
DEFAULTDATA_TMPL = TEMPLATES_DIR / "DefaultData"

LAUNCHER_GEN_HINTS = (
    Path(r"C:\PortableApps\PortableApps.com"),
    Path(r"C:\PortableApps\PortableApps.comLauncher"),
    Path(r"C:\PortableApps"),
)
LAUNCHER_GEN_EXE = "PortableApps.comLauncherGenerator.exe"


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
    cmd_str = [str(c) for c in cmd]
    log.info("$ " + " ".join(cmd_str))
    res = subprocess.run(cmd_str, cwd=cwd, env=env, check=False)
    if check and res.returncode != 0:
        log.err(f"command failed (exit {res.returncode})")
        sys.exit(2)
    return res


def _rmtree(path: Path) -> None:
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
            [git, "diff-index", "--quiet", "HEAD", "--", ":(exclude)ui/_buildinfo.py"], cwd=PROJECT_ROOT
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

    BUILDINFO_PY.write_text(
        '"""Generated by bundling/build.py. Do not edit by hand."""\n'
        f'VERSION: str = {version!r}\n'
        f'COMMIT_SHA: str = {sha!r}\n'
        f'BUILD_DIRTY: bool = {dirty!r}\n'
        f'BUILD_TIMESTAMP: str = {datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")!r}\n',
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

    dest = STAGING / "App" / "PASkills"
    if dest.exists():
        _rmtree(dest)
    shutil.copytree(src_dir, dest)
    log.ok(f"frozen build copied to {dest.relative_to(PROJECT_ROOT)}")
    return dest


# ---------------------------------------------------------------------------
# Phase 2a — native binaries + agent pull.
# ---------------------------------------------------------------------------

def _copy_vendor_subtree(src: Path, dest: Path, label: str, log: "_Log") -> int:
    if not src.is_dir():
        log.warn(f"vendor source missing: {src} - {label} not bundled")
        log.warn("  run: python bundling\\refresh_binaries.py --target " + label)
        return 0
    if dest.exists():
        _rmtree(dest)
    shutil.copytree(src, dest)
    count = sum(1 for _ in dest.rglob("*") if _.is_file())
    log.info(f"  {label}: {count} files copied to {dest.relative_to(PROJECT_ROOT)}")
    return count


def step5_native_binaries(log: _Log) -> None:
    log.step(5, "Native binaries - Tesseract")
    src = PROJECT_ROOT / "vendor" / "tesseract"
    dest = STAGING / "App" / "PASkills" / "tesseract"
    n = _copy_vendor_subtree(src, dest, "tesseract", log)
    if n:
        log.ok(f"tesseract bundled ({n} files)")


def step6_poppler(log: _Log) -> None:
    log.step(6, "Native binaries - Poppler")
    src = PROJECT_ROOT / "vendor" / "poppler"
    dest = STAGING / "App" / "PASkills" / "poppler"
    n = _copy_vendor_subtree(src, dest, "poppler", log)
    if n:
        log.ok(f"poppler bundled ({n} files)")


# ---------------------------------------------------------------------------
# Step 7 — Pull agents/ from upstream (local copy or git clone).
# ---------------------------------------------------------------------------

def _parse_sources_toml(path: Path) -> dict:
    """Parse bundling/sources.toml. Uses tomllib/tomli if available,
    otherwise a minimal regex parser for the flat key=value format."""
    text = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(text)

    # Minimal fallback — handles our simple single-section TOML.
    result: dict = {}
    section: dict = result
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            key = line[1:-1].strip()
            section = result.setdefault(key, {})
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            # Handle arrays: ["*.py", "*.md"]
            if v.startswith("[") and v.endswith("]"):
                items = [
                    i.strip().strip('"').strip("'")
                    for i in v[1:-1].split(",")
                    if i.strip()
                ]
                section[k] = items
            else:
                section[k] = v
    return result


def _matches_globs(rel_path: str, patterns: list[str]) -> bool:
    """True if rel_path matches any of the glob patterns."""
    for pat in patterns:
        if fnmatch.fnmatch(rel_path, pat):
            return True
    return False


def _sync_agents(source_dir: Path, dest_dir: Path,
                 includes: list[str], excludes: list[str],
                 log: _Log) -> int:
    """Copy files from source_dir to dest_dir, filtered by include/exclude
    globs. Returns the number of files copied."""
    if not source_dir.is_dir():
        log.err(f"source directory not found: {source_dir}")
        sys.exit(2)

    # Wipe dest and recreate
    if dest_dir.exists():
        _rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for src_file in sorted(source_dir.rglob("*")):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(source_dir).as_posix()

        if not _matches_globs(rel, includes):
            continue
        if _matches_globs(rel, excludes):
            continue

        dst = dest_dir / src_file.relative_to(source_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dst)
        copied += 1

    return copied


def _pull_agents_local(cfg: dict, log: _Log) -> Path:
    """Resolve the local source path for agents/."""
    local_path = cfg.get("local_path", "")
    if not local_path:
        log.err("sources.toml: kind='local' but local_path is empty")
        sys.exit(1)
    # Resolve relative to sources.toml location
    resolved = (SOURCES_TOML.parent / local_path).resolve()
    if not resolved.is_dir():
        log.err(f"local agents source not found: {resolved}")
        log.err("Ensure the sibling platform-agnostic-skills repo exists, "
                "or switch sources.toml to kind='git'.")
        sys.exit(2)
    log.info(f"source: local  {resolved}")
    return resolved


def _cache_key(url: str, ref: str) -> str:
    """Deterministic short hash for a (url, ref) pair, used as cache dir name."""
    return hashlib.sha256(f"{url}@{ref}".encode()).hexdigest()[:12]


def _pull_agents_git(cfg: dict, log: _Log) -> Path:
    """Clone or update the upstream repo into .agents_cache/ and return
    the path to the agents/ subtree inside the clone."""
    url = cfg.get("git_url", "")
    ref = cfg.get("git_ref", "") or "main"
    if not url:
        log.err("sources.toml: kind='git' but git_url is empty")
        sys.exit(1)

    cache_dir = AGENTS_CACHE / _cache_key(url, ref)
    clone_ok = False

    if cache_dir.is_dir() and (cache_dir / ".git").is_dir():
        # Cache exists — try to update
        log.info(f"cache hit: {cache_dir.relative_to(PROJECT_ROOT)}")
        try:
            subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", ref],
                cwd=cache_dir, check=True,
                capture_output=True, timeout=120,
            )
            subprocess.run(
                ["git", "checkout", "FETCH_HEAD"],
                cwd=cache_dir, check=True,
                capture_output=True, timeout=30,
            )
            clone_ok = True
            log.info(f"updated cache to {ref}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            log.warn(f"git fetch failed ({exc!r}); using stale cache")
            clone_ok = True  # stale but usable
    else:
        # Fresh clone
        cache_dir.mkdir(parents=True, exist_ok=True)
        log.info(f"cloning {url}  (ref={ref})")
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", ref, url, str(cache_dir)],
                check=True, capture_output=True, timeout=300,
            )
            clone_ok = True
            log.info("clone complete")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            log.err(f"git clone failed: {exc!r}")
            # If there's a partial clone, clean it up
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)

    if not clone_ok:
        log.err("upstream unreachable and no cached clone available")
        sys.exit(2)

    # The agents/ subtree is expected at the repo root
    agents_in_clone = cache_dir / "agents"
    if not agents_in_clone.is_dir():
        # Try src/agents/ as fallback
        agents_in_clone = cache_dir / "src" / "agents"
    if not agents_in_clone.is_dir():
        log.err(f"agents/ directory not found in cloned repo at {cache_dir}")
        sys.exit(2)

    log.info(f"source: git  {url} @ {ref}")
    return agents_in_clone


def step7_pull_agents(args: argparse.Namespace, log: _Log) -> None:
    log.step(7, "Pull agents/ from upstream")
    if args.skip_pull:
        log.info("--skip-pull set; agents/ not refreshed")
        return

    cfg = _parse_sources_toml(SOURCES_TOML).get("upstream", {})
    kind = cfg.get("kind", "local")
    includes = cfg.get("include", ["**/*.py", "**/*.md", "**/*.yaml"])
    excludes = cfg.get("exclude", ["**/__pycache__/**", "**/*.pyc"])

    if kind == "local":
        source_dir = _pull_agents_local(cfg, log)
    elif kind == "git":
        source_dir = _pull_agents_git(cfg, log)
    else:
        log.err(f"sources.toml: unknown kind '{kind}' (expected 'local' or 'git')")
        sys.exit(1)

    count = _sync_agents(source_dir, SRC_AGENTS, includes, excludes, log)
    log.ok(f"synced {count} files into src/agents/")


# ---------------------------------------------------------------------------
# Phase 2b - INI rendering, icons, launcher generator, zip.
# ---------------------------------------------------------------------------

_VERSION_CORE_RE = re.compile(r"^(\d+\.\d+\.\d+)")


def _portable_versions(version: str) -> tuple[str, str]:
    """
    VERSION_3 is the dotted-3 form (e.g. 0.2.0); VERSION_4 is dotted-4
    (e.g. 0.2.0.0). +dirty/+sha suffixes are stripped because the PA Launcher
    INI parser dislikes +.
    """
    m = _VERSION_CORE_RE.match(version)
    core = m.group(1) if m else "0.0.0"
    return core, f"{core}.0"


def _write_ini(dest: Path, text: str) -> None:
    """Write an INI string with CRLF line endings, parents created."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    normalised = text.replace("\r\n", "\n").replace("\n", "\r\n")
    dest.write_bytes(normalised.encode("utf-8"))


# Project palette per locked decision 2026-05-01.
_ICON_BG     = (10, 10, 10, 255)         # #0A0A0A
_ICON_ACCENT = (59, 130, 246, 255)       # #3B82F6
_PNG_SIZES   = {
    "appicon_16.png":  16,
    "appicon_32.png":  32,
    "appicon_75.png":  75,
    "appicon_128.png": 128,
}
_ICO_SIZES   = [(16, 16), (32, 32), (48, 48), (256, 256)]


def _draw_placeholder_master(size: int):
    """
    Render a 256-px master image in the project palette: dark square with a
    centred electric-blue circle. Returns a Pillow Image (RGBA).
    """
    from PIL import Image, ImageDraw  # type: ignore[import-not-found]
    img = Image.new("RGBA", (size, size), _ICON_BG)
    draw = ImageDraw.Draw(img)
    margin = max(1, size // 8)
    draw.ellipse([margin, margin, size - margin, size - margin], fill=_ICON_ACCENT)
    return img


def _ensure_appinfo_icons(log: _Log) -> None:
    """
    Populate staging/App/AppInfo/ with the five icon files the
    PortableApps.com Launcher Generator expects:
      appicon.ico, appicon_16.png, appicon_32.png, appicon_75.png,
      appicon_128.png.

    Real artwork at bundling/icons/<name> overrides placeholders.
    """
    appinfo_dir = STAGING / "App" / "AppInfo"
    appinfo_dir.mkdir(parents=True, exist_ok=True)

    icons_src = PROJECT_ROOT / "bundling" / "icons"
    targets = ["appicon.ico"] + list(_PNG_SIZES.keys())
    real_present = [n for n in targets if (icons_src / n).is_file()]
    missing = [n for n in targets if n not in real_present]

    for name in real_present:
        shutil.copy2(icons_src / name, appinfo_dir / name)
    if real_present:
        log.info(f"copied {len(real_present)} real icon(s) from {icons_src.relative_to(PROJECT_ROOT)}")

    if not missing:
        log.ok("appicon set complete (all real artwork)")
        return

    try:
        master = _draw_placeholder_master(256)
    except ModuleNotFoundError:
        log.err(
            "Pillow not installed in the build venv; cannot synthesise placeholder icons. "
            "Either drop real artwork into bundling/icons/, or `pip install Pillow` in .venv."
        )
        sys.exit(1)

    from PIL import Image  # type: ignore[import-not-found]
    resample = getattr(Image, "Resampling", Image).LANCZOS

    for name in missing:
        dest = appinfo_dir / name
        if name.lower() == "appicon.ico":
            master.save(dest, format="ICO", sizes=_ICO_SIZES)
        else:
            px = _PNG_SIZES[name]
            master.resize((px, px), resample).save(dest, format="PNG")
    log.warn(f"generated {len(missing)} placeholder icon(s) - drop real artwork into bundling/icons/ to override")


def step8_render_inis(version: str, log: _Log) -> None:
    log.step(8, "Render appinfo.ini + Launcher INI + icons")

    v3, v4 = _portable_versions(version)
    log.info(f"VERSION_3={v3}  VERSION_4={v4}")

    if not APPINFO_TMPL.is_file():
        log.err(f"missing template: {APPINFO_TMPL}")
        sys.exit(1)
    if not LAUNCHER_TMPL.is_file():
        log.err(f"missing template: {LAUNCHER_TMPL}")
        sys.exit(1)

    mapping = {"VERSION_3": v3, "VERSION_4": v4}

    appinfo_txt  = string.Template(APPINFO_TMPL.read_text(encoding="utf-8")).safe_substitute(mapping)
    launcher_txt = string.Template(LAUNCHER_TMPL.read_text(encoding="utf-8")).safe_substitute(mapping)

    appinfo_out  = STAGING / "App" / "AppInfo" / "appinfo.ini"
    launcher_out = STAGING / "App" / "AppInfo" / "Launcher" / "PASkillsPortable.ini"

    _write_ini(appinfo_out, appinfo_txt)
    _write_ini(launcher_out, launcher_txt)

    log.ok(f"wrote {appinfo_out.relative_to(PROJECT_ROOT)}")
    log.ok(f"wrote {launcher_out.relative_to(PROJECT_ROOT)}")

    _ensure_appinfo_icons(log)


def step9_copy_defaults(log: _Log) -> None:
    log.step(9, "Copy DefaultData -> staging")

    if not DEFAULTDATA_TMPL.is_dir():
        log.warn(f"no DefaultData template at {DEFAULTDATA_TMPL} - skipping")
        return

    dest = STAGING / "App" / "DefaultData"
    if dest.exists():
        _rmtree(dest)
    shutil.copytree(DEFAULTDATA_TMPL, dest)
    count = sum(1 for _ in dest.rglob("*") if _.is_file())
    log.ok(f"DefaultData copied -> {dest.relative_to(PROJECT_ROOT)} ({count} files)")


def _find_launcher_generator(override: str | None, log: _Log) -> Path | None:
    def _from(p: Path) -> Path | None:
        if p.is_file() and p.name.lower() == LAUNCHER_GEN_EXE.lower():
            return p
        if p.is_dir():
            direct = p / LAUNCHER_GEN_EXE
            if direct.is_file():
                return direct
            for found in p.rglob(LAUNCHER_GEN_EXE):
                return found
        return None

    if override:
        c = _from(Path(override))
        if c is not None:
            return c
        log.warn(f"--launcher-gen path did not resolve: {override}")

    env_val = os.environ.get("PASKILLS_LAUNCHER_GEN")
    if env_val:
        c = _from(Path(env_val))
        if c is not None:
            return c

    on_path = shutil.which(LAUNCHER_GEN_EXE)
    if on_path:
        return Path(on_path)

    for hint in LAUNCHER_GEN_HINTS:
        c = _from(hint)
        if c is not None:
            return c

    return None


def step10_launcher_gen(args: argparse.Namespace, log: _Log) -> bool:
    log.step(10, "Invoke PortableApps Launcher Generator")

    gen = _find_launcher_generator(args.launcher_gen, log)
    if gen is None:
        log.warn("PortableApps.comLauncherGenerator.exe not found.")
        log.warn("  Install: https://portableapps.com/apps/development/portableapps.com_launcher")
        log.warn("  Or pass: --launcher-gen <path-to-generator.exe>")
        log.warn("  Skipping; staging/PASkillsPortable.exe will not be created.")
        return False

    log.info(f"using launcher generator: {gen}")

    if platform.system() != "Windows":
        log.warn("Launcher Generator is a Windows .exe; current platform isn't Windows.")
        log.warn("  Skipping; rerun this step on Windows to wrap pa_skills.exe.")
        return False

    res = subprocess.run([str(gen), str(STAGING)], cwd=PROJECT_ROOT, check=False)
    if res.returncode != 0:
        log.warn(f"launcher generator exited {res.returncode} - wrapper exe may not be present")

    wrapper = STAGING / "PASkillsPortable.exe"
    if wrapper.is_file():
        log.ok(f"wrapper produced: {wrapper.relative_to(PROJECT_ROOT)}")
        return True

    # Surface the launcher's own log when it bails - the generator writes
    # PortableApps.comLauncherGeneratorLog.txt next to its exe.
    gen_log = gen.parent / "PortableApps.comLauncherGeneratorLog.txt"
    if gen_log.is_file():
        log.warn(f"see: {gen_log}")
    log.warn(f"expected wrapper not found at {wrapper}")
    return False


def _iter_staging_files() -> Iterable[Path]:
    for p in sorted(STAGING.rglob("*")):
        if p.is_file():
            yield p


def step11_zip(version: str, log: _Log, *, launcher_ok: bool) -> Path | None:
    """
    Produce dist/PASkillsPortable_<version>.zip from staging/.

    Deterministic: sorted file order, fixed (2026,1,1,0,0,0) timestamp on
    each entry, so identical inputs produce a byte-identical archive.
    """
    log.step(11, "Zip staging -> dist")

    if not launcher_ok:
        log.warn("launcher wrapper missing - skipping zip. Run step10 successfully, then re-build.")
        return None

    if not STAGING.is_dir():
        log.err(f"staging missing: {STAGING}")
        sys.exit(2)

    v3, _ = _portable_versions(version)
    DIST.mkdir(parents=True, exist_ok=True)
    out = DIST / f"PASkillsPortable_{v3}.zip"
    if out.exists():
        out.unlink()

    fixed_time = (2026, 1, 1, 0, 0, 0)
    count = 0
    total_bytes = 0
    top = "PASkillsPortable"

    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in _iter_staging_files():
            rel = path.relative_to(STAGING).as_posix()
            arcname = f"{top}/{rel}"
            info = zipfile.ZipInfo(filename=arcname, date_time=fixed_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            data = path.read_bytes()
            zf.writestr(info, data)
            count += 1
            total_bytes += len(data)

    mb = total_bytes / (1024 * 1024)
    log.ok(f"wrote {out.relative_to(PROJECT_ROOT)}  ({count} files, {mb:.1f} MiB uncompressed)")
    return out


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
    p.add_argument(
        "--launcher-gen",
        metavar="PATH",
        help="Path to PortableApps.comLauncherGenerator.exe (or its folder). "
             "Otherwise PASKILLS_LAUNCHER_GEN env var, PATH, and standard install hints are searched.",
    )
    p.add_argument(
        "--skip-launcher",
        action="store_true",
        help="Don't invoke the launcher generator (also skips the zip step).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log = _Log(use_color=not args.no_color)

    log.info(f"project root: {PROJECT_ROOT}")
    log.info(f"python:       {sys.executable} ({sys.version.split()[0]})")
    log.info(f"platform:     {platform.system()} {platform.release()}")

    version, sha, dirty = step1_resolve_version(args, log)
    step2_reset_staging(log)
    step7_pull_agents(args, log)
    py = step3_create_venv(args, log)
    step4_run_pyinstaller(py, log)

    step5_native_binaries(log)
    step6_poppler(log)
    step8_render_inis(version, log)
    step9_copy_defaults(log)

    if args.skip_launcher:
        log.step(10, "Invoke PortableApps Launcher Generator - skipped (--skip-launcher)")
        log.step(11, "Zip staging -> dist - skipped (no launcher)")
        launcher_ok = False
        zip_path: Path | None = None
    else:
        launcher_ok = step10_launcher_gen(args, log)
        zip_path = step11_zip(version, log, launcher_ok=launcher_ok)

    log.ok(f"build complete (version {version}, sha {sha})")
    log.info(f"Frozen output: {STAGING / 'App' / 'PASkills' / 'pa_skills.exe'}")
    if launcher_ok:
        log.info(f"Wrapper:       {STAGING / 'PASkillsPortable.exe'}")
    if zip_path is not None:
        log.info(f"Release zip:   {zip_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
