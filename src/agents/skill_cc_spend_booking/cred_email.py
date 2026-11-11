"""CC-40: CRED payment-confirmation emails as EVIDENCE for card-payment debits.

An email never creates, changes or doubles a booking. It only confirms the card a payment was already
routed to, or flags that the routing disagrees (a RED FLAG: that payment is held, not booked).

Tie rule: a debit ties to an email when its amount equals the email's "amount paid" exactly and the debit
date is the email's payment date, or up to 2 days after it. The card is the last four digits printed on the
email, resolved through cc_cards.yaml, with the issuer as a cross-check. One email ties to one debit and one
debit to one email; any group with more than one of either is listed as ambiguous and nothing in it is tied.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

SENDER_DOMAIN = "cred.club"
TIE_WINDOW_DAYS = 2
AMOUNT_TOL = 0.005

_ADDR_RX = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+)")
_ANGLE_RX = re.compile(r"<\s*[^<>@\s]+@([A-Za-z0-9.\-]+)\s*>")
_MONEY = r"(?:₹|Rs\.?)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
_PAID_RX = re.compile(r"^\s*amount\s+paid\s*[:\t ]\s*" + _MONEY + r"\s*$", re.I)
_DATE = r"([A-Za-z]{3,9})\.?\s+([0-9]{1,2}),?\s+([0-9]{4})"
_PAYDATE_RX = re.compile(r"^\s*payment\s+date\s*[:\t ]\s*" + _DATE + r"\s*$", re.I)
_CREDITED_RX = re.compile(r"^\s*credited\s+to\s+card\s*[:\t ]\s*" + _DATE + r"\s*$", re.I)
_CARDLINE_RX = re.compile(r"^\s*(.*?)\s*•{4}\s*([0-9]{4})\s*$")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


@dataclass
class Email:
    name: str
    amount: float
    pay_date: date
    credited: date | None
    issuer: str
    last4: str


@dataclass
class Tie:
    email: Email
    payment: object
    card: str = ""             # label resolved from the email's last four ("" = not in the card list)
    routed: tuple = ()         # labels the payment was already routed to
    status: str = ""           # confirmed | clash | unrouted | unresolved


@dataclass
class EmailResult:
    ties: list = field(default_factory=list)                 # Tie, every status
    unreadable: list = field(default_factory=list)           # (file name, reason)
    skipped: list = field(default_factory=list)              # (file name, reason)
    unmatched_emails: list = field(default_factory=list)     # (Email, reason)
    unmatched_debits: list = field(default_factory=list)     # BookPayment
    ambiguous: list = field(default_factory=list)            # ([Email], [BookPayment])
    held: list = field(default_factory=list)                 # Tie with status clash
    basis: dict = field(default_factory=dict)                # payment guid -> basis text
    n_emails: int = 0
    n_debits: int = 0
    emails: list = field(default_factory=list)

    @property
    def confirmed(self) -> list:
        return [t for t in self.ties if t.status == "confirmed"]

    @property
    def open_count(self) -> int:
        return (len(self.held) + len(self.unreadable) + len(self.unmatched_emails)
                + len(self.unmatched_debits) + len(self.ambiguous))


def _sender_ok(sender) -> bool:
    text = sender or ""
    m = _ANGLE_RX.search(text) or _ADDR_RX.search(text)
    if not m:
        return False
    dom = m.group(1).lower().rstrip(".")
    return dom == SENDER_DOMAIN or dom.endswith("." + SENDER_DOMAIN)


def _to_date(mon, day, year):
    mm = _MONTHS.get(mon[:3].lower())
    if not mm:
        return None
    try:
        return date(int(year), mm, int(day))
    except ValueError:
        return None


def parse_body(text, name=""):
    """(Email, "") or (None, reason). Reads by label, never by position. The CRED PROTECT block's
    statement-bill amount is not labelled "amount paid" and is never read."""
    paid = pay = credited = None
    issuer = last4 = ""
    paid_seen = 0
    for raw in (text or "").splitlines():
        line = raw.replace(" ", " ")
        m = _PAID_RX.match(line)
        if m:
            paid_seen += 1
            try:
                paid = round(float(m.group(1).replace(",", "")), 2)
            except ValueError:
                paid = None
            continue
        m = _PAYDATE_RX.match(line)
        if m and pay is None:
            pay = _to_date(*m.groups())
            continue
        m = _CREDITED_RX.match(line)
        if m and credited is None:
            credited = _to_date(*m.groups())
            continue
        m = _CARDLINE_RX.match(line)
        if m and not last4:
            issuer, last4 = m.group(1).strip(), m.group(2)
    if paid_seen > 1:
        return None, "more than one 'amount paid' line"
    if paid is None:
        return None, "no readable 'amount paid'"
    if not last4:
        return None, "no card last four digits"
    if pay is None:
        return None, "no readable 'payment date'"
    return Email(name, paid, pay, credited, issuer, last4), ""


def scan(folder, read_msg=None) -> EmailResult:
    """Read every file in the folder. Non-.msg files and other senders are skipped and counted."""
    if read_msg is None:
        from agents.msg_reader import read_msg as _rm
        read_msg = _rm
    res = EmailResult()
    for p in sorted(Path(folder).iterdir(), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
        if p.suffix.lower() != ".msg":
            res.skipped.append((p.name, "not a .msg file"))
            continue
        try:
            msg = read_msg(p)
        except Exception as exc:  # noqa: BLE001 - an unreadable file is listed, never fatal
            res.unreadable.append((p.name, f"cannot be read ({type(exc).__name__})"))
            continue
        if not _sender_ok(getattr(msg, "sender", None)):
            res.skipped.append((p.name, f"sender is not {SENDER_DOMAIN}"))
            continue
        em, why = parse_body(getattr(msg, "body", "") or "", p.name)
        if em is None:
            res.unreadable.append((p.name, why))
        else:
            res.emails.append(em)
    res.n_emails = len(res.emails)
    return res


def _issuer_ok(issuer, card) -> bool:
    bank = (card.bank or "").lower()
    return bool(bank) and re.search(r"(?<![a-z])" + re.escape(bank) + r"(?![a-z])", (issuer or "").lower()) is not None


def tie(res: EmailResult, payments: list, routing: dict, cards, narration=frozenset()) -> EmailResult:
    """Fill `res` with the ties for these debits. `routing` is {payment guid: set of card labels} the payment
    is already routed to; `narration` the guids whose narration named the card. Repeatable."""
    from agents import cc_cards
    emails = list(res.emails)
    res.ties, res.unmatched_emails, res.unmatched_debits = [], [], []
    res.ambiguous, res.held, res.basis = [], [], {}
    res.n_debits = len(payments)
    edges: dict = {i: [] for i in range(len(emails))}
    back: dict = {p.guid: [] for p in payments}
    for i, em in enumerate(emails):
        for p in payments:
            if abs(round(p.amount, 2) - em.amount) <= AMOUNT_TOL and \
                    em.pay_date <= p.date <= em.pay_date + timedelta(days=TIE_WINDOW_DAYS):
                edges[i].append(p)
                back[p.guid].append(i)
    seen_e: set = set()
    by_guid = {p.guid: p for p in payments}
    tied_guids: set = set()
    listed_e: set = set()
    for i in range(len(emails)):
        if i in seen_e:
            continue
        ge, gp, stack = set(), set(), [("E", i)]
        while stack:
            k, key = stack.pop()
            if k == "E":
                if key in ge:
                    continue
                ge.add(key)
                stack.extend(("P", p.guid) for p in edges[key])
            else:
                if key in gp:
                    continue
                gp.add(key)
                stack.extend(("E", j) for j in back[key])
        seen_e |= ge
        if not gp:
            continue
        listed_e |= ge
        if len(ge) == 1 and len(gp) == 1:
            em, p = emails[next(iter(ge))], by_guid[next(iter(gp))]
            card = cc_cards.card_for_last4(cards, em.last4) if cards else None
            if card is None or not _issuer_ok(em.issuer, card):
                why = ("its card is not in the card list" if card is None
                       else "its issuer does not match the listed card")
                res.unmatched_emails.append((em, f"{why}; not used as evidence"))
                continue
            tied_guids.add(p.guid)
            routed = tuple(sorted(routing.get(p.guid, ())))
            if not routed:
                status = "unrouted"
            elif set(routed) == {card.label}:
                status = "confirmed"
            else:
                status = "clash"
            t = Tie(em, p, card.label, routed, status)
            res.ties.append(t)
            if status == "clash":
                res.held.append(t)
            elif status == "confirmed":
                res.basis[p.guid] = "email-confirmed"
        else:
            res.ambiguous.append(([emails[j] for j in sorted(ge)],
                                  sorted((by_guid[g] for g in gp), key=lambda x: (x.date, x.guid))))
            tied_guids |= gp
    for i, em in enumerate(emails):
        if i not in listed_e:
            res.unmatched_emails.append((em, "no bank debit of that amount within 2 days of the payment date"))
    res.unmatched_debits = [p for p in payments if p.guid not in tied_guids]
    for p in payments:
        if p.guid not in res.basis:
            res.basis[p.guid] = "narration" if p.guid in narration else (
                "statement line" if routing.get(p.guid) else "no card evidence")
    return res
