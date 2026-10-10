"""Pure matching of book payments to statement payment lines, and settlement.

Cash basis. A payment line printed on statement k settles statement k-1 of the
same card (RF-2). Book payments are debit splits on the Credit Card Payment
account (CCP). Pairing is one-to-one and never guessed: a pair is accepted only
when the line has exactly one candidate payment and that payment has exactly
one candidate line. Everything else is reported as ambiguous, unmatched or
partly matched.

Named tolerances (decision 6.8):
  CRED_MAX_OVERAGE   a CRED payment line may exceed the bank amount by 0..15 rupees
  PAY_WINDOW_DAYS    a bank payment and its statement line are within +/-5 days
  PART_WINDOW_DAYS   the parts of a part payment are within 15 days of the line
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date

CRED_MAX_OVERAGE = 15.0
PAY_WINDOW_DAYS = 5
PART_WINDOW_DAYS = 15
PART_MIN_PARTS = 2
PART_MAX_PARTS = 4
PART_POOL_CAP = 14
AMOUNT_TOL = 0.01
SETTLE_TOL = 1.0          # a statement counts as settled when paid >= due - 1 rupee

_CRED_RX = re.compile(r"\bCRED\b", re.I)

# Why a book payment matched nothing (shown in the RED FLAG block).
R_NO_STATEMENT = "no statement for any card in the window"
R_AMOUNT_OFF = "amount off tolerance"
R_REFUSED = "the statement it settles was refused because it is not PASS"
R_BEFORE_RANGE = "settles a statement from before the range"
R_NO_PRIOR = "settles a statement that is not among the statements read"
R_AMBIGUOUS = "ambiguous: more than one possible match"
R_PARTLY = "part of a payment that does not cover the statement"


@dataclass(frozen=True)
class BookPayment:
    guid: str
    date: date
    amount: float
    description: str
    bank_account: str
    in_range: bool = True

    @property
    def cred(self) -> bool:
        return bool(_CRED_RX.search(self.description or ""))


@dataclass
class PayLine:
    stmt: object            # the Statement the line is printed on
    row: object             # the statement Row
    date: date
    amount: float

    @property
    def cred(self) -> bool:
        return bool(_CRED_RX.search(getattr(self.row, "description", "") or ""))


@dataclass
class Pairing:
    line: PayLine
    payments: tuple
    kind: str               # exact | cred | part
    overage: float = 0.0    # CRED: statement line minus the bank amount


@dataclass
class MatchOutcome:
    pairings: list = field(default_factory=list)
    ambiguous: list = field(default_factory=list)          # {"line", "payments", "reason"}
    lines_unmatched: list = field(default_factory=list)
    payments_unmatched: list = field(default_factory=list)
    unmatched_reason: dict = field(default_factory=dict)   # payment guid -> reason


def _day(d):
    return d.date() if hasattr(d, "hour") else d


def pay_lines(statements) -> list:
    """The payment lines (credits) printed on the given statements."""
    out = []
    for st in statements:
        for r in st.parsed.rows:
            if r.kind == "payment" and r.direction == "Cr" and r.date is not None:
                out.append(PayLine(st, r, _day(r.date), round(r.amount, 2)))
    return out


def single_fit(line: PayLine, pay: BookPayment):
    """'exact' | 'cred' | None for one line against one bank payment."""
    if abs((line.date - pay.date).days) > PAY_WINDOW_DAYS:
        return None
    diff = round(line.amount - pay.amount, 2)
    if abs(diff) <= AMOUNT_TOL:
        return "exact"
    if (line.cred or pay.cred) and 0 < diff <= CRED_MAX_OVERAGE + 1e-9:
        return "cred"
    return None


def _subset_fit(line: PayLine, parts):
    total = round(sum(p.amount for p in parts), 2)
    diff = round(line.amount - total, 2)
    if abs(diff) <= AMOUNT_TOL:
        return "exact"
    if (line.cred or any(p.cred for p in parts)) and 0 < diff <= CRED_MAX_OVERAGE + 1e-9:
        return "cred"
    return None


def match_payments(lines: list, payments: list) -> MatchOutcome:
    out = MatchOutcome()
    cand: dict = {i: [] for i in range(len(lines))}
    back: dict = {p.guid: [] for p in payments}
    for i, ln in enumerate(lines):
        for p in payments:
            if single_fit(ln, p):
                cand[i].append(p)
                back[p.guid].append(i)

    used: set = set()           # payment guids paired or involved in an ambiguity
    done_lines: set = set()
    for i, ln in enumerate(lines):
        cs = cand[i]
        if len(cs) == 1 and len(back[cs[0].guid]) == 1:
            p = cs[0]
            kind = single_fit(ln, p)
            out.pairings.append(Pairing(ln, (p,), kind,
                                        round(ln.amount - p.amount, 2) if kind == "cred" else 0.0))
            used.add(p.guid)
            done_lines.add(i)
        elif cs:
            out.ambiguous.append({"line": ln, "payments": list(cs),
                                  "reason": f"the statement line could be {len(cs)} bank payments"})
            used.update(p.guid for p in cs)
            done_lines.add(i)
    # a payment that fits several lines is ambiguous even when one of them had a single candidate
    paired_guids = {pp.guid for pr in out.pairings for pp in pr.payments}
    for p in payments:
        if len(back[p.guid]) > 1 and p.guid not in paired_guids:
            out.ambiguous.append({"line": None, "payments": [p],
                                  "reason": f"the bank payment could settle {len(back[p.guid])} statement lines"})
            used.add(p.guid)

    # Part payments: only lines with no single candidate, only unused payments,
    # and only when the solution is unique and shares no payment with another line.
    free = [p for p in payments if p.guid not in used]
    sol: dict = {}
    for i, ln in enumerate(lines):
        if i in done_lines:
            continue
        near = [p for p in free if abs((p.date - ln.date).days) <= PART_WINDOW_DAYS]
        near = sorted(near, key=lambda p: abs((p.date - ln.date).days))[:PART_POOL_CAP]
        found = []
        for n in range(PART_MIN_PARTS, PART_MAX_PARTS + 1):
            for combo in itertools.combinations(near, n):
                kind = _subset_fit(ln, combo)
                if kind:
                    found.append((combo, kind))
        if found:
            sol[i] = found
    claim: dict = {}
    for i, found in sol.items():
        for combo, _k in found:
            for p in combo:
                claim.setdefault(p.guid, set()).add(i)
    for i, found in sol.items():
        ln = lines[i]
        clash = any(len(claim[p.guid]) > 1 for combo, _k in found for p in combo)
        if len(found) == 1 and not clash:
            combo, kind = found[0]
            out.pairings.append(Pairing(ln, tuple(combo), "part",
                                        round(ln.amount - sum(p.amount for p in combo), 2)
                                        if kind == "cred" else 0.0))
            used.update(p.guid for p in combo)
            done_lines.add(i)
        else:
            members = {p.guid: p for combo, _k in found for p in combo}
            out.ambiguous.append({"line": ln, "payments": list(members.values()),
                                  "reason": "the statement line could be made up of different part payments"})
            used.update(members)
            done_lines.add(i)

    out.lines_unmatched = [ln for i, ln in enumerate(lines) if i not in done_lines]
    amb_guids = {p.guid for a in out.ambiguous for p in a["payments"]}
    paired = {pp.guid for pr in out.pairings for pp in pr.payments}
    for p in payments:
        if p.guid in paired:
            continue
        out.payments_unmatched.append(p)
        out.unmatched_reason[p.guid] = R_AMBIGUOUS if p.guid in amb_guids else _why_unmatched(p, lines)
    return out


def _why_unmatched(p: BookPayment, lines: list) -> str:
    near = [ln for ln in lines if abs((ln.date - p.date).days) <= PAY_WINDOW_DAYS]
    if near:
        best = min(near, key=lambda ln: abs(ln.amount - p.amount))
        return (f"{R_AMOUNT_OFF} (nearest statement payment line: {best.stmt.card} "
                f"{best.date:%d %b %Y} {best.amount:,.2f})")
    return R_NO_STATEMENT


# ---------------------------------------------------------------------------
# Settlement (RF-2): a payment line on statement k settles statement k-1.
# ---------------------------------------------------------------------------

S_SETTLED = "SETTLED"
S_PARTLY = "PARTLY SETTLED"
S_REFUSED = "REFUSED (not PASS)"
S_BEFORE = "SETTLES A STATEMENT FROM BEFORE THE RANGE"
S_NO_PRIOR = "SETTLES A STATEMENT NOT READ"


@dataclass
class Settlement:
    target: object                 # the statement settled (k-1), or None
    paid_on: object                # the statement carrying the payment lines (k)
    status: str
    due: float | None = None
    paid: float = 0.0
    pairings: list = field(default_factory=list)
    reason: str = ""

    @property
    def payments(self) -> list:
        return [p for pr in self.pairings for p in pr.payments]

    @property
    def pay_date(self):
        ds = [p.date for p in self.payments]
        return min(ds) if ds else None


def settle(pairings: list, run_statements: list, pool: list, ties_by_id: dict, find_prior, is_pass) -> list:
    """One Settlement per statement that matched payments settle. `ties_by_id`
    maps id(statement) to its TieOut."""
    in_run = {id(s) for s in run_statements}
    groups: dict = {}
    order: list = []
    for pr in pairings:
        target = find_prior(pr.line.stmt, pool)
        key = id(target) if target is not None else ("none", id(pr.line.stmt))
        if key not in groups:
            groups[key] = (target, pr.line.stmt, [])
            order.append(key)
        groups[key][2].append(pr)
    out = []
    for key in order:
        target, paid_on, prs = groups[key]
        paid = round(sum(pr.line.amount for pr in prs), 2)
        if target is None:
            out.append(Settlement(None, paid_on, S_NO_PRIOR, None, paid, prs, R_NO_PRIOR))
            continue
        tie = ties_by_id.get(id(target))
        due = tie.printed if tie is not None else None
        if id(target) not in in_run:
            out.append(Settlement(target, paid_on, S_BEFORE, due, paid, prs, R_BEFORE_RANGE))
            continue
        if tie is None or not is_pass(tie.result):
            out.append(Settlement(target, paid_on, S_REFUSED, due, paid, prs, R_REFUSED))
            continue
        if due is None or paid + SETTLE_TOL < due:
            out.append(Settlement(target, paid_on, S_PARTLY, due, paid, prs,
                                  f"paid {paid:,.2f} of {due:,.2f}" if due is not None else "no printed total"))
            continue
        out.append(Settlement(target, paid_on, S_SETTLED, due, paid, prs))
    return out
