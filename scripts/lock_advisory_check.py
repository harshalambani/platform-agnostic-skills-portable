"""scripts/lock_advisory_check.py - check the locked packages against the public
advisory list (pip-audit) and fail on any finding.

CI runs this on requirements-lock.txt. Three guards keep it from going green
over nothing:

* the pinned packages are counted in the lock first; fewer than the floor is an
  error (an empty or truncated lock must not pass);
* pip-audit's own report must list at least the floor's worth of audited
  packages (a tool that audited nothing must not pass);
* an advisory id is only skipped if it is written in the ignore file.

Usage:
    python scripts/lock_advisory_check.py [--lock FILE] [--ignore-file FILE]
                                          [--floor N] [--pip-audit CMD]

Exit codes: 0 clean, 1 findings, 2 the floor was not met, 3 pip-audit failed.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = ROOT / "requirements-lock.txt"
DEFAULT_IGNORE = ROOT / ".github" / "pip-audit-ignore.txt"
DEFAULT_FLOOR = 80          # the lock holds about 100 pinned packages

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==", re.M)


def count_pins(lock_text: str) -> int:
    """Pinned packages in a hashed lock ("name==1.2.3 \\" lines)."""
    return len(_PIN_RE.findall(lock_text))


def read_ignore_ids(text: str) -> list[str]:
    """One advisory id per line; '#' starts a comment; blank lines are skipped."""
    ids = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.append(line.split()[0])
    return ids


def findings_from_report(report: dict) -> list[dict]:
    out = []
    for dep in report.get("dependencies", []):
        for v in dep.get("vulns", []) or []:
            out.append({"package": dep.get("name"), "version": dep.get("version"),
                        "id": v.get("id"), "aliases": v.get("aliases", []),
                        "fix": ", ".join(v.get("fix_versions", []) or []) or "none listed"})
    return out


def audited_count(report: dict) -> int:
    return sum(1 for d in report.get("dependencies", []) if "skip_reason" not in d)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock", default=str(DEFAULT_LOCK))
    ap.add_argument("--ignore-file", default=str(DEFAULT_IGNORE))
    ap.add_argument("--floor", type=int, default=DEFAULT_FLOOR)
    ap.add_argument("--pip-audit", default="pip-audit", help="the pip-audit command to run")
    args = ap.parse_args(argv)

    lock = Path(args.lock)
    n = count_pins(lock.read_text(encoding="utf-8")) if lock.is_file() else 0
    print(f"Lock file: {n} pinned packages (floor {args.floor}).")
    if n < args.floor:
        print(f"::error::only {n} pinned packages found in {lock.name}; the floor is "
              f"{args.floor}. The check will not pass over an empty or truncated lock.")
        return 2

    ignore_path = Path(args.ignore_file)
    ignore = read_ignore_ids(ignore_path.read_text(encoding="utf-8")) if ignore_path.is_file() else []
    print(f"Reviewed advisories skipped: {len(ignore)} {ignore if ignore else ''}")

    cmd = [*shlex.split(args.pip_audit), "-r", str(lock), "--require-hashes", "--no-deps",
           "--disable-pip", "-f", "json", "--progress-spinner", "off"]
    for i in ignore:
        cmd += ["--ignore-vuln", i]
    cp = subprocess.run(cmd, capture_output=True, text=True)
    try:
        report = json.loads(cp.stdout)
    except ValueError:
        print(cp.stdout[-1500:], cp.stderr[-1500:])
        print("::error::pip-audit did not produce a report.")
        return 3
    audited = audited_count(report)
    print(f"pip-audit checked {audited} packages.")
    if audited < args.floor:
        print(f"::error::pip-audit checked only {audited} packages; the floor is {args.floor}.")
        return 2

    found = findings_from_report(report)
    if found:
        print(f"{len(found)} advisory finding(s):")
        for f in found:
            print(f"  {f['package']}=={f['version']}  {f['id']}  fixed in: {f['fix']}")
        print("::error::the locked packages have advisories. Review each one; an advisory that "
              "has been reviewed goes in .github/pip-audit-ignore.txt with its reason.")
        return 1
    if cp.returncode not in (0,):
        print(cp.stderr[-1500:])
        print(f"::error::pip-audit exited {cp.returncode} with no findings reported.")
        return 3
    print("No advisories found in the locked packages.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
