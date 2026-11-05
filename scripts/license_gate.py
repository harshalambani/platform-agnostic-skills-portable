"""scripts/license_gate.py - fail if a locked package declares a copyleft licence.

Every package pinned in ``requirements-lock.txt`` is looked up among the
installed distributions and its licence metadata is classified. A package that
declares GPL, LGPL or AGPL (in any spelling, alone or as part of a dual
licence) in ``License``, ``License-Expression`` or a licence classifier fails
the gate. There is deliberately no allowlist: a flagged package is removed or
replaced, not excused.

Packages the lock names but that are not installed in the current environment
are listed. The CI runner leaves out the native-window pair on purpose (see
``scripts/ci_lock_subset.py``), so ``EXPECTED_ABSENT`` names exactly those; any
other absent package fails, so the gate cannot quietly stop looking at things.

Usage:
    python scripts/license_gate.py [--lock requirements-lock.txt]
"""
from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

# Not installed on the CI runner (pythonnet needs .NET, pywebview needs GTK/Qt).
EXPECTED_ABSENT = frozenset({"pywebview", "pythonnet"})

# Short forms: GPL, LGPL, AGPL, GPL-3.0, GPLv3, LGPLv3+, "GPL 2 or later" ...
# A letter in front means some other word ("xgpl"), so it is not a match.
_SHORT = re.compile(r"(?<![a-z0-9])[al]?gpl(?![a-z])|(?<![a-z0-9])[al]?gpl(?=v?\d)")
# Long forms.
_LONG = re.compile(
    r"gnu\s+(?:(?:lesser|library|affero)\s+)?general\s+public\s+licen[sc]e"
    r"|gnu\s+(?:lesser|library|affero)\b"
    r"|(?:lesser|affero)\s+general\s+public\s+licen[sc]e"
)

# A License field longer than this is a pasted licence text, not a name. Only
# its head is classified: a permissive text may legitimately mention the GNU
# licences in a clause further down, whereas a copyleft text names itself first.
_HEAD = 400


def _hits(text: str) -> bool:
    low = " ".join(text.lower().split())
    return bool(_SHORT.search(low) or _LONG.search(low))


def classify_license(
    license_field: Optional[str] = None,
    license_expression: Optional[str] = None,
    classifiers: Sequence[str] = (),
) -> list[str]:
    """Return a list of reasons this metadata declares a GPL-family licence.

    An empty list means nothing copyleft was declared (it says nothing about
    packages that declare no licence at all)."""
    reasons: list[str] = []
    if license_expression and _hits(license_expression):
        reasons.append(f"License-Expression: {license_expression.strip()[:80]}")
    if license_field and _hits(license_field[:_HEAD]):
        reasons.append(f"License: {' '.join(license_field.split())[:80]}")
    for c in classifiers:
        if c.startswith("License ::") and _hits(c):
            reasons.append(f"Classifier: {c}")
    return reasons


def lock_names(lock_text: str) -> list[str]:
    """Distribution names pinned in a hashed lock file."""
    return [
        m.group(1)
        for m in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", lock_text, re.M)
    ]


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_installed(
    names: Iterable[str],
    lookup=None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Classify each named distribution.

    ``lookup(name)`` returns a metadata mapping with ``get``/``get_all`` or
    raises ``metadata.PackageNotFoundError``. Returns (violations, absent)."""
    lookup = lookup or metadata.metadata
    violations: dict[str, list[str]] = {}
    absent: list[str] = []
    for name in names:
        try:
            md: Mapping = lookup(name)
        except metadata.PackageNotFoundError:
            absent.append(_norm(name))
            continue
        reasons = classify_license(
            md.get("License"),
            md.get("License-Expression"),
            md.get_all("Classifier") or [],
        )
        if reasons:
            violations[_norm(name)] = reasons
    return violations, absent


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock", default=str(Path(__file__).resolve().parents[1] / "requirements-lock.txt"))
    args = ap.parse_args(argv)
    names = lock_names(Path(args.lock).read_text(encoding="utf-8"))
    violations, absent = check_installed(names)
    unexpected = sorted(set(absent) - EXPECTED_ABSENT)
    print(f"Checked {len(names) - len(absent)} of {len(names)} locked packages.")
    if absent:
        print("Not installed here: " + ", ".join(sorted(absent)))
    for name, reasons in sorted(violations.items()):
        print(f"GPL-family licence: {name}")
        for r in reasons:
            print(f"    {r}")
    if unexpected:
        print("Locked but not installed (not on the expected list): " + ", ".join(unexpected))
    if violations or unexpected:
        return 1
    print("No GPL, LGPL or AGPL licence declared by any checked package.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
