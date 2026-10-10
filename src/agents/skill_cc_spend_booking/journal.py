"""Pure journal building for the card-spend booking skill (cash basis).

Every journal is two splits against the Credit Card Payment account (CCP):

  spend          Dr mapped expense          Cr CCP     (side W: the Account side is debited)
  refund         Dr CCP                     Cr the original expense
  cashback       Dr CCP                     Cr Drawings
  fee            Dr Bank Service Charge     Cr CCP
  fee reversal   Dr CCP                     Cr Bank Service Charge
  CRED overage   Dr CCP                     Cr Drawings

EMI interest (to the entity's EMI interest account) and the EMI processing fee / GST
(as fees) are journalled; EMI conversion, principal and unclassified rows are never journalled here. Nothing in this module reads
or writes a file.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

NUM_PREFIX = "CCSB-"
ALREADY_BOOKED_WINDOW_DAYS = 5
EXPENSE_PREFIXES = ("Expense:", "Expenses:")
LARGE_SPEND_FLAG = "Possible asset (jewellery, phone, appliance ...)"

K_SPEND, K_REFUND, K_CASHBACK = "spend", "refund", "cashback"
K_FEE, K_FEE_REV, K_OVERAGE = "fee", "fee_reversal", "cred_overage"
K_EMI_INT = "emi_interest"
# EMI kinds that ARE booked (user decision 10 Oct); every other emi_* kind is never booked.
EMI_FEE_KINDS = ("emi_processing_fee", "gst_on_emi")
EMI_UNVALIDATED = ("UNVALIDATED: EMI wording has not been checked against a real statement - "
                   "check every EMI row booked below before importing.")
KIND_LABEL = {K_SPEND: "Spend", K_REFUND: "Refund", K_CASHBACK: "Cashback", K_FEE: "Fee",
              K_FEE_REV: "Fee reversal", K_OVERAGE: "CRED overage",
              K_EMI_INT: "EMI interest"}

ST_READY = "READY"
ST_BOOKED = "ALREADY BOOKED"
ST_POSSIBLY = "POSSIBLY ALREADY BOOKED (withheld)"
ST_EMI = "WITHHELD: original purchase of an EMI conversion"
ST_BLOCKED = "WITHHELD: account is hidden or a placeholder"
ST_NO_ACCOUNT = "WITHHELD: no account"

EMI_UNBOOKED_TITLE = "EMI rows: not booked, awaiting decision"


@dataclass
class Journal:
    num: str
    kind: str
    date: date                   # the date it is booked on
    real_date: date              # the date on the statement
    description: str
    amount: float
    side: str                    # W: Account debited / CCP credited. D: Account credited / CCP debited
    card: str
    statement: str               # label of the statement it came from
    source: str = ""
    account: str = ""
    confidence: str = ""
    match_reason: str = ""
    needs_mapping: bool = False
    flag: str = ""
    status: str = ST_READY
    why: str = ""
    note: str = ""
    paired_with: str = ""        # a fee reversal: the fee it reverses
    raw_description: str = ""    # the statement wording, without the date suffix (what the mapper reads)
    emi: bool = False            # came from an emi_* row (booking is UNVALIDATED)
    row_id: int = 0              # id() of the statement row it came from

    @property
    def ccp_effect(self) -> float:
        """Effect on the CCP account (debit positive)."""
        return round(-self.amount if self.side == "W" else self.amount, 2)


def fy_start(d: date) -> date:
    return date(d.year if d.month >= 4 else d.year - 1, 4, 1)


def _day(d):
    return d.date() if isinstance(d, datetime) else d


def make_num(card: str, statement: str, kind: str, when: date, amount: float, description: str,
             occurrence: int) -> str:
    """Deterministic Transaction ID / Num for one entry. Re-running the same
    statement gives the same Num, so a re-import is recognised as already booked."""
    key = "|".join([card, statement, kind, when.isoformat(), f"{amount:.2f}", " ".join(description.split()),
                    str(occurrence)])
    return NUM_PREFIX + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def booking_date(real: date, pay_date: date) -> tuple:
    """(date to book on, note). A row dated before the financial year of the
    settling payment is booked on 1 April of that year (decision 6.2); the real
    date goes into the Description by the caller."""
    start = fy_start(pay_date)
    if real < start:
        return start, True
    return real, False


def _desc(base: str, real: date, moved: bool, label: str) -> str:
    base = " ".join((base or "").split())
    return f"{base} [{label} {real:%d-%b-%Y}]" if moved else base


def journals_for_settlement(target, pay_date: date, fee_reversal_rx, large_threshold: float,
                            book_interest: bool = True) -> tuple:
    """(journals, emi_rows, issues) for one SETTLED statement."""
    journals: list = []
    emi_rows: list = []
    issues: list = []
    seen: dict = defaultdict(int)
    for r in target.parsed.rows:
        if r.kind == "payment":
            if r.direction == "Dr":
                issues.append(f"{target.card} {target.label}: payment reversal row not booked: "
                              f"{r.description[:40]} {r.amount:,.2f}")
            continue
        emi_kind = None
        if r.kind.startswith("emi") or r.kind == "gst_on_emi":
            # only interest (when its account is usable) and the processing fee / GST are booked;
            # conversion, principal and unclassified rows are never booked
            if r.kind == "emi_interest" and r.direction == "Dr" and book_interest:
                emi_kind = K_EMI_INT
            elif r.kind in EMI_FEE_KINDS and r.direction == "Dr":
                emi_kind = K_FEE
            else:
                emi_rows.append(r)
                continue
        if r.date is None:
            issues.append(f"{target.card} {target.label}: row without a date not booked: "
                          f"{r.description[:40]} {r.amount:,.2f}")
            continue
        real = _day(r.date)
        if emi_kind:
            kind, side = emi_kind, "W"
        elif r.kind == "spend" and r.direction == "Dr":
            kind, side = K_SPEND, "W"
        elif r.kind == "refund" and r.direction == "Cr":
            if fee_reversal_rx is not None and fee_reversal_rx.search(r.description or ""):
                kind, side = K_FEE_REV, "D"
            else:
                kind, side = K_REFUND, "D"
        elif r.kind == "cashback" and r.direction == "Cr":
            kind, side = K_CASHBACK, "D"
        elif r.kind == "fee" and r.direction == "Dr":
            kind, side = K_FEE, "W"
        else:
            issues.append(f"{target.card} {target.label}: row not booked (unhandled {r.kind} {r.direction}): "
                          f"{r.description[:40]} {r.amount:,.2f}")
            continue
        when, moved = booking_date(real, pay_date)
        k = (kind, real, round(r.amount, 2), " ".join(r.description.split()))
        occ = seen[k]
        seen[k] += 1
        j = Journal(
            num=make_num(r.card, target.label, kind, real, r.amount, r.description, occ),
            kind=kind, date=when, real_date=real,
            description=_desc(r.description, real, moved, "spend" if kind == K_SPEND else KIND_LABEL[kind].lower()),
            amount=round(r.amount, 2), side=side, card=r.card, statement=target.label, source=target.source,
            needs_mapping=kind in (K_SPEND, K_REFUND), raw_description=" ".join(r.description.split()),
            emi=bool(emi_kind), row_id=id(r),
        )
        if moved:
            j.note = f"dated {real:%d %b %Y}, before the financial year of the payment: booked on {when:%d %b %Y}"
        if kind == K_SPEND and r.amount + 1e-9 >= large_threshold:
            j.flag = LARGE_SPEND_FLAG
        journals.append(j)
    return journals, emi_rows, issues


def overage_journal(pairing, when: date) -> Journal | None:
    """CRED paid the card slightly more than the bank sent it: the difference is
    Dr CCP / Cr Drawings. None when there is no overage."""
    if pairing.overage <= 0.004:
        return None
    guid_key = ",".join(sorted(p.guid for p in pairing.payments))
    r = pairing.line.row
    num = NUM_PREFIX + hashlib.sha1(
        f"cred-overage|{guid_key}|{pairing.line.date.isoformat()}|{pairing.overage:.2f}".encode()).hexdigest()[:12]
    return Journal(num=num, kind=K_OVERAGE, date=when, real_date=when,
                   description=f"CRED payment: card credited {pairing.overage:,.2f} more than the bank sent",
                   amount=round(pairing.overage, 2), side="D", card=r.card,
                   statement=pairing.line.stmt.label, source=pairing.line.stmt.source)


def pair_fee_reversals(journals: list) -> None:
    """Pair each fee reversal with a fee of the same card and amount (both go to
    Bank Service Charge). Annotates the reversal; an unpaired one is noted."""
    fees = [j for j in journals if j.kind == K_FEE]
    used: set = set()
    for rev in (j for j in journals if j.kind == K_FEE_REV):
        hit = next((f for f in fees if id(f) not in used and f.card == rev.card
                    and abs(f.amount - rev.amount) <= 0.005 and f.real_date <= rev.real_date), None)
        if hit is not None:
            used.add(id(hit))
            rev.paired_with = f"{hit.description[:40]} {hit.real_date:%d %b %Y}"
        else:
            rev.note = (rev.note + "; " if rev.note else "") + "no matching fee in this run"


def withhold_emi_originals(journals: list, emi_rows: list) -> list:
    """An EMI conversion credit reverses the original purchase. Booking the
    purchase as an expense AND the EMI later would count it twice, so the
    purchase journal is withheld. Returns warnings for conversions with no
    purchase journal in this run."""
    warnings: list = []
    for c in emi_rows:
        if c.kind != "emi_conversion" or c.direction != "Cr":
            continue
        hit = next((j for j in journals if j.kind == K_SPEND and j.status == ST_READY and j.card == c.card
                    and abs(j.amount - c.amount) <= 0.005 and (c.date is None or j.real_date <= _day(c.date))),
                   None)
        if hit is not None:
            hit.status = ST_EMI
            hit.why = f"converted to EMI on {c.date:%d %b %Y}" if c.date else "converted to EMI"
        else:
            warnings.append(f"{c.card} {c.period}: EMI conversion of {c.amount:,.2f} has no matching purchase "
                            f"in this run (it may already be booked as an expense)")
    return warnings


def emi_summary(emi_rows: list) -> dict:
    """Rows grouped for the EMI block, plus emi_interest subtotals per card and
    per financial year. Interest is never added into any fee total."""
    interest: dict = defaultdict(float)
    for r in emi_rows:
        if r.kind == "emi_interest" and r.date is not None:
            fy = fy_start(_day(r.date)).year
            interest[(r.card, f"{fy}-{(fy + 1) % 100:02d}")] += r.amount if r.direction == "Dr" else -r.amount
    return {"rows": list(emi_rows), "interest": {k: round(v, 2) for k, v in sorted(interest.items())}}


def fee_totals(journals: list) -> dict:
    """Totals of fee journals (EMI interest is not a fee journal and never in here)."""
    fees = [j for j in journals if j.kind == K_FEE and j.status == ST_READY]
    revs = [j for j in journals if j.kind == K_FEE_REV and j.status == ST_READY]
    return {"fees": round(sum(j.amount for j in fees), 2), "reversals": round(sum(j.amount for j in revs), 2),
            "net": round(sum(j.amount for j in fees) - sum(j.amount for j in revs), 2),
            "count": len(fees) + len(revs)}


# ---------------------------------------------------------------------------
# Already-booked check (RF-1)
# ---------------------------------------------------------------------------

def mark_booked(journals: list, txns: list, ccp_path: str) -> None:
    """A book transaction whose Num equals the journal's Num means the journal is
    ALREADY BOOKED (definite). The fallback catches a hand-booked entry: a book
    transaction whose Num is not ours and that has at least one split on an
    expense account other than CCP, with an expense split equal to the journal's
    amount (Dr for a debit-side kind, Cr for a credit-side kind), dated within
    ALREADY_BOOKED_WINDOW_DAYS of the statement or booking date. Whether it also
    touches CCP does not matter: Dr Expense / Cr CCP is a booked spend. A bill
    payment (CCP against a bank or asset account, no expense split) is not a
    candidate (RF-1). The account the mapper would choose is irrelevant, and an
    unmapped journal is checked too. Pairing is one-to-one, nearest date first,
    and only withholds with a warning."""
    by_num = {t.num: t for t in txns if t.num}
    for j in journals:
        if j.status == ST_READY and j.num in by_num:
            j.status = ST_BOOKED
            j.why = f"book transaction with Num {j.num} exists"
    cands = [t for t in txns if not (t.num or "").startswith(NUM_PREFIX)
             and any(p != ccp_path and p.startswith(EXPENSE_PREFIXES) for p, _ in t.splits)]
    pairs = []
    for ji, j in enumerate(journals):
        if j.status != ST_READY:
            continue
        want = j.amount if j.side == "W" else -j.amount          # expense side: debit +, credit -
        for t in cands:
            dist = min(abs((t.date - j.real_date).days), abs((t.date - j.date).days))
            if dist > ALREADY_BOOKED_WINDOW_DAYS:
                continue
            hit = next(((p, v) for p, v in t.splits
                        if p != ccp_path and p.startswith(EXPENSE_PREFIXES) and abs(v - want) <= 0.005), None)
            if hit is not None:
                pairs.append((dist, ji, t.guid, t, hit[0]))
    pairs.sort(key=lambda x: (x[0], x[1], x[2]))
    used_j: set = set()
    used_t: set = set()
    for dist, ji, guid, t, path in pairs:
        if ji in used_j or guid in used_t:
            continue
        used_j.add(ji)
        used_t.add(guid)
        j = journals[ji]
        j.status = ST_POSSIBLY
        j.why = (f"a book transaction on {t.date:%d %b %Y} ({dist} day(s) off) already has {j.amount:,.2f} "
                 f"on {path} ({t.description[:40]})")
