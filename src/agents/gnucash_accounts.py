"""
gnucash_accounts.py — shared, placeholder/hidden-aware GnuCash account reader.

GnuCash marks certain accounts as "special account types" via KVP *slots* on
the account element. Such accounts are NOT valid posting targets and must never
be offered as an auto-match candidate or a user-pickable account:

  * placeholder            — a header / grouping account; GnuCash forbids
                             posting splits to it directly.
  * hidden                 — retired / inactive; should not receive new entries.
  * tax-related            — flagged for tax reports (forward-guard).
  * auto-interest-transfer — scheduled-interest helper account (forward-guard).
  * opening-balance        — the Equity opening-balances account. Encoded
                             differently: an ``equity-type`` *string* slot, not
                             a boolean.

In the real books shipped/used with this app only ``placeholder`` and
``hidden`` are ever set (both as ``<slot:value type="string">true</slot:value>``);
the other three are supported so a future book that sets them is handled
without a code change. An unknown/never-set flag simply never matches, so the
forward-guards are harmless if a key name is imperfect.

Nothing here calls the network or an LLM — it is a pure XML reader and is
therefore cheaply unit-testable.

The 26AS journal builder (``skill_26as_journal/scripts/build_tds_journals.py``)
runs as a stand-alone subprocess and keeps its OWN self-contained copy of this
flag logic (it cannot rely on ``agents`` being importable in a frozen child
process). A drift-guard test asserts that copy's flag-key set stays identical
to :data:`BOOL_FLAG_KEYS` here.
"""
from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Union

# GnuCash XML namespaces (URIs are stable across GnuCash versions).
_GNC = "http://www.gnucash.org/XML/gnc"
_ACT = "http://www.gnucash.org/XML/act"

# Boolean 'true'/'false' KVP slot keys that mark an account as a non-postable
# "special type". ``placeholder`` + ``hidden`` are CONFIRMED against real books;
# ``tax-related`` + ``auto-interest-transfer`` are forward-guards (never observed
# in data — a miss is harmless). Keep this in sync with the copy in
# build_tds_journals.py (enforced by test_gnucash_accounts.py).
BOOL_FLAG_KEYS = ("placeholder", "hidden", "tax-related", "auto-interest-transfer")

# Values (case-insensitive) that count as "flag is set" for a boolean slot.
_TRUE_VALUES = frozenset({"true", "t", "1", "yes", "y"})

# 'Opening balance' is NOT a boolean slot — GnuCash stores it as an
# ``equity-type`` string slot whose value is ``opening-balance``.
_EQUITY_TYPE_KEY = "equity-type"
_OPENING_BALANCE_VALUE = "opening-balance"

# The canonical set of special-flag names an account may carry (used by tests
# and callers that want to reason about *why* an account was excluded).
SPECIAL_FLAG_NAMES = tuple(BOOL_FLAG_KEYS) + (_OPENING_BALANCE_VALUE,)

_ROOT_PREFIX = "Root Account:"


@dataclass(frozen=True)
class GncAccount:
    """One account in the book, with its special-type flags resolved."""

    id: str
    name: str
    type: str                 # ROOT, ASSET, INCOME, EXPENSE, EQUITY, ...
    parent_id: Union[str, None]
    path: str                 # full colon path WITHOUT the 'Root Account:' prefix
    special_flags: frozenset  # subset of SPECIAL_FLAG_NAMES that are set

    @property
    def is_special(self) -> bool:
        """True if the account carries any special-type flag (placeholder,
        hidden, tax-related, auto-interest-transfer, opening-balance) and is
        therefore not a valid posting target."""
        return bool(self.special_flags)

    @property
    def is_root(self) -> bool:
        return self.type == "ROOT" or not self.path

    @property
    def leaf(self) -> str:
        return self.path.rsplit(":", 1)[-1] if ":" in self.path else self.path


def _local(tag: str) -> str:
    """Strip an ElementTree ``{namespace}local`` tag down to ``local``.

    GnuCash slot XML mixes namespaced (``slot:key``) and un-prefixed (``slot``)
    elements; matching on the local name is bulletproof against that quirk.
    """
    return tag.rsplit("}", 1)[-1]


def _account_flags(acc_el: ET.Element) -> frozenset:
    """Return the set of special-flag names set on a ``<gnc:account>`` element.

    Only the DIRECT children of ``<act:slots>`` are inspected — boolean flags
    live at the top level; nested ``frame`` slots (e.g. import-map noise) are
    intentionally not recursed into.
    """
    slots = acc_el.find(f"{{{_ACT}}}slots")
    if slots is None:
        return frozenset()
    flags = set()
    for slot in list(slots):                     # each <slot> container
        key = value = ""
        for child in slot:
            name = _local(child.tag)
            if name == "key":
                key = (child.text or "").strip()
            elif name == "value":
                value = (child.text or "").strip()
        if key in BOOL_FLAG_KEYS:
            if value.lower() in _TRUE_VALUES:
                flags.add(key)
        elif key == _EQUITY_TYPE_KEY and value.lower() == _OPENING_BALANCE_VALUE:
            flags.add(_OPENING_BALANCE_VALUE)
    return frozenset(flags)


def _read_root(gnucash_path: Union[str, Path]) -> Union[ET.Element, None]:
    """Parse a .gnucash file (gzipped or plain XML) and return its root, or
    None if it can't be read."""
    try:
        raw = Path(gnucash_path).read_bytes()
        data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        return ET.fromstring(data)
    except Exception:
        return None


def load_accounts(gnucash_path: Union[str, Path]) -> list[GncAccount]:
    """Read EVERY account in the book (placeholders/hidden included) with its
    special-type flags resolved. Returns [] if the file can't be read.

    Callers that want only valid posting targets should pass the result through
    :func:`postable_accounts` (or use :func:`read_postable_paths`). The full
    list is returned here because *existence* checks ("does this account already
    exist?") must still see placeholders — only *candidacy* is gated on flags.
    """
    root = _read_root(gnucash_path)
    if root is None:
        return []

    ns = {"act": _ACT}
    raw: dict[str, dict] = {}
    for a in root.iter(f"{{{_GNC}}}account"):
        nm = a.find("act:name", ns)
        idv = a.find("act:id", ns)
        if nm is None or idv is None:
            continue
        typ = a.find("act:type", ns)
        par = a.find("act:parent", ns)
        raw[idv.text] = {
            "name": nm.text or "",
            "type": (typ.text if typ is not None else "") or "",
            "parent": par.text if par is not None else None,
            "flags": _account_flags(a),
        }

    def full_path(aid: str) -> str:
        parts: list[str] = []
        cur, seen = aid, set()
        while cur in raw and cur not in seen:
            seen.add(cur)
            parts.append(raw[cur]["name"])
            cur = raw[cur]["parent"]
        parts = list(reversed(parts))
        if parts and parts[0].lower().startswith("root"):
            parts = parts[1:]
        return ":".join(parts)

    out = []
    for aid, info in raw.items():
        out.append(GncAccount(
            id=aid,
            name=info["name"],
            type=info["type"],
            parent_id=info["parent"],
            path=full_path(aid),
            special_flags=info["flags"],
        ))
    return out


def postable_accounts(accounts: Iterable[GncAccount]) -> list[GncAccount]:
    """The subset of ``accounts`` that are valid posting targets: not the root,
    and not carrying any special-type flag."""
    return [a for a in accounts if not a.is_root and not a.is_special]


def _strip_root(path: str) -> str:
    return path[len(_ROOT_PREFIX):] if path.startswith(_ROOT_PREFIX) else path


def read_postable_paths(gnucash_path: Union[str, Path]) -> set[str]:
    """Set of full paths (without 'Root Account:') for accounts that ARE valid
    posting targets — i.e. every account minus root/placeholder/hidden/etc."""
    return {a.path for a in postable_accounts(load_accounts(gnucash_path)) if a.path}


def read_special_paths(gnucash_path: Union[str, Path]) -> set[str]:
    """Set of full paths (without 'Root Account:') for accounts that carry a
    special-type flag and must NOT be offered as a posting target. Useful for
    subtracting from a candidate set learned elsewhere (e.g. from history)."""
    return {a.path for a in load_accounts(gnucash_path) if a.is_special and a.path}
