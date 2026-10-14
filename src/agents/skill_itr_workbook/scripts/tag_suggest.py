"""
tag_suggest.py -- ITR-07: plain-word helpers for the Review Mapping screen.

Everything here is DISPLAY and SUGGESTION support. Nothing in this module
decides what a mapping means, and nothing here is ever written to a mapping
file: a suggestion is shown, a person applies it (or does not).

  * labels / groups   a plain-word label for every tag and the order the return
                      uses the tags in (balance sheet first), for the coverage
                      panel on the right-hand side.
  * kind(tag)         how a tag relates to tax: inc / ded / paid (these change
                      tax), rep (reported, no tax), none (not in the return).
                      ONE helper for both sides of the screen.
  * book_key(path)    GnuCash tree order (each path segment compared in turn,
                      case-insensitively), so a parent's children stay together.
  * word_scores       rule-based scoring of an account name against the tags.
  * suggest           the top (at most 3) tags with reasons.

The suggestion rule is deliberately simple and explainable (no model):
  - a word of the account's own name that matches a tag's words scores 2
    (0.3 when the word is generic: tax, interest, income, ...);
  - a word of a parent segment scores 1 (0.2 when generic);
  - each other account under the same parent that already carries the tag adds
    1.5 once;
  - a tag needs at least 1.5, and at least half the best score; when the best
    tag has a word hit, tags with no word hit are dropped; at most 3 are kept.
HOLD is never suggested.
"""
from __future__ import annotations

import re

import tags as tag_vocab

STOP = frozenset({"on", "of", "the", "and", "a", "to", "for", "in", "account", "paid"})
GENERIC = frozenset({"tax", "interest", "income", "insurance", "expense", "asset",
                     "investment", "current"})

MIN_SCORE = 1.5
TOP_FRACTION = 0.5
MAX_SUGGESTIONS = 3
LEAF_WORD, LEAF_GENERIC = 2.0, 0.3
PARENT_WORD, PARENT_GENERIC = 1.0, 0.2
SIBLING_BONUS = 1.5

# Tax relationship classes (see kind()). Only these three change the tax.
TAXK = frozenset({"inc", "ded", "paid"})


def kind(tag: str | None) -> str | None:
    """None for no tag / HOLD; inc / ded / paid change tax; rep is reported
    without tax (exempt income, Schedule AL); none is not in the return."""
    if not tag or tag == tag_vocab.HOLD:
        return None
    if re.match(r"^(SALARY|HP_|BUS_|CG_|OS_)", tag):
        return "inc"
    if tag.startswith("DED_"):
        return "ded"
    if tag.startswith("TAXPAID_"):
        return "paid"
    if re.match(r"^(EXEMPT_|AL_)", tag):
        return "rep"
    return "none"


def affects_tax(tag: str | None) -> bool:
    return kind(tag) in TAXK


# ---------------------------------------------------------------------------
# Order the return uses the tags in, with plain-word labels.
# ---------------------------------------------------------------------------

# (section name, tags). Balance sheet first, then in the order the return uses
# them. Every tag in tags.TAGS, plus HOLD, appears exactly once (a test pins it).
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Schedule AL: assets", ("AL_IMMOVABLE", "AL_JEWELLERY", "AL_VEHICLE", "AL_CASH_BANK",
                             "AL_SECURITIES", "AL_INSURANCE_POLICY", "AL_LOANS_GIVEN",
                             "AL_FIRM_INTEREST")),
    ("Schedule AL: liabilities", ("AL_LIABILITY",)),
    ("Balance sheet, outside AL", ("EQUITY_CAPITAL", "TRADING")),
    ("Salary", ("SALARY_GROSS",)),
    ("House property", ("HP_RENT", "HP_MUNICIPAL_TAX", "HP_INTEREST")),
    ("Business / partnership", ("BUS_REMUNERATION", "BUS_EXPENSE")),
    ("Capital gains", ("CG_ST_CONTROL", "CG_LT_CONTROL")),
    ("Other sources", ("OS_INTEREST_SB", "OS_INTEREST_BANK", "OS_INTEREST_NBFC",
                       "OS_INTEREST_EPF_TAXABLE", "OS_REFUND_INTEREST", "OS_DIVIDEND", "OS_SLBS")),
    ("Exempt income (Schedule EI)", ("EXEMPT_10_2A", "EXEMPT_PPF_INTEREST",
                                     "EXEMPT_TAXFREE_BOND_INTEREST", "EXEMPT_OTHER")),
    ("Deductions (Chapter VI-A)", ("DED_80C_CANDIDATE", "DED_80D_CANDIDATE", "DED_80G_CANDIDATE")),
    ("Taxes paid", ("TAXPAID_TDS_SALARY", "TAXPAID_TDS_INTEREST", "TAXPAID_TDS_DIVIDEND",
                    "TAXPAID_TDS_PARTNER", "TAXPAID_TDS_OTHER", "TAXPAID_TCS", "TAXPAID_ADV",
                    "TAXPAID_SAT", "TAXPAID_FOREIGN")),
    ("Not in the return", ("PERSONAL", "NONTAX_REFUND_PRINCIPAL", tag_vocab.HOLD)),
)

TAG_LABELS: dict[str, str] = {
    "SALARY_GROSS": "Salary (gross)",
    "BUS_REMUNERATION": "Partner remuneration and interest on capital",
    "BUS_EXPENSE": "Business expense",
    "OS_INTEREST_SB": "Savings-bank interest",
    "OS_INTEREST_BANK": "FD / RD interest",
    "OS_INTEREST_NBFC": "Company / NBFC deposit interest",
    "OS_INTEREST_EPF_TAXABLE": "Taxable EPF interest",
    "OS_REFUND_INTEREST": "Interest on income-tax refund",
    "OS_DIVIDEND": "Dividends",
    "OS_SLBS": "Securities lending income",
    "NONTAX_REFUND_PRINCIPAL": "Tax refund (principal)",
    "CG_LT_CONTROL": "Long-term gains (books total)",
    "CG_ST_CONTROL": "Short-term gains (books total)",
    "EXEMPT_PPF_INTEREST": "PPF interest",
    "EXEMPT_10_2A": "Share of firm/LLP profit",
    "EXEMPT_TAXFREE_BOND_INTEREST": "Tax-free bond interest",
    "EXEMPT_OTHER": "Other exempt income",
    "HP_RENT": "Rent received from a tenant",
    "HP_MUNICIPAL_TAX": "Municipal / property tax",
    "HP_INTEREST": "Home-loan interest, s.24(b)",
    "TAXPAID_ADV": "Advance tax",
    "TAXPAID_SAT": "Self-assessment tax",
    "TAXPAID_TDS_SALARY": "TDS on salary",
    "TAXPAID_TDS_INTEREST": "TDS on interest",
    "TAXPAID_TDS_DIVIDEND": "TDS on dividend",
    "TAXPAID_TDS_PARTNER": "TDS on partner payments (194T)",
    "TAXPAID_TDS_OTHER": "TDS under another section",
    "TAXPAID_TCS": "TCS collected",
    "TAXPAID_FOREIGN": "Foreign tax withheld",
    "PERSONAL": "Personal spending / drawings",
    "DED_80C_CANDIDATE": "Life cover, school fees, ELSS (80C)",
    "DED_80D_CANDIDATE": "Health insurance (80D)",
    "DED_80G_CANDIDATE": "Donations (80G)",
    "AL_IMMOVABLE": "House, flat or land",
    "AL_JEWELLERY": "Jewellery, gold, art",
    "AL_VEHICLE": "Vehicle",
    "AL_SECURITIES": "Shares, mutual funds, PPF, bonds",
    "AL_INSURANCE_POLICY": "Insurance policy (cash value)",
    "AL_LOANS_GIVEN": "Loan you gave",
    "AL_CASH_BANK": "Bank account, FD or cash",
    "AL_FIRM_INTEREST": "Capital in a firm or LLP",
    "AL_LIABILITY": "Loan you owe / card due",
    "EQUITY_CAPITAL": "Opening balances / equity",
    "TRADING": "Trading account",
    tag_vocab.HOLD: "Not sure yet: hold",
}

# Words used ONLY for scoring an account name (search synonyms live in
# tags.TAG_SYNONYMS). Kept apart so adding a scoring word can never change what
# the picker's search finds.
SCORE_WORDS: dict[str, str] = {
    "SALARY_GROSS": "salary wage pay form16",
    "HP_RENT": "tenant let letout received",
    "HP_MUNICIPAL_TAX": "municipal property",
    "HP_INTEREST": "home housing mortgage emi",
    "BUS_REMUNERATION": "partner partnership remuneration capital llp firm",
    "BUS_EXPENSE": "business office professional",
    "CG_ST_CONTROL": "short stcg gain",
    "CG_LT_CONTROL": "long ltcg gain",
    "OS_INTEREST_SB": "savings sb",
    "OS_INTEREST_BANK": "fd rd fixed deposit recurring",
    "OS_INTEREST_NBFC": "nbfc company corporate",
    "OS_INTEREST_EPF_TAXABLE": "epf pf provident",
    "OS_REFUND_INTEREST": "refund",
    "OS_DIVIDEND": "dividend",
    "OS_SLBS": "slbs lending",
    "EXEMPT_10_2A": "share profit firm llp partnership",
    "EXEMPT_PPF_INTEREST": "ppf",
    "EXEMPT_TAXFREE_BOND_INTEREST": "taxfree bond",
    "EXEMPT_OTHER": "exempt gift agricultural",
    "DED_80C_CANDIDATE": "school tuition fee education life term lic elss endowment premium",
    "DED_80D_CANDIDATE": "medical health mediclaim hospital",
    "DED_80G_CANDIDATE": "donation charity",
    "TAXPAID_TDS_SALARY": "tds salary",
    "TAXPAID_TDS_INTEREST": "tds",
    "TAXPAID_TDS_DIVIDEND": "tds dividend",
    "TAXPAID_TDS_PARTNER": "tds partner partnership payment 194t llp firm",
    "TAXPAID_TDS_OTHER": "tds 194j 194c 194h 194i professional contract commission",
    "TAXPAID_TCS": "tcs collected",
    "TAXPAID_ADV": "advance",
    "TAXPAID_SAT": "self assessment",
    "TAXPAID_FOREIGN": "foreign withheld withholding abroad",
    "AL_IMMOVABLE": "flat house land plot property",
    "AL_JEWELLERY": "gold jewellery jewelry bullion silver art",
    "AL_VEHICLE": "car vehicle bike scooter",
    "AL_CASH_BANK": "bank fd fixed deposit cash savings hand",
    "AL_SECURITIES": "share stock equity mutual fund ppf nsc bond demat",
    "AL_INSURANCE_POLICY": "lic endowment policy ulip",
    "AL_LOANS_GIVEN": "loan given advance friend receivable",
    "AL_FIRM_INTEREST": "llp firm partner capital partnership",
    "AL_LIABILITY": "home loan credit card liability payable owe",
    "EQUITY_CAPITAL": "equity opening balance",
    "TRADING": "trading",
    "PERSONAL": "personal drawings household groceries rent fuel charge electricity repair car mobile phone club book",
    "NONTAX_REFUND_PRINCIPAL": "refund principal",
}


def stem(word: str) -> str:
    w = re.sub(r"[^a-z0-9]", "", word.lower())
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def words(text: str) -> list[str]:
    return [w for w in (stem(p) for p in re.split(r"[\s:()/,\-]+", text or "")) if w and w not in STOP]


_TAG_WORDS: dict[str, frozenset[str]] = {}


def tag_words(tag: str) -> frozenset[str]:
    """The stemmed words that identify `tag`: its search synonyms, its scoring
    words and its label."""
    got = _TAG_WORDS.get(tag)
    if got is None:
        got = frozenset(words(" ".join(tag_vocab.synonyms_for(tag))
                              + " " + SCORE_WORDS.get(tag, "") + " " + TAG_LABELS.get(tag, "")))
        _TAG_WORDS[tag] = got
    return got


# ---------------------------------------------------------------------------
# Paths and book order
# ---------------------------------------------------------------------------

def split_path(path: str | None) -> list[str]:
    """Path segments, leaf last. Accepts ':' (GnuCash) and '/' separators."""
    return [s.strip() for s in str(path or "").replace("/", ":").split(":") if s.strip()]


def book_key(path: str | None) -> tuple[str, ...]:
    """Sort key giving GnuCash tree order: segment by segment, case-insensitive,
    a parent before its children. Plain text order is wrong because a space
    sorts before ':' (so 'Car Insurance' lands between 'Car' and 'Car:Fuel')."""
    return tuple(s.lower() for s in split_path(path))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def word_scores(path: str | None, kind_name: str | None = None) -> dict[str, tuple[float, list[str]]]:
    """{tag: (score, matched words)} for the words of the account's own name and
    its parents, restricted to the tags valid for `kind_name` (None = no
    restriction). Zero scores are omitted. HOLD never appears."""
    segs = split_path(path)
    if not segs:
        return {}
    leaf, parents = segs[-1], segs[1:-1]
    allowed = tag_vocab.allowed_tags_for_kind(kind_name) if kind_name else None
    out: dict[str, tuple[float, list[str]]] = {}
    for tag in tag_vocab.TAGS:
        if allowed is not None and tag not in allowed:
            continue
        tw = tag_words(tag)
        score, hits = 0.0, []
        for w in words(leaf):
            if w in tw:
                if w in GENERIC:
                    score += LEAF_GENERIC
                else:
                    score += LEAF_WORD
                    hits.append(w)
        for w in words(" ".join(parents)):
            if w in tw:
                if w in GENERIC:
                    score += PARENT_GENERIC
                else:
                    score += PARENT_WORD
                    hits.append(w)
        if score > 0:
            out[tag] = (score, hits)
    return out


def suggest(path: str, kind_name: str | None = None,
            sibling_tags: list[str] | None = None) -> list[dict]:
    """Up to 3 suggestions [{tag, score, why}] for one account. `sibling_tags`
    is the tags of the OTHER accounts under the same parent (one entry each)."""
    allowed = tag_vocab.allowed_tags_for_kind(kind_name) if kind_name else None
    scores: dict[str, float] = {}
    hits: dict[str, list[str]] = {}
    for tag, (s, h) in word_scores(path, kind_name).items():
        scores[tag] = s
        hits[tag] = list(h)
    sib_count: dict[str, int] = {}
    for t in sibling_tags or ():
        if t and t != tag_vocab.HOLD and t in tag_vocab.TAGS and (allowed is None or t in allowed):
            sib_count[t] = sib_count.get(t, 0) + 1
    for t, n in sib_count.items():
        scores[t] = scores.get(t, 0.0) + SIBLING_BONUS
    segs = split_path(path)
    parent = segs[-2] if len(segs) > 1 else ""
    ranked = sorted((t for t, s in scores.items() if s >= MIN_SCORE),
                    key=lambda t: (-scores[t], list(tag_vocab.TAGS).index(t)))
    if not ranked:
        return []
    top = scores[ranked[0]]
    if hits.get(ranked[0]):
        ranked = [t for t in ranked if hits.get(t)]
    out = []
    for t in [t for t in ranked if scores[t] >= top * TOP_FRACTION][:MAX_SUGGESTIONS]:
        why = []
        uniq = list(dict.fromkeys(hits.get(t, [])))
        if uniq:
            why.append('matches "' + '", "'.join(uniq) + '"')
        if sib_count.get(t):
            n = sib_count[t]
            why.append(f"used by {n} other account{'s' if n > 1 else ''} under {parent}")
        out.append({"tag": t, "score": scores[t], "why": "; ".join(why[:2])})
    return out
