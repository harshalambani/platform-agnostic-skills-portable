"""CC-28: the one card list every credit-card skill reads (cc_cards.yaml).

The file lives in the settings directory, next to config.yaml, and is found the
same way persistent_rules.py finds its files: ``Path(settings_path).resolve().parent``.

    cards:
      - bank: "ICICI"
        card: "Sapphiro"
        last4: ["0001", "0002"]     # a list of 4-digit TEXT values; one card can list two
        holder: "entity_a"          # an entity key from entities.yaml, or "" when not set
        active: true

The loader is strict and never echoes a card digit in an error: a message names
the card by its position in the file ("card 3") and the field, nothing more.
Card digits are identification data, so they are kept out of every log line too.

This module has no UI and no skill logic; CC Sort, Credit Card Transactions,
CC Book Spends and the Cards screen all import it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

CARDS_FILENAME = "cc_cards.yaml"
ALLOWED_KEYS = ("bank", "card", "last4", "holder", "active")
OPTIONAL_KEYS = ("account",)          # CC-39: the card's own sub-account under the pool
TOP_KEYS = ("cards", "ccp_split_from", "prepayment_account")
# The Credit Card Payment pool. Kept equal to skill_cc_spend_booking.bookio.CCP_PATH by a test.
CCP_PATH = "Expense:Withdrawals:Credit Card Payment"

_LAST4_RX = re.compile(r"[0-9]{4}")

# The CC Sort folders (<bank>-<card_type>) a listed card may point at. A card whose
# bank/card pair is not one of these has nowhere to be sorted to and is refused.
KNOWN_FOLDERS: tuple[tuple[str, str], ...] = (
    ("Axis", "Flipkart"), ("Axis", "IndianOil"),
    ("HDFC", "Regalia"), ("HDFC", "TataNeu"),
    ("ICICI", "Amazon"), ("ICICI", "Sapphiro"),
    ("SBI", "BPCL-Octane"), ("SBM", "Global"),
    ("HSBC", "Premier"), ("YES", "Reserv"),
)
# Spellings that also reach a folder: ("SBM", "Niyo Global") is SBM-Global.
_ALIASES: dict[tuple[str, str], tuple[str, str]] = {
    ("sbm", "niyoglobal"): ("SBM", "Global"),
    ("sbmniyo", "global"): ("SBM", "Global"),
    ("sbmniyoglobal", "creditcard"): ("SBM", "Global"),
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


_FOLDER_BY_NORM = {(_norm(b), _norm(c)): (b, c) for b, c in KNOWN_FOLDERS}
_FOLDER_BY_NORM.update(_ALIASES)

TOP_COMMENT = """\
# cc_cards.yaml - the one list of credit cards the CC skills read.
#
# One entry per card:
#   bank    the bank, as text, e.g. "ICICI"
#   card    the card, as text, e.g. "Sapphiro"; the bank and card must match a CC Sort
#           folder (the folder is named <bank>-<card>)
#   last4   a LIST of the card's last 4 digits, each one in quotes: ["1234"]. A card with
#           two numbers on one bill lists both. Never the full card number.
#   holder  the entity key from entities.yaml whose card this is, or "" when not set
#   active  true or false; a card that is false is still recognised, it is just not in use
#
# Write every value in double quotes. Without quotes YAML turns YES into true and
# 0123 into a number, and the file is then refused. No other keys are allowed.
#   account (optional) the full GnuCash path of this card's own sub-account under
#           Expense:Withdrawals:Credit Card Payment. Blank: the card is not split.
#
# Two optional top-level keys sit above "cards:":
#   ccp_split_from      "2026-04-01" - payments on or after this date are moved to the card's
#                       sub-account. Absent: no card is split at all.
#   prepayment_account  the Drawings account a corporate-card prepayment is booked to.
#
# A last4 may appear on one card only. Keep this comment block; the Cards screen keeps it.
"""


class CardListError(ValueError):
    """The card list is missing or not usable. The message never carries digits."""


class CardListMissing(CardListError):
    """cc_cards.yaml does not exist in the settings folder."""


@dataclass(frozen=True)
class Card:
    bank: str          # canonical, as in the CC Sort folder name
    card: str          # canonical, as in the CC Sort folder name
    last4: tuple[str, ...]
    holder: str        # entity key, or "" when blank
    active: bool
    account: str = ""  # CC-39: sub-account path under CCP_PATH, or "" (not split)

    @property
    def folder(self) -> tuple[str, str]:
        return (self.bank, self.card)

    @property
    def label(self) -> str:
        return f"{self.bank}-{self.card}"


def folder_for(bank: str, card: str) -> tuple[str, str] | None:
    return _FOLDER_BY_NORM.get((_norm(bank), _norm(card)))


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

def cards_path_for(settings_path) -> Path:
    """cc_cards.yaml beside the settings file (persistent_rules.py's convention)."""
    return Path(settings_path).resolve().parent / CARDS_FILENAME


def entities_path_for(settings_path) -> Path:
    """The entities.yaml of the same data root: <data_root>/itr/entities.yaml."""
    return Path(settings_path).resolve().parent.parent / "itr" / "entities.yaml"


def entity_keys_from(entities_path) -> set[str]:
    """Top-level keys of entities.yaml; an empty set when it cannot be read."""
    try:
        import yaml
        raw = yaml.safe_load(Path(entities_path).read_text(encoding="utf-8")) or {}
    except Exception:
        return set()
    if not isinstance(raw, dict):
        return set()
    return {str(k) for k, v in raw.items() if isinstance(v, dict)}


# ---------------------------------------------------------------------------
# The strict loader
# ---------------------------------------------------------------------------

class CardList(list):
    """The cards, plus the two optional top-level settings (CC-39 / CC-29). A plain
    list to every caller that predates them."""
    split_from = None            # datetime.date or None
    prepayment_account = ""


def _check_account(where: str, value, label: str = "account") -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CardListError(f"{where}: '{label}' must be text in quotes, or blank")
    v = value.strip()
    if v and v != value:
        raise CardListError(f"{where}: '{label}' has spaces around it")
    return v


def _parse_split_date(value):
    import datetime as _dt
    if not isinstance(value, str):
        raise CardListError("'ccp_split_from' must be a date as text in quotes, like \"2026-04-01\"")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise CardListError("'ccp_split_from' must be a date like \"2026-04-01\"")
    try:
        return _dt.date.fromisoformat(value)
    except ValueError:
        raise CardListError("'ccp_split_from' is not a real date") from None


def parse_cards(raw, entity_keys) -> CardList:
    """Validate an already-parsed YAML value. Raises CardListError (no digits in it)."""
    keys = set(entity_keys or ())
    if not isinstance(raw, dict):
        raise CardListError("the card list must be a mapping with a 'cards' list")
    extra = [k for k in raw if k not in TOP_KEYS]
    if extra:
        raise CardListError("the card list has a top-level key other than "
                            + ", ".join(f"'{k}'" for k in TOP_KEYS))
    entries = raw.get("cards")
    if not isinstance(entries, list):
        raise CardListError("'cards' must be a list")

    cards = CardList()
    if raw.get("ccp_split_from") is not None:
        cards.split_from = _parse_split_date(raw["ccp_split_from"])
    elif "ccp_split_from" in raw:
        raise CardListError("'ccp_split_from' must be a date as text in quotes, or left out")
    pre = _check_account("the card list", raw.get("prepayment_account"), "prepayment_account")
    if pre == CCP_PATH or pre.startswith(CCP_PATH + ":"):
        raise CardListError("'prepayment_account' must be a Drawings account, not the Credit Card Payment pool")
    cards.prepayment_account = pre
    seen: dict[str, int] = {}
    seen_acct: dict[str, int] = {}
    for i, e in enumerate(entries, 1):
        where = f"card {i}"
        if not isinstance(e, dict):
            raise CardListError(f"{where}: each card must be a mapping")
        for k in e:
            if k not in ALLOWED_KEYS and k not in OPTIONAL_KEYS:
                raise CardListError(f"{where}: unknown key (allowed: {', '.join(ALLOWED_KEYS + OPTIONAL_KEYS)})")
        for k in ALLOWED_KEYS:
            if k not in e:
                raise CardListError(f"{where}: '{k}' is missing")
        bank, card = e["bank"], e["card"]
        if not isinstance(bank, str) or not bank.strip():
            raise CardListError(f"{where}: 'bank' must be text in quotes (an unquoted YES or NO is read as true or false)")
        if not isinstance(card, str) or not card.strip():
            raise CardListError(f"{where}: 'card' must be text in quotes")
        folder = folder_for(bank, card)
        if folder is None:
            raise CardListError(f"{where}: this bank and card have no CC Sort folder or alias")
        l4 = e["last4"]
        if not isinstance(l4, list) or not l4:
            raise CardListError(f"{where}: 'last4' must be a list of 4-digit values in quotes")
        for v in l4:
            if not isinstance(v, str) or not _LAST4_RX.fullmatch(v):
                raise CardListError(f"{where}: each 'last4' value must be exactly 4 digits, in quotes")
        if len(set(l4)) != len(l4):
            raise CardListError(f"{where}: the same last4 is listed twice on this card")
        for v in l4:
            if v in seen:
                raise CardListError(f"card {seen[v]} and card {i} share a last4; a last4 belongs to one card")
        for v in l4:
            seen[v] = i
        holder = e["holder"]
        if holder is None:
            holder = ""
        if not isinstance(holder, str):
            raise CardListError(f"{where}: 'holder' must be an entity key in quotes, or blank")
        if holder != "" and holder not in keys:
            raise CardListError(f"{where}: 'holder' is not an entity key in entities.yaml")
        if not isinstance(e["active"], bool):
            raise CardListError(f"{where}: 'active' must be true or false")
        acct = _check_account(where, e.get("account"))
        if acct:
            if not acct.startswith(CCP_PATH + ":") or acct.endswith(":") or "::" in acct:
                raise CardListError(f"{where}: 'account' must be a sub-account under {CCP_PATH}")
            if acct in seen_acct:
                raise CardListError(f"card {seen_acct[acct]} and card {i} share an account; "
                                    f"a sub-account belongs to one card")
            seen_acct[acct] = i
        cards.append(Card(folder[0], folder[1], tuple(l4), holder, e["active"], acct))
    return cards


def parse_text(text: str, entity_keys) -> CardList:
    import yaml
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f" near line {mark.line + 1}" if mark is not None else ""
        # The parser's own message quotes file content, which could carry digits.
        raise CardListError(f"the card list is not valid YAML{line}") from None
    return parse_cards(raw, entity_keys)


def load_cards(path, entity_keys) -> CardList:
    """Load and validate cc_cards.yaml. A missing or unreadable file is an error,
    never 'no cards'."""
    p = Path(path)
    if not p.is_file():
        raise CardListMissing(f"the card list {CARDS_FILENAME} was not found in the settings folder")
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise CardListError(f"the card list {CARDS_FILENAME} could not be read") from None
    return parse_text(text, entity_keys)


def load_for_settings(settings_path, entities_path=None) -> CardList:
    """The loader as the skills call it: file beside the settings, holders checked
    against the data root's entities.yaml (or the one given)."""
    ents = Path(entities_path) if entities_path else entities_path_for(settings_path)
    return load_cards(cards_path_for(settings_path), entity_keys_from(ents))


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def index_by_last4(cards) -> dict[str, Card]:
    return {d: c for c in cards for d in c.last4}


def card_for_last4(cards, last4: str) -> Card | None:
    return index_by_last4(cards).get(last4)


def card_for_tail(cards, tail: str) -> Card | None:
    """A card whose last4 ends with `tail` (3 digits: Amex prints a 3-digit last
    group). Only when exactly one listed last4 ends that way."""
    hits = {d: c for d, c in index_by_last4(cards).items() if d.endswith(tail)}
    if len(hits) == 1:
        return next(iter(hits.values()))
    return None


# ---------------------------------------------------------------------------
# Finding digits in text
# ---------------------------------------------------------------------------

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_TOKEN_RX = re.compile(r"(?<![0-9])[Xx*]*([0-9]{4})(?![0-9])")


def _is_year_like(text: str, start: int, end: int, value: str) -> bool:
    if not 1900 <= int(value) <= 2100:
        return False
    before = text[max(0, start - 6):start].lower()
    after = text[end:end + 6].lower()
    if before[-1:] in ("/", ".", "-") or after[:1] in ("/", ".", "-"):
        return True
    return bool(re.search(_MONTH + r"[\s_-]*$", before) or re.match(r"[\s_-]*" + _MONTH, after))


def digit_tokens(text: str) -> list[str]:
    """Every exact 4-digit token in a file name: not inside a longer number, not part
    of a decimal or comma amount, not a year (1900-2100 beside / . - or a month name).
    A leading X, x or * (a mask) is allowed."""
    out = []
    for m in _TOKEN_RX.finditer(text):
        s, e = m.start(1), m.end(1)
        prev2 = text[max(0, s - 2):s]
        if re.search(r"[0-9][.,]$", prev2) or re.match(r"[.,][0-9]", text[e:e + 2]):
            continue
        if _is_year_like(text, s, e, m.group(1)):
            continue
        out.append(m.group(1))
    return out


def cards_named_in(text: str, cards) -> list[Card]:
    """The distinct listed cards whose last4 appears as a token in `text`."""
    idx = index_by_last4(cards)
    found: list[Card] = []
    for t in digit_tokens(text):
        c = idx.get(t)
        if c is not None and c not in found:
            found.append(c)
    return found


_PRINTED_RX = re.compile(
    r"(?<![0-9Xx*])[0-9Xx*]{4}(?:[\s-]*[0-9Xx*]{4,6}){1,2}[\s-]*[0-9Xx*]{3,5}(?![0-9Xx*])")


def printed_last4s(text: str) -> list[str]:
    """Last 4 digits of each masked card number printed in `text` (a PDF's text).
    A masked number has at least four mask characters and ends in 4 digits; plain
    digit runs (amounts, account numbers with no mask) are not taken."""
    out: list[str] = []
    for m in _PRINTED_RX.finditer(text or ""):
        s = re.sub(r"[\s-]", "", m.group(0))
        if len(re.findall(r"[Xx*]", s)) < 4:
            continue
        tail = s[-4:]
        if _LAST4_RX.fullmatch(tail) and tail not in out:
            out.append(tail)
    return out


# ---------------------------------------------------------------------------
# Writing (the Cards screen)
# ---------------------------------------------------------------------------

def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def leading_comments(text: str) -> str:
    """The comment/blank lines at the top of the file, verbatim."""
    lines = []
    for ln in text.splitlines(keepends=True):
        if ln.strip() == "" or ln.lstrip().startswith("#"):
            lines.append(ln)
        else:
            break
    return "".join(lines)


def render_yaml(rows: list[dict], header: str = TOP_COMMENT, split_from: str = "",
                prepayment_account: str = "") -> str:
    """The file text: `header` verbatim, then every text value double-quoted. `split_from`
    (ISO date text) and `prepayment_account` are written only when set; `account` is written
    on a card only when it has one."""
    out = [header if header.endswith("\n") or not header else header + "\n"]
    if header and not header.endswith("\n\n"):
        out.append("\n")
    if split_from:
        out.append(f"ccp_split_from: {_q(split_from)}\n")
    if prepayment_account:
        out.append(f"prepayment_account: {_q(prepayment_account)}\n")
    out.append("cards:" + (" []" if not rows else "") + "\n")
    for r in rows:
        out.append(f"  - bank: {_q(r['bank'])}\n")
        out.append(f"    card: {_q(r['card'])}\n")
        out.append("    last4: [" + ", ".join(_q(v) for v in r["last4"]) + "]\n")
        out.append(f"    holder: {_q(r.get('holder') or '')}\n")
        out.append(f"    active: {'true' if r['active'] else 'false'}\n")
        if r.get("account"):
            out.append(f"    account: {_q(r['account'])}\n")
    return "".join(out)
