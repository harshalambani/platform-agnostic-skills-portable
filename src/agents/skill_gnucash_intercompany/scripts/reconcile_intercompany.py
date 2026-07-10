#!/usr/bin/env python3
"""
reconcile_intercompany.py -- Intercompany (inter-person) reconciliation between
two GnuCash books. DIRECT mode, deterministic, no LLM.

Given two .gnucash files (e.g. Vaikunth <-> Kiran), it:

  1. Derives each book's OWNER entity from the filename (VaikunthAmbani2526 ->
     "Vaikunth Ambani", FY 2025-26). "X HUF" is treated as a DISTINCT entity
     from "X".
  2. Finds the CONTRA accounts in each book that refer to the OTHER book's owner
     -- all purpose-accounts for that entity (e.g. "Kiran Ambani" AND
     "Rent receivable -Kiran Ambani"), while excluding a same-surname HUF.
  3. Computes an opening balance carried forward (net of all splits dated before
     the FY start) per side.
  4. Matches the FY movements between the two books: equal-and-opposite amount,
     within a date tolerance, with bank account-number / name tokens used as a
     tie-breaker.
  5. For every unmatched exception, hunts the OTHER book's ENTIRE account tree
     for a same-amount entry within the date window (bank/cash accounts ranked
     first) and surfaces it as a probable mis-posting.
  6. Writes an Excel workbook: Summary, Matched, Exceptions-A, Exceptions-B.

Run standalone:
    python reconcile_intercompany.py BOOK_A BOOK_B OUT.xlsx \
        [--period "FY 2025-26" | --start YYYY-MM-DD --end YYYY-MM-DD] [--tol 7]

Exit codes: 0 = clean tie, 2 = completed with exceptions/difference, 1 = error.
"""
from __future__ import annotations

import argparse
import gzip
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

# --------------------------------------------------------------------------- #
# GnuCash XML namespaces (element tags are fully-qualified in ElementTree).
# NOTE: the transaction container element is gnc:transaction, NOT trn:...
# --------------------------------------------------------------------------- #
GNC = "{http://www.gnucash.org/XML/gnc}"
ACT = "{http://www.gnucash.org/XML/act}"
TRN = "{http://www.gnucash.org/XML/trn}"
SPLIT = "{http://www.gnucash.org/XML/split}"
TS = "{http://www.gnucash.org/XML/ts}"

# Account types whose splits most often catch a mis-posted counterparty payment.
LIQUID_TYPES = {"BANK", "CASH", "CREDIT"}


# --------------------------------------------------------------------------- #
# Data model.
# --------------------------------------------------------------------------- #
@dataclass
class Account:
    guid: str
    name: str
    atype: str
    parent: Optional[str]


@dataclass
class Movement:
    """One split hitting a target account (a contra account, or any account)."""
    d: date
    amount: float          # signed, as stored in the book
    desc: str              # transaction description
    memo: str
    account_name: str      # the account this split posts to
    account_path: str
    other_accounts: list[str]              # account name(s) on the other legs
    tokens: set[str] = field(default_factory=set)  # match keys (acct #s, names)
    matched: bool = False


@dataclass
class Book:
    path: Path
    owner: str                                 # derived entity, e.g. "Kiran Ambani"
    fy_from_name: Optional[tuple[int, int]]    # (2025, 2026) if detectable
    accounts: dict[str, Account]               # guid -> Account
    root: ET.Element


# --------------------------------------------------------------------------- #
# Owner / FY derivation from filename.
# --------------------------------------------------------------------------- #
def _split_camel(s: str) -> str:
    """VaikunthAmbaniHUF -> 'Vaikunth Ambani HUF'."""
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def derive_owner_and_fy(path: Path) -> tuple[str, Optional[tuple[int, int]]]:
    """
    'VaikunthAmbani2526.gnucash'     -> ('Vaikunth Ambani', (2025, 2026))
    'VaikunthAmbaniHUF2526.gnucash'  -> ('Vaikunth Ambani HUF', (2025, 2026))
    """
    stem = path.stem
    fy = None
    m = re.search(r"(\d{2})(\d{2})$", stem)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b == (a + 1) % 100:
            fy = (2000 + a, 2000 + b)
        stem = stem[: m.start()]
    stem = re.sub(r"\d{4}$", "", stem)      # strip any lone trailing year
    owner = _split_camel(stem)
    owner = re.sub(r"\bhuf\b", "HUF", owner, flags=re.I)
    return owner.strip(), fy


# --------------------------------------------------------------------------- #
# Parse a .gnucash file into a Book (accounts) + keep root for splits.
# --------------------------------------------------------------------------- #
def load_book(path: str) -> Book:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    lck = p.with_name(p.name + ".LCK")
    if lck.exists():
        print(f"WARNING: {p.name}.LCK present -- book may be open in GnuCash. "
              f"Reading anyway (read-only).", file=sys.stderr)
    with gzip.open(p, "rt", encoding="utf-8") as f:
        root = ET.parse(f).getroot()

    accounts: dict[str, Account] = {}
    for a in root.iter(f"{GNC}account"):
        gid = a.find(f"{ACT}id")
        nm = a.find(f"{ACT}name")
        ty = a.find(f"{ACT}type")
        pa = a.find(f"{ACT}parent")
        if gid is None or nm is None:
            continue
        accounts[gid.text] = Account(
            guid=gid.text,
            name=nm.text or "",
            atype=ty.text if ty is not None else "",
            parent=pa.text if pa is not None else None,
        )
    owner, fy = derive_owner_and_fy(p)
    return Book(path=p, owner=owner, fy_from_name=fy, accounts=accounts, root=root)


def account_path(book: Book, guid: str) -> str:
    parts, seen = [], set()
    cur = guid
    while cur and cur in book.accounts and cur not in seen:
        seen.add(cur)
        acc = book.accounts[cur]
        if acc.atype == "ROOT":
            break
        parts.append(acc.name)
        cur = acc.parent
    return ":".join(reversed(parts))


# --------------------------------------------------------------------------- #
# Entity-aware contra account detection.
# --------------------------------------------------------------------------- #
_STOP = {"ambani", "the", "a"}   # surname alone is not distinguishing


def _core_tokens(entity: str) -> tuple[set[str], bool]:
    """'Vaikunth Ambani HUF' -> ({'vaikunth','ambani'}, is_huf=True)."""
    toks = re.findall(r"[a-z0-9]+", entity.lower())
    is_huf = "huf" in toks
    core = {t for t in toks if t != "huf"}
    return core, is_huf


def find_contra_accounts(book: Book, other_owner: str) -> list[Account]:
    """
    All accounts in `book` that refer to `other_owner` as an entity.
    Requires every core name token of the owner to appear in the account name,
    and matches the HUF flag exactly (so 'X HUF' != 'X').
    """
    core, want_huf = _core_tokens(other_owner)
    distinguishing = core - _STOP or core
    hits = []
    for acc in book.accounts.values():
        if acc.atype in ("ROOT", ""):
            continue
        name_toks = set(re.findall(r"[a-z0-9]+", acc.name.lower()))
        if not core.issubset(name_toks):
            continue
        if not (distinguishing & name_toks):
            continue
        if ("huf" in name_toks) != want_huf:
            continue
        hits.append(acc)
    return hits


def _descendants(book: Book, guids: set[str]) -> set[str]:
    children: dict[str, list[str]] = {}
    for g, acc in book.accounts.items():
        if acc.parent:
            children.setdefault(acc.parent, []).append(g)
    out, stack = set(), list(guids)
    while stack:
        g = stack.pop()
        if g in out:
            continue
        out.add(g)
        stack.extend(children.get(g, []))
    return out


# --------------------------------------------------------------------------- #
# Token extraction for match tie-breaking / hunt ranking.
# --------------------------------------------------------------------------- #
_TOKEN_NOISE = {"bank", "hdfc", "icici", "from", "vijaya", "coop", "account"}


def _tokens_from(*texts: str) -> set[str]:
    toks: set[str] = set()
    for t in texts:
        if not t:
            continue
        low = t.lower()
        toks |= set(re.findall(r"\d{6,}", low))                 # acct / ref #s
        toks |= {w for w in re.findall(r"[a-z]{4,}", low)
                 if w not in _STOP and w not in _TOKEN_NOISE}
    return toks


# --------------------------------------------------------------------------- #
# Extract movements from a set of accounts within an optional date range.
# --------------------------------------------------------------------------- #
def _parse_amount(value_text: str) -> Optional[float]:
    v = (value_text or "").strip()
    try:
        if "/" in v:
            n, d = v.split("/")
            return int(n) / int(d)
        return float(v.replace(",", ""))
    except (ValueError, ZeroDivisionError):
        return None


def extract_movements(book: Book, target_guids: set[str],
                      lo: Optional[date] = None,
                      hi: Optional[date] = None) -> list[Movement]:
    moves: list[Movement] = []
    for tx in book.root.iter(f"{GNC}transaction"):
        dp = tx.find(f"{TRN}date-posted/{TS}date")
        if dp is None or not dp.text:
            continue
        try:
            d = datetime.strptime(dp.text.strip().split()[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        if lo and d < lo:
            continue
        if hi and d > hi:
            continue
        desc_el = tx.find(f"{TRN}description")
        desc = desc_el.text if desc_el is not None and desc_el.text else ""
        splits = list(tx.iter(f"{TRN}split"))
        for s in splits:
            ael = s.find(f"{SPLIT}account")
            if ael is None or ael.text not in target_guids:
                continue
            amt = _parse_amount(s.findtext(f"{SPLIT}value", ""))
            if amt is None:
                continue
            memo_el = s.find(f"{SPLIT}memo")
            memo = memo_el.text if memo_el is not None and memo_el.text else ""
            others = [book.accounts.get(o.find(f"{SPLIT}account").text,
                                        Account("", "?", "", None)).name
                      for o in splits if o is not s
                      and o.find(f"{SPLIT}account") is not None]
            mv = Movement(
                d=d, amount=amt, desc=desc, memo=memo,
                account_name=book.accounts[ael.text].name,
                account_path=account_path(book, ael.text),
                other_accounts=others,
            )
            mv.tokens = _tokens_from(desc, memo, *others)
            moves.append(mv)
    moves.sort(key=lambda m: (m.d, -abs(m.amount)))
    return moves


# --------------------------------------------------------------------------- #
# Matching.
# --------------------------------------------------------------------------- #
@dataclass
class Pair:
    a: Movement
    b: Movement
    day_gap: int
    basis: str


def match_movements(a_moves: list[Movement], b_moves: list[Movement],
                    tol_days: int) -> list[Pair]:
    """
    Greedy one-to-one. A movement in book A mirrors one in book B with the
    OPPOSITE sign. Preference: more shared tokens, then smaller date gap.
    """
    pairs: list[Pair] = []
    for a in a_moves:
        if a.matched:
            continue
        best, best_score = None, None
        for b in b_moves:
            if b.matched:
                continue
            if round(a.amount + b.amount, 2) != 0:     # equal & opposite
                continue
            gap = abs((a.d - b.d).days)
            if gap > tol_days:
                continue
            score = (len(a.tokens & b.tokens), -gap)
            if best_score is None or score > best_score:
                best, best_score = b, score
        if best is not None:
            best.matched = a.matched = True
            shared = len(a.tokens & best.tokens)
            gap = abs((a.d - best.d).days)
            if shared and gap == 0:
                basis = "amount + date + ref"
            elif shared:
                basis = "amount + ref token"
            elif gap == 0:
                basis = "amount + date"
            else:
                basis = f"amount + date (+/-{gap}d)"
            pairs.append(Pair(a=a, b=best, day_gap=gap, basis=basis))
    pairs.sort(key=lambda p: p.a.d)
    return pairs


# --------------------------------------------------------------------------- #
# Leftover hunt -- find a probable mis-posting in the OTHER book.
# --------------------------------------------------------------------------- #
@dataclass
class Suggestion:
    account_path: str
    d: date
    amount: float
    desc: str
    day_gap: int
    liquid: bool
    shared: int


def hunt_mispostings(exc: Movement, other_book: Book,
                     exclude_guids: set[str], tol_days: int,
                     max_out: int = 3) -> list[Suggestion]:
    """
    Search the ENTIRE other book for an entry that mirrors `exc` (opposite sign,
    same magnitude) within the date window. Rank bank/cash first, then shared
    tokens, then date proximity.
    """
    lo, hi = exc.d - timedelta(days=tol_days), exc.d + timedelta(days=tol_days)
    all_guids = set(other_book.accounts) - exclude_guids
    out: list[Suggestion] = []
    for c in extract_movements(other_book, all_guids, lo, hi):
        if round(exc.amount + c.amount, 2) != 0:
            continue
        acc = next((a for a in other_book.accounts.values()
                    if a.name == c.account_name), None)
        liquid = bool(acc and acc.atype in LIQUID_TYPES)
        out.append(Suggestion(
            account_path=c.account_path, d=c.d, amount=c.amount, desc=c.desc,
            day_gap=abs((exc.d - c.d).days), liquid=liquid,
            shared=len(exc.tokens & c.tokens),
        ))
    out.sort(key=lambda s: (not s.liquid, -s.shared, s.day_gap))
    return out[:max_out]


# --------------------------------------------------------------------------- #
# Period resolution.
# --------------------------------------------------------------------------- #
def resolve_period(period: str, start: str, end: str,
                   book_a: Book, book_b: Book) -> tuple[date, date, str]:
    """Return (fy_start, fy_end, label). Indian FY = 1 Apr -> 31 Mar."""
    period = (period or "").strip()
    if start and end:
        return (datetime.strptime(start, "%Y-%m-%d").date(),
                datetime.strptime(end, "%Y-%m-%d").date(),
                f"{start} to {end}")
    m = re.search(r"FY\s*(\d{4})[-/](\d{2,4})", period, re.I)
    if m:
        y1 = int(m.group(1))
        return date(y1, 4, 1), date(y1 + 1, 3, 31), f"FY {y1}-{str(y1 + 1)[-2:]}"
    m = re.search(r"Calendar\s*Year\s*(\d{4})", period, re.I)
    if m:
        y = int(m.group(1))
        return date(y, 1, 1), date(y, 12, 31), f"CY {y}"
    fy = book_a.fy_from_name or book_b.fy_from_name
    if fy:
        y1 = fy[0]
        return date(y1, 4, 1), date(y1 + 1, 3, 31), f"FY {y1}-{str(y1 + 1)[-2:]} (auto)"
    raise ValueError("Could not determine the reconciliation period. "
                     "Pass --period 'FY 2025-26' or --start/--end.")


# --------------------------------------------------------------------------- #
# Reconcile -- orchestration, returns a result dict.
# --------------------------------------------------------------------------- #
def reconcile(book_a_path: str, book_b_path: str,
              period: str = "", start: str = "", end: str = "",
              tol_days: int = 7) -> dict:
    a = load_book(book_a_path)
    b = load_book(book_b_path)
    fy_start, fy_end, fy_label = resolve_period(period, start, end, a, b)

    a_contra = find_contra_accounts(a, b.owner)
    b_contra = find_contra_accounts(b, a.owner)
    if not a_contra:
        raise ValueError(f"No contra account for '{b.owner}' found in {a.path.name}.")
    if not b_contra:
        raise ValueError(f"No contra account for '{a.owner}' found in {b.path.name}.")

    a_guids = _descendants(a, {x.guid for x in a_contra})
    b_guids = _descendants(b, {x.guid for x in b_contra})

    day_before = fy_start - timedelta(days=1)
    a_open = sum(m.amount for m in extract_movements(a, a_guids, hi=day_before))
    b_open = sum(m.amount for m in extract_movements(b, b_guids, hi=day_before))

    a_moves = extract_movements(a, a_guids, fy_start, fy_end)
    b_moves = extract_movements(b, b_guids, fy_start, fy_end)

    pairs = match_movements(a_moves, b_moves, tol_days)

    a_exc = [m for m in a_moves if not m.matched]
    b_exc = [m for m in b_moves if not m.matched]

    a_suggestions = {id(m): hunt_mispostings(m, b, b_guids, tol_days) for m in a_exc}
    b_suggestions = {id(m): hunt_mispostings(m, a, a_guids, tol_days) for m in b_exc}

    a_move_sum = sum(m.amount for m in a_moves)
    b_move_sum = sum(m.amount for m in b_moves)
    a_close = a_open + a_move_sum
    b_close = b_open + b_move_sum
    # A proper intercompany balance is equal & opposite: closing_A == -closing_B.
    difference = round(a_close + b_close, 2)

    return {
        "book_a": a, "book_b": b,
        "fy_start": fy_start, "fy_end": fy_end, "fy_label": fy_label,
        "tol_days": tol_days,
        "a_contra": a_contra, "b_contra": b_contra,
        "a_open": a_open, "b_open": b_open,
        "a_move_sum": a_move_sum, "b_move_sum": b_move_sum,
        "a_close": a_close, "b_close": b_close,
        "difference": difference,
        "pairs": pairs, "a_exc": a_exc, "b_exc": b_exc,
        "a_suggestions": a_suggestions, "b_suggestions": b_suggestions,
    }


# --------------------------------------------------------------------------- #
# CLI (Excel writing lives in excel_report.py; imported lazily).
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Intercompany GnuCash reconciliation")
    ap.add_argument("book_a")
    ap.add_argument("book_b")
    ap.add_argument("out_xlsx")
    ap.add_argument("--period", default="")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--tol", type=int, default=7)
    args = ap.parse_args(argv)

    try:
        result = reconcile(args.book_a, args.book_b, args.period,
                           args.start, args.end, args.tol)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    sys.path.insert(0, str(Path(__file__).parent))
    from excel_report import write_workbook
    write_workbook(result, args.out_xlsx)

    a, b = result["book_a"], result["book_b"]
    print(f"Reconciliation: {a.owner}  <->  {b.owner}   [{result['fy_label']}]")
    print(f"  contra in {a.path.name}: {', '.join(x.name for x in result['a_contra'])}")
    print(f"  contra in {b.path.name}: {', '.join(x.name for x in result['b_contra'])}")
    print(f"  matched pairs : {len(result['pairs'])}")
    print(f"  exceptions    : {len(result['a_exc'])} (A) / {len(result['b_exc'])} (B)")
    print(f"  opening b/f   : {a.owner} {result['a_open']:,.2f} | "
          f"{b.owner} {result['b_open']:,.2f}")
    print(f"  closing       : {a.owner} {result['a_close']:,.2f} | "
          f"{b.owner} {result['b_close']:,.2f}")
    print(f"  difference    : {result['difference']:,.2f} "
          f"({'TIES' if result['difference'] == 0 else 'OUT OF BALANCE'})")
    print(f"  workbook      : {args.out_xlsx}")

    clean = (result["difference"] == 0 and not result["a_exc"] and not result["b_exc"])
    return 0 if clean else 2


if __name__ == "__main__":
    sys.exit(main())
