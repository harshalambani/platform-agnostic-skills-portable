"""IMP-15: the shared "is this entry already in the book?" check (rule S.1 = B).

Every import skill that writes a journal or CSV asks this one module, so the
rule is written once. The module is pure and read-only: it is handed the
entries a run is about to write and the book's transactions (both as plain
data) and returns one verdict per entry. It never opens a book, never writes,
and never raises on a bad match: a book that could not be read is the
caller's `unchecked` verdict (see `unchecked_verdicts`).

The rule
  * DEFINITE  - same Num AND identical per-account amounts. Left OUT of the
                import file and listed with the reason.
  * MISMATCH  - same Num, different amounts. Left OUT, RED FLAG.
  * PROBABLE  - no Num hit, but inside the date window with the same amounts
                on the same accounts. STAYS IN, flagged "check before import".
  * POSSIBLE  - no Num hit, inside the date window with the same amounts but
                not provably the same accounts (opt-in). STAYS IN, flagged.
  * TIE       - several book transactions fit equally well (at the PROBABLE
                or POSSIBLE level). Never auto-picked: STAYS IN, all listed.
  * NOT_POSTED- nothing in the book resembles it. Written.
  * UNCHECKED - the book could not be read. Written, RED FLAG "NOT CHECKED".

One-to-one: a book transaction clears at most one entry. Num matches claim
first (across ALL entries), then the date-window fallback, nearest date first.

The user's design rule: report loud, never fail hard, never withhold silently.
Amounts are integer minor units (paise); callers convert at the boundary.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction

DEFINITE = "definite"
MISMATCH = "mismatch"
PROBABLE = "probable"
POSSIBLE = "possible"
TIE = "tie"
NOT_POSTED = "not_posted"
UNCHECKED = "unchecked"

ALL_VERDICTS = (DEFINITE, MISMATCH, PROBABLE, POSSIBLE, TIE, NOT_POSTED, UNCHECKED)
LEFT_OUT_VERDICTS = (DEFINITE, MISMATCH)          # not written to the import file
RED_FLAG_VERDICTS = (MISMATCH, UNCHECKED)         # shout at the top of the reply
CHECK_BEFORE_IMPORT = (PROBABLE, POSSIBLE, TIE)   # written, but flagged


@dataclass(frozen=True)
class BookTxn:
    """One book transaction. `legs` = (account, signed amount in minor units);
    `qty` = optional per-account share quantity (stock splits)."""
    id: str
    num: str
    date: date
    legs: tuple
    qty: tuple = ()          # ((account, Fraction), ...)


@dataclass(frozen=True)
class Entry:
    """One entry the run is about to write."""
    num: str
    date: date | None
    legs: tuple              # ((account, signed amount in minor units), ...)
    ignore: frozenset = frozenset()   # accounts left out of the comparison (both sides)
    qty: tuple = ()          # ((account, Fraction), ...) quantities that must also match
    subset: bool = False     # True: the book may hold MORE accounts than the entry


@dataclass(frozen=True)
class Options:
    """How strictly legs are compared and how wide the date window is.
    net_per_account=False compares the legs as a multiset (one split = one
    leg); True nets each account first (several splits on one account add up).
    window_days=0 is same-date only. possible_amount_only turns on POSSIBLE."""
    net_per_account: bool = False
    window_days: int = 0
    possible_amount_only: bool = False


@dataclass
class Verdict:
    num: str
    kind: str
    book: BookTxn | None = None            # the matched book transaction, if one
    candidates: tuple = ()                 # TIE: every equally good transaction
    tie_of: str = ""                       # TIE: PROBABLE or POSSIBLE level
    reason: str = ""
    detail: dict = field(default_factory=dict)


# ----------------------------------------------------------------- legs --

def _legs(legs, ignore, net: bool):
    kept = [(a, c) for a, c in legs if a not in ignore]
    if not net:
        return Counter((c, a) for a, c in kept)
    out: dict = {}
    for a, c in kept:
        out[a] = out.get(a, 0) + c
    return {a: c for a, c in out.items() if c != 0}


def _same_legs(entry: Entry, txn: BookTxn, opts: Options) -> bool:
    want = _legs(entry.legs, entry.ignore, opts.net_per_account)
    have = _legs(txn.legs, entry.ignore, opts.net_per_account)
    if opts.net_per_account and entry.subset:
        ok = all(have.get(a) == c for a, c in want.items())
    else:
        ok = want == have
    if not ok:
        return False
    if entry.qty:
        q = dict(txn.qty)
        if any(a not in q or q[a] != Fraction(v) for a, v in entry.qty):
            return False
    return True


def _amounts_only(entry: Entry, txn: BookTxn, opts: Options) -> bool:
    mine = sorted(c for a, c in entry.legs if a not in entry.ignore)
    theirs = sorted(c for a, c in txn.legs if a not in entry.ignore)
    return bool(mine) and mine == theirs


def _dist(entry: Entry, txn: BookTxn) -> int:
    return abs((txn.date - entry.date).days) if entry.date else 0


# -------------------------------------------------------------- checker --

class BookChecker:
    """Holds the book's transactions and which of them are already claimed.

    `reserved_nums` = Nums some entry of this run will claim; those book
    transactions are never taken by another entry's date fallback.
    `fallback_ids` = if given, only these book transaction ids may be matched
    by the date fallback (a Num match always looks at the whole book)."""

    def __init__(self, txns, reserved_nums=(), options: Options | None = None,
                 fallback_ids=None):
        self.txns = list(txns)
        self.opts = options or Options()
        self.reserved = set(reserved_nums)
        self.fallback_ids = None if fallback_ids is None else set(fallback_ids)
        self.claimed: set = set()          # indexes into self.txns

    # -- pass 1: Num ----------------------------------------------------
    def check_num(self, entry: Entry) -> Verdict | None:
        if not entry.num:
            return None
        cands = [i for i, t in enumerate(self.txns)
                 if i not in self.claimed and t.num == entry.num]
        if not cands:
            return None
        fit = [i for i in cands if _same_legs(entry, self.txns[i], self.opts)]
        no_legs = not _legs(entry.legs, entry.ignore, self.opts.net_per_account)
        if fit or no_legs:
            i = min(fit or cands, key=lambda k: (_dist(entry, self.txns[k]), k))
            self.claimed.add(i)
            return Verdict(entry.num, DEFINITE, self.txns[i],
                           reason=f"same Num {entry.num} and the same amounts")
        i = min(cands, key=lambda k: (_dist(entry, self.txns[k]), k))
        self.claimed.add(i)
        return Verdict(entry.num, MISMATCH, self.txns[i],
                       reason=f"Num {entry.num} is in the book with DIFFERENT amounts")

    # -- pass 2: date window + amounts -----------------------------------
    def check_fallback(self, entry: Entry) -> Verdict:
        if entry.date is None:
            return Verdict(entry.num, NOT_POSTED, reason="no date to match on")
        no_legs = not _legs(entry.legs, entry.ignore, self.opts.net_per_account)
        if no_legs:
            return Verdict(entry.num, NOT_POSTED, reason="nothing to compare")
        pool = [i for i, t in enumerate(self.txns)
                if i not in self.claimed and t.num not in self.reserved
                and (self.fallback_ids is None or t.id in self.fallback_ids)
                and abs((t.date - entry.date).days) <= self.opts.window_days]
        levels = [(PROBABLE, lambda t: _same_legs(entry, t, self.opts))]
        if self.opts.possible_amount_only:
            levels.append((POSSIBLE, lambda t: _amounts_only(entry, t, self.opts)))
        for kind, fits in levels:
            hit = [i for i in pool if fits(self.txns[i])]
            if not hit:
                continue
            best = min(_dist(entry, self.txns[i]) for i in hit)
            nearest = [i for i in hit if _dist(entry, self.txns[i]) == best]
            if len(nearest) == 1:
                self.claimed.add(nearest[0])
                return Verdict(entry.num, kind, self.txns[nearest[0]],
                               reason=("same date/window, amounts and accounts" if kind == PROBABLE
                                       else "same date/window and amounts"))
            return Verdict(entry.num, TIE, None,
                           candidates=tuple(self.txns[i] for i in nearest), tie_of=kind,
                           reason=f"{len(nearest)} book transactions fit equally well")
        return Verdict(entry.num, NOT_POSTED, reason="nothing in the book resembles it")

    def check(self, entry: Entry) -> Verdict:
        """One entry, Num first and then the fallback (incremental use)."""
        return self.check_num(entry) or self.check_fallback(entry)


def check_all(entries, txns, options: Options | None = None, fallback_ids=None,
              reserve: bool = True) -> list:
    """Verdicts in entry order. Num matches of ALL entries claim first, then
    the fallback runs in entry order, so an earlier look-alike can never steal
    a book transaction that a later entry owns by Num. `reserve=False` lets the
    fallback also take a book transaction whose Num belongs to this run (only
    needed to keep an older caller's behaviour exactly)."""
    entries = list(entries)
    chk = BookChecker(txns, {e.num for e in entries if e.num} if reserve else (),
                      options, fallback_ids)
    out: list = [None] * len(entries)
    for k, e in enumerate(entries):
        out[k] = chk.check_num(e)
    for k, e in enumerate(entries):
        if out[k] is None:
            out[k] = chk.check_fallback(e)
    return out


def unchecked_verdicts(entries, why: str) -> list:
    """The book could not be read: every entry is UNCHECKED (written, RED FLAG)."""
    return [Verdict(e.num, UNCHECKED, reason=why) for e in entries]


# ------------------------------------------------------ partition / text --

def partition(entries, verdicts) -> dict:
    """Split entries by what the check found. `write` is what goes into the
    import file; the other lists say why something is missing or flagged.
    Returns lists of (entry, verdict) pairs keyed by verdict kind plus
    `write` (entries only)."""
    out = {k: [] for k in ALL_VERDICTS}
    out["write"] = []
    for e, v in zip(entries, verdicts):
        out[v.kind].append((e, v))
        if v.kind not in LEFT_OUT_VERDICTS:
            out["write"].append(e)
    return out


def _dates(pairs):
    return ", ".join(f"{e.num} ({e.date.isoformat() if e.date else 'no date'})" for e, _ in pairs)


def report_lines(part: dict, not_checked_reason: str = "") -> tuple:
    """(red_flags, info_lines) in the standard wording. Num and date only -
    never amounts. RED FLAG lines go at the TOP of a reply."""
    flags, info = [], []
    if part[MISMATCH]:
        flags.append(
            f"RED FLAG: {_dates(part[MISMATCH])} is already in the book with DIFFERENT "
            "amounts from the entry this run computes. It was left out of the file; "
            "resolve it before posting anything for it.")
    if part[UNCHECKED]:
        flags.append(
            "RED FLAG: NOT CHECKED against the book"
            + (f" - {not_checked_reason}" if not_checked_reason else "")
            + " Every entry was written; any of them may already be in the book, "
            "so check before import.")
    if part[DEFINITE]:
        info.append(f"Already in the book (left out): {len(part[DEFINITE])} - "
                    f"{_dates(part[DEFINITE])}")
    maybe = part[PROBABLE] + part[POSSIBLE] + part[TIE]
    if maybe:
        info.append(f"Possibly already in the book - check before import: {len(maybe)} - "
                    f"{_dates(maybe)}")
    return flags, info
