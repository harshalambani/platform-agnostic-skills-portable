"""Pure matching of book payments to statement payment lines, and settlement.

Cash basis. A payment line printed on statement k settles statement k-1 of the
same card (RF-2). Book payments are debit splits on the Credit Card Payment
account (CCP). Pairing is one-to-one and never guessed: a pair is accepted only
when the line has exactly one candidate payment and that payment has exactly
one candidate line. Everything else is reported as ambiguous, unmatched or
partly matched.

Named tolerances (decision 6.8):
  CRED_MAX_OVERAGE   a CRED payment line may exceed the bank amount by 0..50 rupees (per entity: cc_cred_max_overage)
  PAY_WINDOW_DAYS    a bank payment and its statement line are within +/-5 days
  PART_WINDOW_DAYS   the parts of a part payment are within 15 days of the line
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

CRED_MAX_OVERAGE = 50.0   # the default; an entity may set cc_cred_max_overage (CC-31)
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
R_REFUSED = "the statement it settles has no tie-out result, so it was not read whole"
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


def single_fit(line: PayLine, pay: BookPayment, cred_max: float = CRED_MAX_OVERAGE):
    """'exact' | 'cred' | None for one line against one bank payment."""
    if abs((line.date - pay.date).days) > PAY_WINDOW_DAYS:
        return None
    diff = round(line.amount - pay.amount, 2)
    if abs(diff) <= AMOUNT_TOL:
        return "exact"
    if (line.cred or pay.cred) and 0 < diff <= cred_max + 1e-9:
        return "cred"
    return None


def _subset_fit(line: PayLine, parts, cred_max: float = CRED_MAX_OVERAGE):
    total = round(sum(p.amount for p in parts), 2)
    diff = round(line.amount - total, 2)
    if abs(diff) <= AMOUNT_TOL:
        return "exact"
    if (line.cred or any(p.cred for p in parts)) and 0 < diff <= cred_max + 1e-9:
        return "cred"
    return None


def match_payments(lines: list, payments: list, cred_max: float = CRED_MAX_OVERAGE) -> MatchOutcome:
    out = MatchOutcome()
    cand: dict = {i: [] for i in range(len(lines))}
    back: dict = {p.guid: [] for p in payments}
    for i, ln in enumerate(lines):
        for p in payments:
            if single_fit(ln, p, cred_max):
                cand[i].append(p)
                back[p.guid].append(i)

    used: set = set()           # payment guids paired or involved in an ambiguity
    done_lines: set = set()
    # CC-09: the statement has exactly as many payment lines of an amount as the bank has payments
    # of it (and nothing else could fit them): pair them one-to-one by date. Unequal counts, or any
    # line/payment that also fits something outside the group, stay ambiguous below.
    by_amt_l: dict = {}
    by_amt_p: dict = {}
    for i, ln in enumerate(lines):
        by_amt_l.setdefault(ln.amount, []).append(i)
    for p in payments:
        by_amt_p.setdefault(round(p.amount, 2), []).append(p)
    for amt, idxs in by_amt_l.items():
        ps = by_amt_p.get(amt, [])
        if len(idxs) < 2 or len(idxs) != len(ps):
            continue
        guids = {p.guid for p in ps}
        if any(not {q.guid for q in cand[i]} <= guids for i in idxs):
            continue
        if any(not set(back[g]) <= set(idxs) for g in guids):
            continue
        pairs = list(zip(sorted(idxs, key=lambda i: (lines[i].date, i)), sorted(ps, key=lambda p: (p.date, p.guid))))
        if any(single_fit(lines[i], p, cred_max) != "exact" for i, p in pairs):
            continue
        for i, p in pairs:
            out.pairings.append(Pairing(lines[i], (p,), "exact", 0.0))
            used.add(p.guid)
            done_lines.add(i)
    # CC-31: rank before calling a match ambiguous. For one line the best bank payment is the exact
    # amount, then the nearest date; for one bank payment the best line ranks the same way. A pair is
    # taken only when each is the UNIQUE best of the other (one-to-one, never guessed); a true tie at the
    # best rank stays ambiguous (also across two cards: decision 6.6).
    def rank(i, p):
        return (0 if single_fit(lines[i], p, cred_max) == "exact" else 1, abs((lines[i].date - p.date).days))

    taken: set = set()
    paired_lines: set = set()
    progress = True
    while progress:
        progress = False
        for i, ln in enumerate(lines):
            if i in done_lines or i in paired_lines:
                continue
            cs = [p for p in cand[i] if p.guid not in taken and p.guid not in used]
            if not cs:
                continue
            best = min(rank(i, p) for p in cs)
            top = [p for p in cs if rank(i, p) == best]
            if len(top) != 1:
                continue
            p = top[0]
            rivals = [j for j in back[p.guid] if j not in done_lines and j not in paired_lines]
            jbest = min(rank(j, p) for j in rivals)
            if [j for j in rivals if rank(j, p) == jbest] != [i]:
                continue
            kind = single_fit(ln, p, cred_max)
            out.pairings.append(Pairing(ln, (p,), kind,
                                        round(ln.amount - p.amount, 2) if kind == "cred" else 0.0))
            taken.add(p.guid)
            paired_lines.add(i)
            progress = True
    used.update(taken)
    done_lines.update(paired_lines)
    for i, ln in enumerate(lines):
        if i in done_lines:
            continue
        cs = [p for p in cand[i] if p.guid not in taken]
        if cs:
            n_lines = len(by_amt_l.get(ln.amount, []))
            n_pay = len(by_amt_p.get(round(ln.amount, 2), []))
            why = f"the statement line could be {len(cs)} bank payments"
            if n_lines != n_pay:
                # CC-09: say why they were not paired one-to-one
                why += (f" ({n_lines} statement payment line(s) of {ln.amount:,.2f} against {n_pay} bank "
                        f"payment(s) of it: the counts differ, so they were not paired by date)")
            out.ambiguous.append({"line": ln, "payments": list(cs), "reason": why})
            used.update(p.guid for p in cs)
            done_lines.add(i)
    # a payment that fits several still-open lines is ambiguous
    paired_guids = {pp.guid for pr in out.pairings for pp in pr.payments}
    for p in payments:
        open_lines = [j for j in back[p.guid] if j not in paired_lines and not any(
            pr.line is lines[j] for pr in out.pairings)]
        if len(open_lines) > 1 and p.guid not in paired_guids:
            out.ambiguous.append({"line": None, "payments": [p],
                                  "reason": f"the bank payment could settle {len(open_lines)} statement lines"})
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
                kind = _subset_fit(ln, combo, cred_max)
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
S_CSV_PRIOR = "PAYS CSV-BOOKED SPENDS"      # CC-31: the statement it pays is covered by a whole-booked CSV export
CLOSED_STATUSES = (S_SETTLED, S_CSV_PRIOR)
R_CSV_PRIOR = "pays spends booked from a CSV export of the same card"
R_IN_CREDIT = "in credit, nothing owed"


@dataclass
class Settlement:
    target: object                 # the statement settled (k-1), or None
    paid_on: object                # the statement carrying the payment lines (k)
    status: str
    due: float | None = None
    paid: float = 0.0
    pairings: list = field(default_factory=list)
    reason: str = ""
    credits: float = 0.0           # CC-32: refunds and cashback on k that reduced the balance carried from k-1
    book_date: object = None       # CC-32: the date to book on when no payment dates it (a statement in credit)

    @property
    def payments(self) -> list:
        return [p for pr in self.pairings for p in pr.payments]

    @property
    def pay_date(self):
        ds = [p.date for p in self.payments]
        return min(ds) if ds else self.book_date


def _closing(st, ties_by_id):
    tie = ties_by_id.get(id(st))
    if tie is not None and tie.printed is not None:
        return tie.printed
    return (getattr(st.parsed, "summary", None) or {}).get("total")


def prior_statement(k, pool: list, ties_by_id: dict, find_prior):
    """The statement k settles. The date rule first; when it finds nothing (periods that overlap), or
    finds one whose closing balance is not the balance k carries forward, the one statement of the same
    card that closed on exactly that carried balance (to a rupee) - CC-32. No balance match: as before."""
    t = find_prior(k, pool)
    prev = (getattr(k.parsed, "summary", None) or {}).get("prev")
    if prev is None or k.period is None or abs(prev) <= SETTLE_TOL:
        return t

    def fits(s):
        c = _closing(s, ties_by_id)
        return c is not None and abs(c - prev) <= SETTLE_TOL

    if t is not None and fits(t):
        return t
    cands = [s for s in pool if s is not k and s.card == k.card and s.period is not None
             and s.period[0] < k.period[0] and fits(s)]
    return cands[0] if len(cands) == 1 else t


def _csv_covering(csv_statements, card, day):
    for c in csv_statements:
        if c.card == card and c.period is not None and c.period[0] <= day <= c.period[1]:
            return c
    return None


def settle(pairings: list, run_statements: list, pool: list, ties_by_id: dict, find_prior, is_pass,
           csv_statements=(), csv_pairings=(), range_end=None) -> list:
    """One Settlement per statement that matched payments settle. `ties_by_id`
    maps id(statement) to its TieOut. CC-31/32: csv_statements / csv_pairings are the whole-booked CSV
    exports of the run and their payment rows matched to bank payments."""
    in_run = {id(s) for s in run_statements}
    csv_statements = list(csv_statements)
    groups: dict = {}
    order: list = []
    for pr in pairings:
        k = pr.line.stmt
        target = prior_statement(k, pool, ties_by_id, find_prior)
        key = id(target) if target is not None else ("none", id(k))
        if key not in groups:
            groups[key] = (target, k, [])
            order.append(key)
        groups[key][2].append(pr)
    # CC-32: payment rows of a CSV export count as the payments of the period AFTER a PDF statement that
    # has no later PDF statement, when the export covers that period. Only rows matched to the bank.
    for pr in csv_pairings:
        c = pr.line.stmt
        best = None
        for t in run_statements:
            if t.card != c.card or t.period is None or c.period is None or t.period[1] >= pr.line.date:
                continue
            nxt_day = t.period[1] + timedelta(days=1)
            if not (c.period[0] <= nxt_day <= c.period[1]):
                continue
            if any(o is not t and o.card == t.card and prior_statement(o, pool, ties_by_id, find_prior) is t
                   for o in pool):
                continue
            if best is None or t.period[1] > best.period[1]:
                best = t
        if best is not None:
            key = id(best)
            if key not in groups:
                groups[key] = (best, c, [])
                order.append(key)
            groups[key][2].append(pr)
    out = []
    targeted: set = set()
    for key in order:
        target, paid_on, prs = groups[key]
        paid = round(sum(pr.line.amount for pr in prs), 2)
        if target is None:
            covered = (paid_on.period is not None
                       and _csv_covering(csv_statements, paid_on.card, paid_on.period[0] - timedelta(days=1)))
            if covered:
                out.append(Settlement(None, paid_on, S_CSV_PRIOR, None, paid, prs, R_CSV_PRIOR))
            else:
                out.append(Settlement(None, paid_on, S_NO_PRIOR, None, paid, prs, R_NO_PRIOR))
            continue
        targeted.add(id(target))
        tie = ties_by_id.get(id(target))
        due = tie.printed if tie is not None else None
        if id(target) not in in_run:
            out.append(Settlement(target, paid_on, S_BEFORE, due, paid, prs, R_BEFORE_RANGE))
            continue
        # CC-26: a tie-out that does not pass no longer refuses the settlement; the rows are booked and
        # reported loudly as NOT TIED. A statement with no tie-out at all is still refused.
        if tie is None:
            out.append(Settlement(target, paid_on, S_REFUSED, due, paid, prs, R_REFUSED))
            continue
        credits = 0.0
        if id(paid_on) in in_run:
            credits = round(sum(r.amount for r in paid_on.parsed.rows
                                if r.direction == "Cr" and r.kind in ("refund", "cashback")), 2)
        if due is None or paid + credits + SETTLE_TOL < due:
            if due is None:
                why = "no printed total"
            elif credits:
                why = f"paid {paid:,.2f} and credited {credits:,.2f} of {due:,.2f}"
            else:
                why = f"paid {paid:,.2f} of {due:,.2f}"
            out.append(Settlement(target, paid_on, S_PARTLY, due, paid, prs, why, credits))
            continue
        if paid + SETTLE_TOL < due:
            out.append(Settlement(target, paid_on, S_SETTLED, due, paid, prs,
                                  f"paid {paid:,.2f} and credited {credits:,.2f} of {due:,.2f}", credits))
            continue
        out.append(Settlement(target, paid_on, S_SETTLED, due, paid, prs, "", credits))
    # CC-32: a statement whose printed closing balance is in credit (or nil) owes nothing: it is settled
    # without a payment and its spends are booked on its own period end.
    for st in run_statements:
        if id(st) in targeted or st.period is None:
            continue
        tie = ties_by_id.get(id(st))
        if tie is None or not is_pass(tie.result) or tie.printed is None or tie.printed > SETTLE_TOL:
            continue
        if range_end is not None and st.period[1] > range_end:
            continue
        out.append(Settlement(st, None, S_SETTLED, tie.printed, 0.0, [], R_IN_CREDIT, 0.0, st.period[1]))
    return out
