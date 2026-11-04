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

SEC-20 adds a third check on the same walk: an employer / firm name (and a bank
reference marker) must not reappear in any tracked text file as a whole word,
case-insensitively. Only hashes are stored, so this file holds neither string.
Employer markers belong in per-entity config (`reimbursement_markers`), and
tests use a synthetic firm.

SEC-22 adds a fourth check: listed-company names, a ticker, an ISIN and an
order reference that tests and docs once used as examples must not reappear
(hashes only). The public price / corporate-action lists under
src/agents/skill_itr_workbook/data/ are exempt from this one list alone.

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
    "2b93af703b10334ee066de5926a2a36b53d57d5664359402b10186fb3f43624c",  # SEC-21
    "02b8dedc020796898e0231f16086351ae6248158f51475be906b4006bc0737fd",  # SEC-23
    "30822c0a45c4b87b6bf96b7857872b1550d3e6a55fbf7f98ad4b54556ad3059f",  # SEC-23
    "649f419b18d6d5b537ac87c71504b50867e3c8bdef87d5968963360742031075",  # SEC-23
    "6c9f04616daea86cd92ab1380fcad417037f9524d97d37a7b8bf70c7a05c28ec",  # SEC-23
    "704722aeb4a6a08615eac286a63cb97913485afb434a4c0c640d0bd4cc6cb0d8",  # SEC-23
    "73fce7050250076a58c763e060c74c0cfb87698599c3af112f5a735682492419",  # SEC-23
    "97c48cb39254cd76814d99d8a580680a28ce007cbe24f126f040abca0ab272ff",  # SEC-23
    "bbcb0603a09b2cd9e7e4e4aa9e65cb11921a7b561683e82f4fdf3df1e156b0cf",  # SEC-23
    "bc8428ef0365f83e2005574daed36d6e3316dad66434f3f3fa2e05534b70a34a",  # SEC-23
    "c773bf2c19a3b02e9567ff63a477d5709c0a8f74b19fa59f29c277238d1295fd",  # SEC-23
    "cbe406ca1ec22328e372f70268d7a10c5cdcf545feeb8ceeefed6826b5acf5c0",  # SEC-23
    "f110d32f29018d2015451df03db269d97f4e59f850b713181fb6091ad23006ba",  # SEC-23
    "f72a236b9265956897d2282fd2fc1586ffe9bb32e3d6ee6576ab4e7ad1c82ba1",  # SEC-23
    "f795ada5efac14371b3aace12a84b24c958ef9bec20addb58d1d2bd70494dbf6",  # SEC-23
})

# SEC-20: sha256 of the lower-cased whole word (a 4-letter firm name and a
# 4-letter bank reference marker). Hashes only, so this file never matches itself.
EMPLOYER_NAME_SHA256 = frozenset({
    "344bff8b2788383a072daddf6088c90e6b7b2f026e992a7267ff5094ad6c13eb",
    "3d8bf2c74d4fba32e752aa536d84008212f477a7c57e02c15e7f0926e050a5bd",
})
EMPLOYER_NAME_LENGTHS = frozenset({4})   # only words of these lengths are hashed
WORD = re.compile(r"\w+")

# SEC-22: sha256 of lower-cased tokens (listed-company names, a ticker symbol, an
# ISIN, an order reference) that tests and docs used as examples. Hashes only.
# These tokens are public in the price / corporate-action lists under
# LISTED_NAME_OK_DIR, so that directory alone is exempt from this list; the
# older DENYLIST_SHA256 and EMPLOYER_NAME_SHA256 still apply there.
LISTED_NAME_SHA256 = frozenset({
    "0f65c26224eb381072e46e95e45883be7758fdeaae164e40f065d702fb52e567",
    "1cac4d101ee696fd7a7089a7a97c1e9149409536e170f93ecba08d110d12dcfe",
    "41be67fc4277aa0aa976c6f5f09bf78f0a6b5f01b5a8d716240b2ab5457938a6",
    "98b52c4b6b7d1f48e7477a5ccc10955dd195d0ac5a38c8281bfeb08762634909",
    "bc26f6b88c37735bcae46061b3abc1c90e96c813539eb8c546e6b5dd08eee3f8",
    "d246936244c8c3b9ae86afe5d8032a02473e03ec818c36ca5087fcc8458ab458",
    "518951c1f845a1c4409bdc6246b29eb96d5617a2389f74ea23399f68d5f9cb97",  # SEC-22
    "ede455d0876bad7a447b939d125dba8fd6f55503bf8f27c3bf72f2fd8cb8f603",  # SEC-22
    "5cafe3c306a6a95bb03cc1afba6d768c20cd1cf78ee8d5ad578b51bfaa42895f",  # SEC-27
})
LISTED_NAME_OK_DIR = "src/agents/skill_itr_workbook/data/"
ALNUM4 = re.compile(r"[A-Za-z0-9]{4,}")

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


def scan_text(text: str, allow_listed_names: bool = False) -> list[tuple[int, str]]:
    """[(line_no, kind)] for one file's text.

    allow_listed_names exempts only the SEC-22 list (public price lists)."""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, rx in SHAPES:
            if rx.search(line):
                found.append((n, kind))
        for tok in TOKEN.findall(line):
            if _digest(tok) in DENYLIST_SHA256 or (
                    tok.startswith("0") and _digest(tok.lstrip("0")) in DENYLIST_SHA256):
                found.append((n, "a value that was removed under SEC-13/16/17"))
        for w in WORD.findall(line):
            if len(w) in EMPLOYER_NAME_LENGTHS and _digest(w) in EMPLOYER_NAME_SHA256:
                found.append((n, "an employer / firm name removed under SEC-20 "
                                 "(use per-entity config or a synthetic name)"))
        if not allow_listed_names:
            for tok in ALNUM4.findall(line):
                if _digest(tok) in LISTED_NAME_SHA256:
                    found.append((n, "a listed-company example removed under SEC-22 "
                                     "(use a synthetic name)"))
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
        rel = f.relative_to(root).as_posix()
        for n, kind in scan_text(text, allow_listed_names=rel.startswith(LISTED_NAME_OK_DIR)):
            problems.append(f"{rel}:{n}: {kind}")
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
