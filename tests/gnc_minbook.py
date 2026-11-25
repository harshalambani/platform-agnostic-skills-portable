"""A minimal VALID synthetic GnuCash book for tests that only need the book to
exist and be readable (GNC-01: a book that cannot be read stops a run).

The book holds one gnc:book element, a root account and one bank account, and
no transactions. It is gzip XML, the format GnuCash writes by default.
"""
from __future__ import annotations

import gzip
from pathlib import Path

_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<gnc-v2 xmlns:gnc="http://www.gnucash.org/XML/gnc" '
    'xmlns:act="http://www.gnucash.org/XML/act">\n'
    '<gnc:count-data xmlns:cd="http://www.gnucash.org/XML/cd" cd:type="book">1</gnc:count-data>\n'
    '<gnc:book version="2.0.0">\n'
    '<gnc:account version="2.0.0"><act:name>Root Account</act:name>'
    '<act:id type="guid">root</act:id><act:type>ROOT</act:type></gnc:account>\n'
    '<gnc:account version="2.0.0"><act:name>Synthetic Bank</act:name>'
    '<act:id type="guid">bank1</act:id><act:type>BANK</act:type>'
    '<act:parent type="guid">root</act:parent></gnc:account>\n'
    '</gnc:book>\n</gnc-v2>\n'
)


def write_min_book(path) -> Path:
    """Write the minimal valid book at `path` (gzip) and return the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(p, "wt", encoding="utf-8", newline="\n") as f:
        f.write(_XML)
    return p
