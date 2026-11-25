"""One shared opener for GnuCash XML books (GNC-01, PR 1).

Stdlib only, so the stand-alone scripts can import it the same way they import
``book_check``.

``read_xml_root(path)`` opens a .gnucash book that is either gzip-compressed or
plain XML (it sniffs the gzip magic bytes and never trusts the extension),
parses it once, and caches the parsed root keyed on (absolute path, mtime_ns,
size) so a book is parsed once per run. A changed file is re-read.

A book that cannot be read raises ``GncBookUnreadable`` -- one exception type,
one clear message. It never returns None or an empty root.

The cached tree is shared: callers must treat it as read-only.
"""
from __future__ import annotations

import gzip
import os
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Union

_GNC_BOOK_TAG = "{http://www.gnucash.org/XML/gnc}book"
_GZIP_MAGIC = b"\x1f\x8b"

_cache: dict[tuple[str, int, int], ET.Element] = {}


class GncBookUnreadable(Exception):
    """The file is missing, empty, damaged, or not a GnuCash XML book."""

    def __init__(self, path: Union[str, Path], reason: str) -> None:
        self.path = str(path)
        self.reason = reason
        super().__init__(f"GnuCash book could not be read: {self.path} ({reason})")


def clear_cache() -> None:
    """Forget every cached book (for tests)."""
    _cache.clear()


def read_xml_root(path: Union[str, Path]) -> ET.Element:
    """Return the parsed root of a GnuCash XML book (gzip or plain)."""
    p = Path(path)
    try:
        abs_path = os.path.abspath(p)
        st = os.stat(abs_path)
    except OSError as exc:
        raise GncBookUnreadable(p, f"cannot open: {type(exc).__name__}") from exc

    key = (abs_path, st.st_mtime_ns, st.st_size)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    try:
        raw = Path(abs_path).read_bytes()
    except OSError as exc:
        raise GncBookUnreadable(p, f"cannot read: {type(exc).__name__}") from exc
    if not raw:
        raise GncBookUnreadable(p, "file is empty")

    if raw[:2] == _GZIP_MAGIC:
        try:
            data = gzip.decompress(raw)
        except (OSError, EOFError, zlib.error) as exc:
            raise GncBookUnreadable(p, "damaged or truncated gzip data") from exc
    else:
        data = raw

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise GncBookUnreadable(p, "not valid XML") from exc

    if root.find(_GNC_BOOK_TAG) is None:
        raise GncBookUnreadable(p, "no GnuCash book found in the XML")

    # Drop stale entries for the same path, then remember this one.
    for old in [k for k in _cache if k[0] == abs_path]:
        del _cache[old]
    _cache[key] = root
    return root
