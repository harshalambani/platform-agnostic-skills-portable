"""
scripts/regen_lock.py - Regenerate requirements-lock.txt with full SHA-256 hashes.

This script is the single authoritative way to update the dependency lock.
It:
  1. Detects uv on PATH and uses it (much faster resolver, ~10x speed-up).
  2. Falls back to pip-compile (pip-tools) if uv is not found.
  3. Runs the chosen tool with --generate-hashes to produce a fully-pinned,
     hashed requirements-lock.txt from requirements.txt.
  4. Verifies the output is UTF-8 and contains hashes.
  5. Reports the number of pinned packages.

Run from the repo root:

    python scripts\\regen_lock.py

After running, commit both requirements.txt (if changed) and the new
requirements-lock.txt together so they stay in sync.

The resulting lock file is used by bundling/build.py with --require-hashes
--no-deps, which means pip will refuse to install any package whose wheel
hash does not match an entry in the file. This prevents supply-chain
substitution even if a package index is compromised.

NOTE: uv is optional but strongly recommended for speed.
  Install: pipx install uv
  Docs: https://github.com/astral-sh/uv
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements.txt"
LOCK_OUT = ROOT / "requirements-lock.txt"


def _python_exe() -> str:
    """
    Prefer the project .venv Python so pip-compile can reuse already-downloaded
    wheel metadata (much faster). Fall back to sys.executable (system Python).
    """
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"  # Windows
    if not venv_py.is_file():
        venv_py = ROOT / ".venv" / "bin" / "python"      # Linux/macOS
    if venv_py.is_file():
        print(f"  using venv Python: {venv_py}")
        return str(venv_py)
    print(f"  using system Python: {sys.executable}")
    return sys.executable


def _compile_with_uv(req_in: Path, lock_out: Path) -> None:
    """Compile lock file using uv (fast, modern resolver)."""
    cmd = [
        "uv", "pip", "compile",
        "--generate-hashes",
        "--resolver", "backtracking",
        "--output-file", str(lock_out),
        "--no-header",
        str(req_in),
    ]
    print("$", " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(
            "\nERROR: uv pip compile failed.\n"
            "  Make sure you are running from a terminal with internet access.\n"
            "  Ensure uv is up-to-date: pipx upgrade uv",
            file=sys.stderr,
        )
        sys.exit(result.returncode)


def _compile_with_piptools(py: str, req_in: Path, lock_out: Path) -> None:
    """Compile lock file using pip-compile (fallback when uv is unavailable)."""
    cmd = [
        py, "-m", "piptools", "compile",
        "--generate-hashes",
        "--output-file", str(lock_out),
        "--resolver", "backtracking",
        "--no-header",
        str(req_in),
    ]
    print("$", " ".join(cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(
            "\nERROR: pip-compile failed.\n"
            "  Make sure you are running from a venv that has internet access.\n"
            "  Try: pip install pip-tools  then re-run this script.",
            file=sys.stderr,
        )
        sys.exit(result.returncode)


def main() -> int:
    print("=" * 60)
    print("PA Skills Portable - dependency lock regeneration")
    print("=" * 60)

    if not REQUIREMENTS.is_file():
        print(f"ERROR: requirements.txt not found at {REQUIREMENTS}", file=sys.stderr)
        return 1

    py = _python_exe()

    # Detect uv on PATH; prefer it if available, fall back to pip-tools.
    uv_path = shutil.which("uv")

    if uv_path:
        print(f"\nuv found at {uv_path} - using uv pip compile (fast resolver).\n")
        print(f"Compiling {REQUIREMENTS.name} -> {LOCK_OUT.name} with hashes...\n")
        print("NOTE: this typically takes 1-3 minutes with uv.\n")
        _compile_with_uv(REQUIREMENTS, LOCK_OUT)
    else:
        print("\nuv not found on PATH. Falling back to pip-compile (pip-tools).")
        print("TIP: for ~10x faster resolution, install uv: pipx install uv\n")

        # Ensure pip-tools is available in the chosen Python.
        result = subprocess.run(
            [py, "-c", "import piptools"],
            capture_output=True,
        )
        if result.returncode == 0:
            print("pip-tools already installed.")
        else:
            print("pip-tools not found - installing...")
            subprocess.run([py, "-m", "pip", "install", "pip-tools"], check=True)

        # Run pip-compile.
        print(f"\nCompiling {REQUIREMENTS.name} -> {LOCK_OUT.name} with hashes...\n")
        print("NOTE: this takes 5-15 minutes for a large dependency tree (gradio + langchain).\n")
        _compile_with_piptools(py, REQUIREMENTS, LOCK_OUT)

    # Verify the output.
    if not LOCK_OUT.is_file():
        print(f"ERROR: expected output not found at {LOCK_OUT}", file=sys.stderr)
        return 1

    text = LOCK_OUT.read_text(encoding="utf-8")   # pip-compile writes UTF-8

    if "--hash=sha256:" not in text:
        print(
            "ERROR: output lock file contains no hashes. "
            "pip-compile may not have run with --generate-hashes correctly.",
            file=sys.stderr,
        )
        return 1

    n_packages = sum(1 for line in text.splitlines()
                     if line and not line.startswith(("#", " ", "\t", "-")))
    n_hashes = text.count("--hash=sha256:")

    print(f"\nOK: {LOCK_OUT.name} written")
    print(f"   {n_packages} packages pinned, {n_hashes} hash entries")
    print(
        "\nNext steps:\n"
        "  1. Review the diff: git diff requirements-lock.txt\n"
        "  2. Commit: git add requirements.txt requirements-lock.txt && "
        "git commit -m 'chore: regenerate dependency lock with hashes'"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
