"""
tds_learnings.py — persisted human confirmations for Form 26AS deductor/
collector credit accounts (26AS Journal skill's Review tab).

Mirrors agents.skill_gnucash_account_mapper.persistent_rules in spirit (a
year-less YAML sidecar next to the GnuCash book, holding only
human-CONFIRMED data, never anything auto-generated) but adapted to this
domain's much simpler matching model: a 26AS deductor/collector is looked up
by an EXACT key -- its TAN (Tax-deduction/collection Account Number, e.g.
"AAAA00000A"), falling back to a normalised name when no TAN was parsed --
rather than persistent_rules' regex-matched transaction Description. 26AS
party names are short, stable header strings straight off the department's
own workbook, not free-text bank narrations, so regex generalisation would
buy nothing here and would only risk a wrong match.

Learnings are namespaced by matching DOMAIN, not just by TAN, because the
same TAN can legitimately appear as both a Part I/II *deductor* (the credit
leg is an INCOME account -- Categories A/B/C/G all search the income
subtree, see match_credit_account()) and a Part VI *collector* (the credit
leg is a spending-side contra account, e.g. Drawings or Bank -- Category T,
see build_tcs_journals()). Sharing one namespace across both would let a
human's TDS-income confirmation silently redirect an unrelated TCS
contra-account pick just because the same bank happens to hold both roles.
DOMAIN_INCOME covers A/B/C/G; DOMAIN_TCS covers T.

This module is a SELF-CONTAINED sibling of build_tds_journals.py -- it must
never import from the `agents` package. build_tds_journals.py runs as a
subprocess (see its module docstring, and tools.py's _run_script()) and
cannot rely on `agents` being importable in a frozen child.

File format (YAML), one sidecar per book, year-less so it survives an FY
roll onto next year's book:

    _learnings:
      - key: "income:AAAA00000A"
        account: "Income:Interest Income:Interest on Zenith Bank"
        source: "user"
        added: "2026-09-25"
      - key: "tcs:name:SOME TRAVEL AGENCY"
        account: "Assets:Bank:HDFC Savings"
        source: "user"
        added: "2026-09-25"

Only a genuinely human Review-tab save (ui/tabs/tds_journal_review.py's
_save_changes()) ever calls save_learning()/save_learnings_batch() -- an
LLM/model-picked override accepted via agent.py must NEVER be persisted
here. See FL1.2 (build_tds_journals.py's build_journals() et al.): an
accepted override still carries needs_review=True precisely so it is not
mistaken for a confirmed pick; silently turning it into a learning here
would undo that same protection one layer down.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

LEARNINGS_KEY = "_learnings"
SOURCE_USER = "user"

DOMAIN_INCOME = "income"  # Categories A/B/C/G -- credit leg is an income account
DOMAIN_TCS = "tcs"        # Category T -- credit leg is a spending-side contra account

_TRAILING_YEAR_DIGITS_RE = re.compile(r"\d+$")
_WS_RE = re.compile(r"\s+")

# Same shape as skill_26as/scripts/extract_26as_to_xlsx.py's TAN_RE --
# restated here (not imported; see the module docstring) purely to
# sanity-check a parsed TAN before trusting it as a key. A TAN that doesn't
# match this shape is treated as absent (falls back to the name key) rather
# than trusted verbatim -- a misread cell (e.g. a stray header/footer row)
# must not silently become a bogus learning key.
_TAN_RE = re.compile(r"^[A-Z]{4}\d{5}[A-Z]$")


def _yearless_stem(stem: str) -> str:
    """Strip a trailing run of digits (year/FY suffix) from a book stem."""
    return _TRAILING_YEAR_DIGITS_RE.sub("", stem)


def learnings_path(gnucash_file: str) -> Path:
    """Resolve the persisted-learnings YAML sidecar for a GnuCash book.

    Year-less stem, co-located with the book -- same convention as
    persistent_rules.rules_path(), simplified: this module has no
    config_path/Data-tree-scan available to it (build_tds_journals.py's
    run() only ever receives the gnucash path itself), so a temp-directory
    book falls back to sitting next to the temp file rather than being
    relocated to the original book's directory. That matches this module's
    much lower stakes than the account-mapper's rules file -- a lost
    learning just means the next run asks the human to confirm the same
    deductor again, not a parser assumption failing silently.
    """
    p = Path(gnucash_file)
    yaml_name = f"{_yearless_stem(p.stem)}_26as_learnings.yaml"
    return p.parent / yaml_name


def normalize_name(name: str) -> str:
    """Normalise a deductor/collector name for use as a fallback key.

    Uppercased and whitespace-collapsed only -- deliberately NOT run through
    build_tds_journals.py's STOPWORDS/token/alias machinery. That machinery
    exists to find a FUZZY match against an unrelated account chart; a
    learnings key needs the opposite property, an EXACT, stable identity for
    the same 26AS party across runs. The department's own header text for
    one party is consistent letter-for-letter run to run; case and
    incidental spacing are the only real noise.
    """
    return _WS_RE.sub(" ", (name or "").strip().upper())


def deductor_key(domain: str, tan: str, name: str) -> str:
    """Build the learnings key for one deductor/collector.

    TAN wins whenever present and well-formed (see _TAN_RE) -- it is the
    department's own unique identifier, immune to a deductor being listed
    under slightly different name text across two 26AS downloads. Falls back
    to the normalised name only when no valid TAN was parsed for this row.
    """
    tan = (tan or "").strip().upper()
    if _TAN_RE.match(tan):
        return f"{domain}:{tan}"
    return f"{domain}:name:{normalize_name(name)}"


def _load_raw_rows(rp: Path) -> List[Dict]:
    if not rp.is_file():
        return []
    try:
        data = yaml.safe_load(rp.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    rows = data.get(LEARNINGS_KEY)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def load_learnings(gnucash_file: str) -> Dict[str, str]:
    """Return {key -> credit account path} for every persisted learning.

    Missing/empty/corrupt sidecar -> {} (never raises): a learnings file is
    an optimisation, not a required input, and a bad YAML must not block a
    build the way a bad 26AS workbook would.
    """
    rp = learnings_path(gnucash_file)
    out: Dict[str, str] = {}
    for row in _load_raw_rows(rp):
        key = row.get("key")
        account = row.get("account")
        if key and account:
            out[str(key)] = str(account)
    return out


def save_learning(gnucash_file: str, key: str, account: str) -> None:
    """Persist (or update) one learning: `key` -> `account`.

    Only ever called from a HUMAN Review-tab save (see the module
    docstring's FL1.2 note) -- never from the LLM-override path. An existing
    row with the same key is replaced (a human correcting their own earlier
    confirmation should win, not accumulate a stale duplicate).
    """
    save_learnings_batch(gnucash_file, [(key, account)])


def save_learnings_batch(gnucash_file: str, items: List[Tuple[str, str]]) -> None:
    """Persist several (key, account) pairs in one file write.

    Behaves like calling save_learning() once per item (last write for a
    repeated key wins) but touches disk once, for the review tab's
    multi-row Save. Blank/falsy keys or accounts are silently skipped
    (mirrors _apply_changes' own blank-input guard -- nothing here is a
    hard error, a skipped learning is just a missed optimisation).
    """
    items = [(k, a) for k, a in items if k and a]
    if not items:
        return
    rp = learnings_path(gnucash_file)
    rows = _load_raw_rows(rp)
    by_key = {r.get("key"): r for r in rows if r.get("key")}
    today = date.today().isoformat()
    for key, account in items:
        by_key[key] = {
            "key": key,
            "account": account,
            "source": SOURCE_USER,
            "added": today,
        }
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(
        yaml.safe_dump({LEARNINGS_KEY: list(by_key.values())}, sort_keys=False,
                       allow_unicode=True),
        encoding="utf-8",
    )
