"""
agent.py -- Coverage-gap detector. DIRECT mode, no LLM, no network, read-only.

Closes a blind spot in skill_gnucash_pipeline's own opening-balance check:
`_reconcile_opening_balance()` there only ever sees ONE statement against
ONE account, so a month where NO statement was ever imported for an account
is invisible to it ("Scenario B ... cannot detect without prior statement").
This skill looks the other way -- across an account's whole transaction
history inside a book -- and infers likely-missing months purely from the
dates already posted, since this codebase has no import ledger to consult
(write_sidecar/read_sidecar in agents.canonical_io is per-file scratch, not
history; building an import ledger is explicitly out of scope for v1).

Algorithm, per in-scope account per selected book:
  1. Collect the dates of every split touching the account.
  2. The account's ACTIVE WINDOW starts at its first transaction date (not
     the FY start -- a mid-year account should not manufacture pre-genesis
     "gaps") and ends at the FY end or today, whichever is earlier.
  3. Every calendar month inside that window with zero transactions is a
     suspected gap. Months after the account's LAST transaction (but still
     inside the window) are a distinct, prominently labelled TRAILING gap --
     the single highest-value signal here, since it usually means "the most
     recent statement was never imported".
  4. Each account's MEDIAN monthly transaction count (opening-balance
     transactions excluded, since they skew the median) grades every zero
     month for that account: >= HIGH_CONFIDENCE_MEDIAN_THRESHOLD is HIGH
     confidence, below is LOW. Never suppressed -- both are reported, with
     the median shown alongside so the grading is auditable.
  5. A zero month that falls on an FY boundary (the book's first or last
     calendar month) is cheaply cross-checked against the adjacent FY's
     book, if the entity is registered and that book exists: transactions
     are sometimes filed into the "wrong side" of a financial-year rollover
     by mistake, and the adjacent book still holding a dated entry for this
     exact month is treated as proof the gap is a filing artefact, not a
     missing statement -- suppressed rather than reported.

Not attempted here: partial-month detection (a different signal, already
partly covered by skill_gnucash_pipeline's final_closing_balance_verdict()),
and auto-fetching anything -- this skill only ever reads.
"""
from __future__ import annotations

import gzip
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from agents.gnucash_accounts import GncAccount, load_accounts, postable_accounts

# Reuse the Inter-entity Matrix's own reuse target: reconcile_intercompany's
# filename-based owner/FY derivation, as the FALLBACK label for a book that
# isn't registered to any entity in entities.yaml (see _resolve_entity()).
_PAIR_SCRIPTS = (Path(__file__).resolve().parents[1]
                 / "skill_gnucash_intercompany" / "scripts")
if str(_PAIR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PAIR_SCRIPTS))

from reconcile_intercompany import derive_owner_and_fy  # noqa: E402

# ---------------------------------------------------------------------------
# GnuCash XML namespaces -- mirrors skill_gnucash_pipeline/agent.py's own
# _NS dict (not imported from there: that module's parsing helpers are
# single-account/bank-name-matching and not a fit here; the shared,
# type/flag-aware reader is agents.gnucash_accounts, used above instead).
# ---------------------------------------------------------------------------
_NS = {
    'gnc': '{http://www.gnucash.org/XML/gnc}',
    'trn': '{http://www.gnucash.org/XML/trn}',
    'split': '{http://www.gnucash.org/XML/split}',
    'ts': '{http://www.gnucash.org/XML/ts}',
}

# Both bank-side (BANK/ASSET) and credit-card-side (LIABILITY/CREDIT)
# accounts are in scope -- month-bucketing is type-agnostic, but the account
# class is carried through to the report so the two families of account
# stay visually distinguishable.
_BANK_TYPES = ("BANK", "ASSET")
_CREDIT_TYPES = ("CREDIT", "LIABILITY")
SCOPE_TYPES = frozenset(_BANK_TYPES) | frozenset(_CREDIT_TYPES)

# Named, module-level, tunable: >= this many non-opening-balance
# transactions per month (median over the account's active window) grades a
# zero month HIGH confidence; below it, LOW. Never used to suppress a gap,
# only to grade it -- see module docstring point 4.
HIGH_CONFIDENCE_MEDIAN_THRESHOLD = 4

_FY_RE = re.compile(r"^(\d{4})-(\d{2})$")


# ---------------------------------------------------------------------------
# XML reading
# ---------------------------------------------------------------------------

def _read_root(path: Path) -> Optional[ET.Element]:
    """Parse a .gnucash file (gzipped or plain XML); None if unreadable.
    Read-only -- never touches the file otherwise, and proceeds even if a
    sibling .LCK (book open in GnuCash) is present, matching the read-only
    convention already used by skill_gnucash_intercompany's load_book()."""
    try:
        raw = path.read_bytes()
        data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        return ET.fromstring(data)
    except Exception:
        return None


def _collect_account_dates(root: ET.Element, ob_ids: frozenset) -> dict:
    """{account_id: [(date_str YYYY-MM-DD, involves_opening_balance), ...]}
    for every split in the book. A transaction "involves_opening_balance" if
    ANY of its splits touches the book's opening-balance Equity account
    (identified via agents.gnucash_accounts' equity-type KVP-slot flag,
    never by string-matching a description -- raw GnuCash transactions carry
    no guaranteed "Opening Balance" text)."""
    result: dict = defaultdict(list)
    for trn in root.findall(f'.//{_NS["gnc"]}transaction'):
        date_el = trn.find(f'{_NS["trn"]}date-posted/{_NS["ts"]}date')
        if date_el is None or not date_el.text:
            continue
        trn_date = date_el.text[:10]
        splits = trn.findall(f'{_NS["trn"]}splits/{_NS["trn"]}split')
        split_accounts = [sp.findtext(f'{_NS["split"]}account', '') for sp in splits]
        involves_ob = any(a in ob_ids for a in split_accounts)
        for acc_id in split_accounts:
            if acc_id:
                result[acc_id].append((trn_date, involves_ob))
    return result


# ---------------------------------------------------------------------------
# Date/month helpers
# ---------------------------------------------------------------------------

def _month_key(date_str: str) -> str:
    return date_str[:7]


def _parse_date(date_str: str) -> date:
    return datetime.strptime(date_str[:10], "%Y-%m-%d").date()


def _month_range(start_month: str, end_month: str) -> list[str]:
    """Inclusive list of "YYYY-MM" strings from start_month to end_month."""
    y, m = int(start_month[:4]), int(start_month[5:7])
    ey, em = int(end_month[:4]), int(end_month[5:7])
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _fy_bounds(fy_key: Optional[str]) -> Optional[tuple]:
    """"2025-26" -> (date(2025,4,1), date(2026,3,31)); None if unparseable."""
    if not fy_key:
        return None
    m = _FY_RE.match(fy_key)
    if not m:
        return None
    start_year = int(m.group(1))
    end_year = start_year + 1
    if end_year % 100 != int(m.group(2)):
        return None
    return date(start_year, 4, 1), date(end_year, 3, 31)


def _adjacent_fy_key(fy_key: str, direction: str) -> Optional[str]:
    m = _FY_RE.match(fy_key or "")
    if not m:
        return None
    a = int(m.group(1))
    na = a + 1 if direction == "next" else a - 1
    if na < 0:
        return None
    return f"{na:04d}-{str(na + 1)[-2:]}"


# ---------------------------------------------------------------------------
# Entity resolution (needed for both the report's "entity" column AND the
# FY-boundary adjacent-book consultation, which requires an entity_key --
# see AGENT.md "Decisions the brief left open" for why this reverse-lookup
# was chosen over reusing derive_owner_and_fy() as the primary path).
# ---------------------------------------------------------------------------

def _resolve_entity(book_path: Path, entities_path: Optional[Path]) -> tuple:
    """(entity_label, entity_key_or_None, fy_key_or_None) for a book path.

    Tries an exact-path match against every entity's registered `books` in
    entities.yaml first: this gives both a clean display name AND the
    entity_key that ui._book_registry.list_books() needs for FY-boundary
    consultation. Falls back to the Inter-entity Matrix's own filename-based
    derive_owner_and_fy() when the book isn't registered to any entity --
    in that case entity_key is None, so FY-boundary consultation is simply
    skipped for that book (there's no registry entry to consult)."""
    resolved = None
    try:
        resolved = book_path.resolve()
    except OSError:
        pass

    if entities_path is not None:
        try:
            from configs import load_entities  # noqa: PLC0415
            if entities_path.is_file():
                entities = load_entities(entities_path)
                for key, profile in entities.items():
                    for fy, raw_path in (profile.books or {}).items():
                        try:
                            if resolved is not None and Path(raw_path).resolve() == resolved:
                                return (profile.name or key), key, fy
                        except OSError:
                            continue
        except Exception:
            pass

    owner, fy_tuple = derive_owner_and_fy(book_path)
    fy_key = f"{fy_tuple[0]:04d}-{str(fy_tuple[1])[-2:]}" if fy_tuple else None
    return owner, None, fy_key


def _ensure_itr_scripts_importable() -> None:
    """Same mechanism ui._book_registry uses -- puts the ITR Workbook
    skill's flat scripts/ dir on sys.path so `import configs` and
    `from ui import _book_registry` both resolve, frozen-build-safe."""
    import agents.skill_itr_workbook as _itr_pkg  # noqa: PLC0415
    scripts_dir = Path(_itr_pkg.__file__).resolve().parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))


def _account_class(acc_type: str) -> str:
    return "Bank/Asset" if acc_type in _BANK_TYPES else "Credit/Liability"


# ---------------------------------------------------------------------------
# Per-account gap detection
# ---------------------------------------------------------------------------

@dataclass
class GapRow:
    entity: str
    book: str
    account_path: str
    account_class: str
    month: str
    confidence: str
    median: float
    trailing: bool


@dataclass
class AccountStat:
    entity: str
    book: str
    account_path: str
    account_class: str
    first_date: str
    last_date: str
    window_end: str
    median: float
    confidence: str
    zero_months: int
    trailing_zero_months: int
    suppressed_boundary_gaps: int


def _boundary_has_adjacent_evidence(
    month: str, fy_key: str, fy_start_month: str, fy_end_month: str,
    entity_key: str, entities_path: Optional[Path],
    account_path: str, this_book_path: Path,
) -> bool:
    """True if the ADJACENT FY's registered book (for the same entity) also
    holds a dated entry, for the same account, falling in this exact
    calendar month -- treated as proof a statement exists but was filed
    into the wrong side of the FY rollover, so the gap is suppressed rather
    than reported. Only ever called for the book's own FY-boundary months
    (its first or last calendar month), per the brief's "e.g. March"
    example. Cheap: only reads one extra book, and only on a boundary hit."""
    if month == fy_end_month:
        direction = "next"
    elif month == fy_start_month:
        direction = "previous"
    else:
        return False

    adj_key = _adjacent_fy_key(fy_key, direction)
    if not adj_key:
        return False

    try:
        _ensure_itr_scripts_importable()
        from ui import _book_registry as reg  # noqa: PLC0415
    except Exception:
        return False

    adj_books = reg.list_books(entity_key, entities_path=entities_path)
    adj_raw = adj_books.get(adj_key)
    if not adj_raw:
        return False
    adj_path = Path(adj_raw)
    try:
        if not adj_path.is_file() or adj_path.resolve() == this_book_path.resolve():
            return False
    except OSError:
        return False

    adj_root = _read_root(adj_path)
    if adj_root is None:
        return False
    adj_accounts = load_accounts(adj_path)
    match = next((a for a in adj_accounts if a.path == account_path), None)
    if match is None:
        return False
    adj_ob_ids = frozenset(a.id for a in adj_accounts if "opening-balance" in a.special_flags)
    adj_dates = _collect_account_dates(adj_root, adj_ob_ids)
    return any(d[:7] == month for d, _ob in adj_dates.get(match.id, []))


def _process_account(
    account: GncAccount, dates_with_ob: list, fy_key: Optional[str], today: date,
    entity_key: Optional[str], entities_path: Optional[Path], book_path: Path,
) -> Optional[tuple]:
    """Returns (list[gap dict], suppressed_count, median, first, last,
    window_end_str) for one account, or None if it has no transactions at
    all (nothing to report -- an account that was never used is not a
    coverage gap, it's simply unused)."""
    if not dates_with_ob:
        return None

    dates_all = sorted(d for d, _ob in dates_with_ob)
    dates_non_ob = sorted(d for d, ob in dates_with_ob if not ob)
    first_str, last_str = dates_all[0], dates_all[-1]

    fy_bounds = _fy_bounds(fy_key)
    if fy_bounds:
        _fy_start, fy_end = fy_bounds
        window_end = min(fy_end, today)
    else:
        window_end = today

    first_date_obj = _parse_date(first_str)
    if first_date_obj > window_end:
        return None  # account's first transaction is after the window closed

    months = _month_range(_month_key(first_str), _month_key(window_end.isoformat()))
    counts_all = Counter(_month_key(d) for d in dates_all)
    counts_non_ob = Counter(_month_key(d) for d in dates_non_ob)

    # Median is computed over EVERY month in the active window (zero months
    # included) using the opening-balance-EXCLUDED counts, per the brief:
    # opening-balance transactions would otherwise skew a normally-quiet
    # account's cadence upward in its genesis month.
    month_counts_non_ob = [counts_non_ob.get(m, 0) for m in months]
    median = statistics.median(month_counts_non_ob) if month_counts_non_ob else 0.0
    confidence = "HIGH" if median >= HIGH_CONFIDENCE_MEDIAN_THRESHOLD else "LOW"

    last_month = _month_key(last_str)
    fy_start_month = f"{fy_bounds[0].year:04d}-04" if fy_bounds else None
    fy_end_month = f"{fy_bounds[1].year:04d}-03" if fy_bounds else None

    gaps = []
    suppressed = 0
    trailing_zero_months = 0
    for m in months:
        if counts_all.get(m, 0) > 0:
            continue
        is_trailing = m > last_month
        if is_trailing:
            trailing_zero_months += 1
        if fy_key and entity_key and m in (fy_start_month, fy_end_month):
            if _boundary_has_adjacent_evidence(
                m, fy_key, fy_start_month, fy_end_month,
                entity_key, entities_path, account.path, book_path,
            ):
                suppressed += 1
                continue
        gaps.append({"month": m, "trailing": is_trailing})

    return gaps, suppressed, median, confidence, first_str, last_str, window_end.isoformat(), trailing_zero_months


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(
    books,
    output_path: str,
    entities_path: str = "Data/itr/entities.yaml",
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Scan every selected .gnucash book for suspected coverage gaps (calendar
    months inside an account's active window with zero transactions) and
    write one consolidated workbook to output_path. Returns a text summary
    for the UI. Read-only -- never writes to or modifies a .gnucash file.

    `books` is either a list of .gnucash paths or a single newline-separated
    string of them (the UI's multi-book field is a path textbox holding one
    path per line, and run_args substitution makes every kwarg a string on
    the way in -- same convention as the Inter-entity Matrix skill).
    """
    from excel_writer import write_gaps_workbook  # noqa: PLC0415
    _this_dir = Path(__file__).resolve().parent
    if str(_this_dir) not in sys.path:
        sys.path.insert(0, str(_this_dir))
        from excel_writer import write_gaps_workbook  # noqa: PLC0415,F811

    if isinstance(books, str):
        paths = [ln.strip() for ln in books.splitlines() if ln.strip()]
    else:
        paths = [str(b).strip() for b in (books or []) if str(b).strip()]
    paths = list(dict.fromkeys(paths))
    if not paths:
        return "ERROR: select at least one .gnucash book."

    entities_path_obj = Path(entities_path) if entities_path else None
    today = date.today()

    all_gaps: list[GapRow] = []
    all_stats: list[AccountStat] = []
    warnings: list[str] = []
    books_scanned = 0

    for p in paths:
        book_path = Path(p)
        if not book_path.is_file():
            warnings.append(f"WARNING: book not found, skipped: {p}")
            continue
        root = _read_root(book_path)
        if root is None:
            warnings.append(f"WARNING: could not read as a GnuCash book, skipped: {p}")
            continue

        books_scanned += 1
        accounts = load_accounts(book_path)
        in_scope = [a for a in postable_accounts(accounts) if a.type in SCOPE_TYPES]
        ob_ids = frozenset(a.id for a in accounts if "opening-balance" in a.special_flags)
        acc_dates = _collect_account_dates(root, ob_ids)
        entity_label, entity_key, fy_key = _resolve_entity(book_path, entities_path_obj)

        for account in in_scope:
            dates_with_ob = acc_dates.get(account.id, [])
            result = _process_account(
                account, dates_with_ob, fy_key, today,
                entity_key, entities_path_obj, book_path,
            )
            if result is None:
                continue
            (gaps, suppressed, median, confidence, first_str, last_str,
             window_end_str, trailing_zero_months) = result

            for g in gaps:
                all_gaps.append(GapRow(
                    entity=entity_label, book=book_path.name,
                    account_path=account.path,
                    account_class=_account_class(account.type),
                    month=g["month"], confidence=confidence, median=median,
                    trailing=g["trailing"],
                ))
            all_stats.append(AccountStat(
                entity=entity_label, book=book_path.name,
                account_path=account.path,
                account_class=_account_class(account.type),
                first_date=first_str, last_date=last_str,
                window_end=window_end_str, median=median,
                confidence=confidence,
                zero_months=len(gaps) + suppressed,
                trailing_zero_months=trailing_zero_months,
                suppressed_boundary_gaps=suppressed,
            ))

    write_gaps_workbook(all_gaps, all_stats, output_path)

    high = sum(1 for g in all_gaps if g.confidence == "HIGH")
    low = sum(1 for g in all_gaps if g.confidence == "LOW")
    trailing = sum(1 for g in all_gaps if g.trailing)
    suppressed_total = sum(s.suppressed_boundary_gaps for s in all_stats)

    lines = [
        f"Coverage-gap scan -- {books_scanned} book(s), "
        f"{len(all_stats)} account(s) with transaction history.",
        f"  Suspected gaps: {len(all_gaps)}  (HIGH confidence: {high}, LOW confidence: {low})",
        f"  Trailing gaps (since last transaction): {trailing}",
    ]
    if suppressed_total:
        lines.append(
            f"  FY-boundary gaps suppressed via adjacent-book evidence: {suppressed_total}"
        )
    lines.extend(warnings)
    lines.append(f"  Workbook: {output_path}")
    return "\n".join(lines)
