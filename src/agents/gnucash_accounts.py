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
    accounts = list(accounts)
    by_id = {a.id: a for a in accounts}

    def _under_hidden(a: GncAccount) -> bool:
        # IMP-09 ancestor rule: an account under a hidden parent is not
        # postable either (a placeholder parent, by contrast, is fine).
        cur, seen = by_id.get(a.parent_id), set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            if "hidden" in cur.special_flags:
                return True
            cur = by_id.get(cur.parent_id)
        return False

    return [a for a in accounts
            if not a.is_root and not a.is_special and not _under_hidden(a)]


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


# ---------------------------------------------------------------------------
# IMP-09: the shared final "never a target" guard
# ---------------------------------------------------------------------------
# Only the Hidden and Placeholder flags are a hard block. ``is_special`` also
# covers tax-related / auto-interest-transfer / opening-balance, which are NOT
# a block (those accounts may legitimately receive postings). Hidden is
# inherited: an account under a hidden ancestor is blocked. A Placeholder
# parent does NOT block its children (GnuCash only forbids posting to the
# placeholder itself).
BLOCKING_FLAGS = ("hidden", "placeholder")

# The MatchReason prefix a blocked mapping carries into Review.
BLOCKED_PREFIX = "blocked: "


def _fy_start_year(date_text: str) -> Union[int, None]:
    """Indian FY (Apr-Mar) start year for a 'YYYY-MM-DD...' date string."""
    try:
        y, m = int(date_text[0:4]), int(date_text[5:7])
    except (ValueError, IndexError):
        return None
    return y if m >= 4 else y - 1


def fy_start_year_of(iso_date: str) -> Union[int, None]:
    """Public helper: FY start year for an ISO-ish date (first 10 chars used)."""
    return _fy_start_year((iso_date or "").strip())


def read_account_activity(gnucash_path: Union[str, Path]):
    """Return ``(fy_years, balance)`` per account id, read from the book's
    transactions: the set of FY start years in which the account has a split,
    and the net balance (as a Fraction) over all time. ``({}, {})`` if the
    book cannot be read. Hidden accounts are included (nothing is dropped)."""
    from fractions import Fraction
    root = _read_root(gnucash_path)
    if root is None:
        return {}, {}
    years: dict[str, set] = {}
    bal: dict[str, "Fraction"] = {}
    for trn in root.iter(f"{{{_GNC}}}transaction"):
        posted = None
        splits = []
        for ch in trn:
            nm = _local(ch.tag)
            if nm == "date-posted":
                for d in ch:
                    if _local(d.tag) == "date":
                        posted = (d.text or "").strip()
            elif nm == "splits":
                splits = [s for s in ch if _local(s.tag) == "split"]
        fy = _fy_start_year(posted or "")
        for sp in splits:
            acct = val = None
            for f in sp:
                n = _local(f.tag)
                if n == "account":
                    acct = (f.text or "").strip()
                elif n == "value":
                    val = (f.text or "").strip()
            if not acct:
                continue
            if fy is not None:
                years.setdefault(acct, set()).add(fy)
            try:
                bal[acct] = bal.get(acct, Fraction(0)) + Fraction(val or "0")
            except (ValueError, ZeroDivisionError):
                pass
    return years, bal


class TargetGuard:
    """Built ONCE per run from the book; the LAST step before output for every
    skill that emits a posting target asks it ``blocked_target_reason(path)``.

    ``dormant_reason(path, fy)`` is advisory only: an account that merely LOOKS
    dormant (no splits in the statement FY or the prior FY, zero balance) is
    still mapped -- callers highlight it, they never send it to Suspense.
    """

    def __init__(self, accounts: Iterable[GncAccount],
                 fy_years: Union[dict, None] = None,
                 balances: Union[dict, None] = None):
        accs = [a for a in accounts if not a.is_root]
        self._by_path = {a.path: a for a in accs if a.path}
        self._by_id = {a.id: a for a in accs}
        self._all_by_id = {a.id: a for a in accounts}
        self._years = fy_years or {}
        self._bal = balances or {}
        self._cache: dict[str, Union[str, None]] = {}

    @classmethod
    def from_book(cls, gnucash_path: Union[str, Path]) -> "TargetGuard":
        accs = load_accounts(gnucash_path)
        years, bal = read_account_activity(gnucash_path)
        return cls(accs, years, bal)

    @property
    def known(self) -> bool:
        return bool(self._by_path)

    def has_path(self, path: str) -> bool:
        return _strip_root(path or "") in self._by_path

    def blocked_target_reason(self, path: str) -> Union[str, None]:
        """Why ``path`` may not receive a posting, or None if it may (also
        None for a path the book does not know -- the guard never invents a
        block)."""
        key = _strip_root((path or "").strip())
        if key in self._cache:
            return self._cache[key]
        acc = self._by_path.get(key)
        reason = None
        if acc is not None:
            if "hidden" in acc.special_flags:
                reason = "target is hidden in the book"
            elif "placeholder" in acc.special_flags:
                reason = "target is a placeholder account in the book"
            else:
                seen = set()
                cur = self._all_by_id.get(acc.parent_id)
                while cur is not None and cur.id not in seen:
                    seen.add(cur.id)
                    if "hidden" in cur.special_flags:
                        reason = (f"target's parent '{cur.name}' "
                                  "is hidden in the book")
                        break
                    cur = self._all_by_id.get(cur.parent_id)
        self._cache[key] = reason
        return reason

    def is_blocked(self, path: str) -> bool:
        return self.blocked_target_reason(path) is not None

    def dormant_reason(self, path: str, fy_start_year: Union[int, None]):
        """Advisory: no splits in FY ``fy_start_year`` or the prior FY and a
        zero balance. None when active, unknown, or the FY is unknown."""
        if fy_start_year is None:
            return None
        acc = self._by_path.get(_strip_root((path or "").strip()))
        if acc is None:
            return None
        yrs = self._years.get(acc.id, set())
        if fy_start_year in yrs or (fy_start_year - 1) in yrs:
            return None
        if self._bal.get(acc.id, 0) != 0:
            return None
        return "no activity in this or the prior financial year, zero balance"
