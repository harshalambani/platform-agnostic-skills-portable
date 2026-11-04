"""CC-39 / CC-29: pure helpers for the per-card sub-accounts and the pool tie-out.

The pool is the Credit Card Payment account (the parent) plus every account below it. Splitting
a card moves money INSIDE the pool, so the pool total must be exactly what the parent alone
showed before the split. Nothing here reads or writes a file.
"""
from __future__ import annotations

import re

from . import journal as J

TOL = 0.005


def in_pool(path: str, ccp_path: str) -> bool:
    return path == ccp_path or path.startswith(ccp_path + ":")


def pool_amount(txn, ccp_path: str) -> float:
    """What a book transaction does to the pool (parent plus sub-accounts), debit positive."""
    return round(sum(v for p, v in txn.splits if in_pool(p, ccp_path)), 2)


def parse_marks(text: str) -> list:
    """The pasted prepayment codes, split on spaces, commas and semicolons, lower-cased and
    de-duplicated in order. Validity is checked by valid_code; nothing is guessed."""
    out: list = []
    for tok in re.split(r"[\s,;]+", (text or "").strip().lower()):
        if tok and tok not in out:
            out.append(tok)
    return out


def valid_code(tok: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{8}", tok))


def pool_tie(counted: list, ccp_path: str) -> dict:
    """The tie of this run's pool effect. `counted` are the journals that change the book.

    new_total  what the book will show: every pool leg of every journal, parent and subs together
    old_total  what the parent alone would have shown with no split (each journal's unsplit effect)
    gap        new_total - old_total; anything beyond a hair means a journal left the pool or a
               row was double counted, and the import must not be used."""
    parent = 0.0
    subs: dict = {}
    for j in counted:
        for path, v in j.pool_legs(ccp_path).items():
            if path == ccp_path:
                parent += v
            else:
                subs[path] = subs.get(path, 0.0) + v
    parent = round(parent, 2)
    subs = {k: round(v, 2) for k, v in sorted(subs.items())}
    new_total = round(parent + sum(subs.values()), 2)
    old_total = round(sum(j.ccp_effect for j in counted), 2)
    return {"parent_effect": parent, "subs_effect": subs, "new_total": new_total, "old_total": old_total,
            "gap": round(new_total - old_total, 2), "ok": abs(new_total - old_total) <= TOL}


def moves_problem(records: list) -> str:
    """'' when, for every split statement, the moved amounts equal the bank payments that settled it."""
    for rec in records:
        moved = round(sum(m.amount for m in rec["moves"]), 2)
        paid = round(sum(p.amount for p in rec["payments"]), 2)
        if abs(moved - paid) > TOL:
            return (f"{rec['card']} {rec['statement']}: the moves to {rec['sub']} total {moved:,.2f} "
                    f"but the bank payments total {paid:,.2f}")
        if any(m.kind != J.K_MOVE for m in rec["moves"]):
            return f"{rec['card']} {rec['statement']}: a row other than a move was recorded as a move"
    return ""
