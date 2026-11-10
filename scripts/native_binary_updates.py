#!/usr/bin/env python3
"""
scripts/native_binary_updates.py -- compare the pinned native-binary versions in
bundling/binaries.toml with the latest upstream release tags.

Used by .github/workflows/check-native-binaries.yml. The workflow looks up the
latest tags and passes them in; this script does the comparing, so the logic is
testable with fixed inputs.

    python scripts/native_binary_updates.py --toml bundling/binaries.toml \
        --tesseract 5.5.4 --poppler v24.07.0-0 --qpdf v11.9.1

Prints one markdown bullet per binary whose upstream release is NEWER than the
pin, and nothing when every pin is current (or ahead). A tag that is empty or
unparseable is skipped with a note on stderr, never reported as an update.

Upstreams: Tesseract = tesseract-ocr/tesseract (tags like 5.5.4); the pin's
version "5.5.0.20241111" is release 5.5.0 plus the installer's build date, so
only its first three numbers are compared. Poppler = oschwartz10612/poppler-windows
(tags like v24.07.0-0). qpdf = qpdf/qpdf (tags like v11.9.1).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

RELEASES = {
    "tesseract": "https://github.com/tesseract-ocr/tesseract/releases",
    "poppler": "https://github.com/oschwartz10612/poppler-windows/releases",
    "qpdf": "https://github.com/qpdf/qpdf/releases",
}
LABELS = {"tesseract": "Tesseract", "poppler": "Poppler", "qpdf": "qpdf"}


def parse_version(text: str):
    """'v24.07.0-0' -> (24, 7, 0); '5.5.0.20241111' -> (5, 5, 0); '' -> None.
    Takes the first three dotted numbers; a trailing '-N' build suffix and any
    fourth component (an installer date) are ignored."""
    m = re.match(r"^\s*v?(\d+)\.(\d+)\.(\d+)", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def updates(pinned: dict, latest: dict):
    """(report_lines, notes). A line only when latest > pinned."""
    lines, notes = [], []
    for name in ("tesseract", "poppler", "qpdf"):
        pin, tag = pinned.get(name, ""), latest.get(name, "")
        pv, lv = parse_version(pin), parse_version(tag)
        if pv is None:
            notes.append(f"{name}: pinned version {pin!r} not understood - skipped")
            continue
        if lv is None:
            notes.append(f"{name}: latest tag {tag!r} not understood - skipped")
            continue
        if lv > pv:
            shown = tag.lstrip("v")
            lines.append(f"- **{LABELS[name]}**: pinned `{pin}`, latest `{shown}` "
                         f"([releases]({RELEASES[name]}))")
    return lines, notes


def pinned_versions(toml_text: str) -> dict:
    data = tomllib.loads(toml_text)
    return {n: str(data.get(n, {}).get("version", "")) for n in ("tesseract", "poppler", "qpdf")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--toml", required=True, type=Path)
    for n in ("tesseract", "poppler", "qpdf"):
        ap.add_argument(f"--{n}", default="", help=f"latest upstream tag for {n}")
    args = ap.parse_args(argv)
    pinned = pinned_versions(args.toml.read_text(encoding="utf-8"))
    latest = {n: getattr(args, n) for n in ("tesseract", "poppler", "qpdf")}
    lines, notes = updates(pinned, latest)
    for n in notes:
        print(n, file=sys.stderr)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
