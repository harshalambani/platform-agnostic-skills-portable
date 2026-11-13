"""
cleanup.py -- step 5: the clean-up LIST and the Recycle Bin script.

This module never deletes. It lists, in the closing year's GnuCash folder,
only timestamped backups (name.gnucash.YYYYMMDDhhmmss.gnucash) and .log
files, plus the closing year's Drive shortcut (book filename + .lnk). The
final book is never listed. The script it writes is run by the user.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

BACKUP_RE = re.compile(r"^.+\.gnucash\.\d{14}\.gnucash$", re.IGNORECASE)
LOG_RE = re.compile(r"^.+\.log$", re.IGNORECASE)


@dataclass(frozen=True)
class CleanupItem:
    path: str
    size: int
    modified: str
    kind: str            # "backup" | "log" | "shortcut"


def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _item(path: Path, kind: str) -> CleanupItem:
    st = path.stat()
    return CleanupItem(
        str(path), st.st_size,
        datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"), kind)


def list_cleanup(gnucash_dir: Path, final_book: Path, drive_dir: Path) -> list[CleanupItem]:
    """The exact items step 5 offers. Read-only."""
    final = _norm(final_book)
    items: list[CleanupItem] = []
    if gnucash_dir.is_dir():
        for p in sorted(gnucash_dir.iterdir(), key=lambda q: q.name.lower()):
            if not p.is_file() or _norm(p) == final:
                continue
            if BACKUP_RE.match(p.name):
                items.append(_item(p, "backup"))
            elif LOG_RE.match(p.name):
                items.append(_item(p, "log"))
    lnk = drive_dir / (final_book.name + ".lnk")
    if drive_dir.is_dir() and lnk.is_file() and _norm(lnk) != final:
        items.append(_item(lnk, "shortcut"))
    return items


def script_safe(item: CleanupItem) -> bool:
    """Only a plain absolute ASCII path with no wildcard goes into the script."""
    p = item.path
    return (p.isascii() and (PureWindowsPath(p).is_absolute() or PurePosixPath(p).is_absolute())
            and not any(c in p for c in "*?\r\n\t\"`$"))


def build_script(items: list[CleanupItem]) -> str:
    """Self-contained ASCII PowerShell: one line per item, recycle only."""
    lines = [
        "# Year-end clean-up. Sends the listed files to the Recycle Bin.",
        "# Nothing here is permanent. Safe to run twice.",
        "Add-Type -AssemblyName Microsoft.VisualBasic",
    ]
    for it in items:
        if not script_safe(it):
            continue
        q = it.path.replace("'", "''")
        lines.append(
            f"$p = '{q}'; if (Test-Path -LiteralPath $p -PathType Leaf) {{ try {{ "
            "[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p, 'OnlyErrorDialogs', 'SendToRecycleBin'); "
            "Write-Host \"Recycled: $p\" } catch { Write-Host \"FAILED: $p\" } } "
            "else { Write-Host \"Skipped (not there): $p\" }")
    return "\r\n".join(lines) + "\r\n"
