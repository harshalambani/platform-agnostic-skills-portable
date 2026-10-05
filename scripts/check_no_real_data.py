"""SEC-13/16/17 guard: no real credentials or account data in tracked files.

Run by CI and by tests/test_no_real_data.py.  Two checks, neither of which
contains a real value:

1. SHAPE -- a constant named REAL_*PASSWORD / REAL_*PWD assigned a non-empty
   string literal.  Real-sample tests must read such a value from the
   environment, never from the source.
2. HASH -- every alphanumeric token of 6+ characters in a tracked text file is
   hashed (sha256 of the lower-cased token, also with leading zeros stripped)
   and compared with a denylist of values that were once committed by mistake
   (statement passwords, an account number, real transaction references).
   Only the hashes are stored here, so this file does not leak them.

Exit 0 = clean, 1 = something found (file:line and the kind, never the value).
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

DENYLIST_SHA256 = frozenset({
    "03734960fb5dd65e598751fbbeca63acf447fa8b1d2e5a323cd6d64ebf31f9b5",
    "244f0742a878274bfaebf36489602b153f2f05c77150bba34f015548c02e21bb",
    "4269274f8108be97eb8bade0837554cbccacc3d25c59ee6c8ec06fd86237c37e",
    "81528a2cefb2133589707b0cf06a623ef914113f53a3bfb5f67d414f53cb504e",
    "88395305c2c48693ba10fcbe03460fd091b267427bbb88a2adc117207d14e6d1",
    "94a53d5bb3b7c11126940675b1816f2649e9fa7c6880bcd7190c98ee8a8131de",
    "975bdffe37221062efa1c83fb1194ed57a03039c1aaffdff4b44a3558ba9865b",
    "99016dd56b0aaf444dc52488e48e623b758a10b67cb0062d620e8b0547df777f",
    "9df9098d95de86bb396fcbac6dbdd6bd74a309bde1a22d6c42b5203210bf134d",
    "d6acb7f0ef759b5d763ae136ba4ea44f8a3b544efd323acd5e4fccba288b6129",
})

SHAPES = [
    ("real-password constant with a literal value",
     re.compile(r"""REAL_\w*(?:PASSWORD|PWD)\w*\s*=\s*[rbf]?["'][^"']+["']""", re.I)),
]
TOKEN = re.compile(r"[A-Za-z0-9]{6,}")
SKIP_NAMES = {"requirements-lock.txt", "check_no_real_data.py", "test_no_real_data.py"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".xlsx", ".xls", ".zip",
                 ".gnucash", ".exe", ".dll", ".whl", ".pyc", ".woff", ".woff2", ".ttf"}


def _digest(token: str) -> str:
    return hashlib.sha256(token.lower().encode("utf-8")).hexdigest()


def scan_text(text: str) -> list[tuple[int, str]]:
    """[(line_no, kind)] for one file's text."""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, rx in SHAPES:
            if rx.search(line):
                found.append((n, kind))
        for tok in TOKEN.findall(line):
            if _digest(tok) in DENYLIST_SHA256 or (
                    tok.startswith("0") and _digest(tok.lstrip("0")) in DENYLIST_SHA256):
                found.append((n, "a value that was removed under SEC-13/16/17"))
    return found


def tracked_files(root: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, check=True).stdout
    return [root / p for p in out.decode("utf-8", "ignore").splitlines() if p]


def scan_repo(root: Path, files=None) -> list[str]:
    problems = []
    for f in (files if files is not None else tracked_files(root)):
        if f.name in SKIP_NAMES or f.suffix.lower() in SKIP_SUFFIXES or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, kind in scan_text(text):
            problems.append(f"{f.relative_to(root).as_posix()}:{n}: {kind}")
    return problems


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    problems = scan_repo(root)
    for p in problems:
        print(p)
    if problems:
        print(f"{len(problems)} finding(s): remove the value (use a synthetic one); "
              "do not commit real credentials or account data.")
        return 1
    print("no real credentials or account data found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
