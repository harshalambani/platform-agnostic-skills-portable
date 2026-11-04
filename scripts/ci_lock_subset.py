"""scripts/ci_lock_subset.py - emit requirements-lock.txt minus named packages.

Why this exists
---------------
The frozen build installs from ``requirements-lock.txt``; CI used to test
against the loose pins in ``requirements.txt``. Those resolve to different
version sets, so a green suite said nothing about the artifact users actually
run. (Concretely: the lock shipped ``tomlkit==0.15.1`` against gradio's
``tomlkit<0.15.0`` and ``click==8.4.0`` against huggingface-hub's
``click>=8.4.2`` for weeks, and every build installed exactly that, because
``build.py`` passes ``--no-deps`` and so never evaluates anyone's metadata.)

Testing against the lock closes that gap. The one obstacle is that a bare
ubuntu runner cannot take the native-window pair -- pythonnet wants a .NET
toolchain, pywebview wants GTK/Qt -- and the lock carries no environment
markers to skip them by. So they have to come out of the file.

The naive version of that is ``grep -vE '^(pywebview|pythonnet)'``, which is
wrong on a hashed lock: it strips the ``name==version \\`` line and leaves the
orphaned ``--hash=`` continuation lines behind, which pip then reads as part of
the *previous* package. This script removes whole blocks instead.

Two guards, both aimed at the failure mode where the filter silently stops
filtering:

* An excluded name that is not present in the lock is an ERROR, not a shrug.
  Otherwise a rename upstream turns this into a no-op and the next CI run
  fails somewhere far away with no clue why.
* A package may only be dropped if everything that requires it is also being
  dropped. pywebview requires pythonnet, so removing pythonnet alone would
  leave an unsatisfiable file. The ``# via`` comments record exactly this, so
  it can be checked rather than assumed.

Usage:
    python scripts/ci_lock_subset.py --exclude pywebview pythonnet --out sub.txt
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = ROOT / "requirements-lock.txt"

# A block starts on a pinned requirement line: "name==1.2.3 \"
_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==")
# "# via -r requirements.txt" / "#   pywebview" / "# via gradio"
_VIA_INLINE_RE = re.compile(r"^\s*#\s*via\s+(\S.*)$")
_VIA_CONT_RE = re.compile(r"^\s*#\s{2,}(\S.*)$")


def canon(name: str) -> str:
    """PEP 503 normalisation, so Clr-Loader / clr_loader / clr-loader agree."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


class Block:
    __slots__ = ("name", "lines", "vias")

    def __init__(self, name: str) -> None:
        self.name = name
        self.lines: list[str] = []
        self.vias: list[str] = []


def parse(text: str) -> tuple[list[str], list[Block]]:
    """Split the lock into its leading preamble and one Block per package."""
    preamble: list[str] = []
    blocks: list[Block] = []
    cur: Block | None = None

    for line in text.splitlines(keepends=True):
        m = _PIN_RE.match(line)
        if m:
            cur = Block(canon(m.group(1)))
            blocks.append(cur)
            cur.lines.append(line)
            continue

        if cur is None:
            preamble.append(line)
            continue

        cur.lines.append(line)

        # Collect the "# via" targets so dependants can be checked.
        m_inline = _VIA_INLINE_RE.match(line)
        if m_inline:
            target = m_inline.group(1).strip()
            if target:
                cur.vias.append(target)
            continue
        m_cont = _VIA_CONT_RE.match(line)
        if m_cont:
            cur.vias.append(m_cont.group(1).strip())

    return preamble, blocks


def build_subset(text: str, exclude: list[str]) -> str:
    preamble, blocks = parse(text)
    if not blocks:
        sys.exit("ERROR: no pinned requirements found -- is this a lock file?")

    drop = {canon(e) for e in exclude}
    present = {b.name for b in blocks}

    missing = sorted(drop - present)
    if missing:
        sys.exit(
            "ERROR: --exclude names not present in the lock: "
            + ", ".join(missing)
            + "\n  The filter would silently do nothing. If the package was "
            "renamed or\n  dropped upstream, update the --exclude list.")

    # A dropped package must not be required by a kept one. "# via X" on
    # package P means X requires P, so X must be dropped too (or be the
    # requirements.txt top level, which is not a package).
    for b in blocks:
        if b.name not in drop:
            continue
        for via in b.vias:
            if via.startswith("-r ") or via.startswith("-c "):
                continue  # top-level pin, not a package dependant
            if canon(via) not in drop:
                sys.exit(
                    f"ERROR: cannot drop '{b.name}' -- '{via}' requires it and "
                    "is being kept.\n  Add it to --exclude, or stop excluding "
                    f"'{b.name}'.")

    kept = [b for b in blocks if b.name not in drop]
    out = "".join(preamble) + "".join("".join(b.lines) for b in kept)
    if not out.endswith("\n"):
        out += "\n"
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    ap.add_argument("--exclude", nargs="+", required=True)
    ap.add_argument("--out", type=Path, help="default: stdout")
    args = ap.parse_args(argv)

    if not args.lock.is_file():
        sys.exit(f"ERROR: lock file not found: {args.lock}")

    subset = build_subset(args.lock.read_text(encoding="utf-8"), args.exclude)

    if args.out:
        args.out.write_text(subset, encoding="utf-8")
        n = sum(1 for ln in subset.splitlines() if _PIN_RE.match(ln))
        print(f"wrote {args.out} ({n} packages, "
              f"excluded: {', '.join(sorted(canon(e) for e in args.exclude))})")
    else:
        sys.stdout.write(subset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
