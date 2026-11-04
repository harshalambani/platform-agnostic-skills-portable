"""
agent.py -- Coverage-gap detector. DIRECT mode, no LLM, no network, read-only.

Closes a blind spot in skill_gnucash_pipeline's own opening-balance check:
`_reconcile_opening_balance()` there only ever sees ONE statement against
ONE account, so a month where NO statement was ever imported for an account
is invisible to it ("Scenario B ... cannot detect without prior statement").
This skill looks the other way -- across an account's transactions inside a
book -- and infers likely-missing months purely from the dates already
posted, since this codebase has no import ledger to consult.

Algorithm, per in-scope account per selected book:
  1. SCOPE. Only accounts that receive a monthly statement: GnuCash types
     BANK and CREDIT. An opt-in (`include_other`) also checks ASSET and
     LIABILITY accounts, reported in their own labelled section. Hidden,
     placeholder and no-transaction accounts are never checked.
  2. WINDOW. Only the book's own financial year (1 Apr - 31 Mar), capped at
     today. The year comes from the entity registry (or the filename); a
     book that resolves to no year uses the year of its latest transaction
     and says so. The window NEVER starts at the account's first-ever
     transaction, so history from earlier years cannot leak in. If the
     account's first transaction falls inside the year, months before it
     are not "missing".
  3. A month in the window with zero transactions is a gap. Months after the
     account's last transaction are reported first, as "no transactions
     since <Mon YYYY>".
  4. A "quiet" account (fewer than QUIET_TXNS_PER_MONTH transactions a month
     on average within the year, opening-balance entries excluded) is told
     to the user as "may be months with no activity - check before importing".
  5. A gap month on an FY boundary (the year's first or last month) is
     cross-checked against the adjacent FY's registered book for the same
     entity/account: postings are sometimes filed on the wrong side of a
     year rollover, and if the adjacent book holds a dated entry for that
     exact month the gap is suppressed rather than reported.

Not attempted here: partial-month detection and auto-fetching anything --
this skill only ever reads.
"""
from __future__ import annotations

import gzip
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from agents.gnucash_accounts import GncAccount, load_accounts, postable_accounts
from agents.skill_gnucash_coverage import excel_writer as XL

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

# Statement accounts: a bank account or a credit card receives a monthly
# statement, so an empty month is a real signal. Everything else (loans,
# deposits, investments, fixed assets, tax payables, Suspense) does not.
CORE_TYPES = frozenset({"BANK", "CREDIT"})
# Opt-in extras (`include_other`): reported in their own labelled section.
OTHER_TYPES = frozenset({"ASSET", "LIABILITY"})
SCOPE_TYPES = CORE_TYPES | OTHER_TYPES

SECTION_CORE = "Bank and card accounts"
SECTION_OTHER = ("Other accounts - no monthly statement is expected, "
                 "so empty months here are often normal")

# An account with fewer than this many transactions a month on average
# (opening-balance entries excluded) inside the checked year is "quiet": an
# empty month may simply be a month with no activity.
QUIET_TXNS_PER_MONTH = 1.0

_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

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



# ---------------------------------------------------------------------------
# Plain-language wording (shared by the reply text and the workbook)
# ---------------------------------------------------------------------------

def month_label(month_key: str) -> str:
    """"2025-06" -> "Jun 2025"."""
    return f"{_MONTH_NAMES[int(month_key[5:7]) - 1]} {month_key[:4]}"


def _months_text(month_keys: list) -> str:
    return ", ".join(month_label(m) for m in month_keys)


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def fy_title(fy_key: str) -> str:
    return f"FY {fy_key}"


def fy_span_text(fy_key: str) -> str:
    b = _fy_bounds(fy_key)
    if not b:
        return ""
    return f"{month_label(b[0].isoformat()[:7])} - {month_label(b[1].isoformat()[:7])}"


# ---------------------------------------------------------------------------
# Per-account gap detection
# ---------------------------------------------------------------------------

@dataclass
class AccountResult:
    entity: str
    book: str
    account_path: str
    section: str                 # SECTION_CORE or SECTION_OTHER
    fy_key: str
    txns_in_fy: int
    months_checked: int
    months_with_txns: int
    first_in_fy: str             # "" if no transaction inside the year
    last_in_fy: str
    last_month: str              # YYYY-MM of the last transaction up to the window end
    gap_months: list = field(default_factory=list)       # empty months before the last transaction
    trailing_months: list = field(default_factory=list)  # empty months after the last transaction
    quiet: bool = False
    suppressed: int = 0          # boundary months shown to be filed in the adjacent book

    @property
    def is_core(self) -> bool:
        return self.section == SECTION_CORE

    @property
    def has_gaps(self) -> bool:
        return bool(self.gap_months or self.trailing_months)

    @property
    def months_text(self) -> str:
        """Every reported empty month, months after the last transaction first."""
        return _months_text(list(self.trailing_months) + list(self.gap_months))

    @property
    def meaning(self) -> str:
        parts = []
        if self.trailing_months:
            since = month_label(self.last_month)
            if self.quiet:
                parts.append(f"No transactions since {since}.")
            else:
                parts.append(f"No transactions since {since} - the latest "
                             f"statement(s) probably not imported.")
        if self.gap_months and not self.quiet:
            parts.append("Statement probably not imported."
                         if not self.trailing_months else
                         "Statement probably not imported for the earlier empty months.")
        if self.quiet:
            parts.append("This account is quiet, so these may be months with "
                         "no activity - check before importing.")
        return " ".join(parts)

    def reply_line(self, show_book: bool) -> str:
        where = f" [{self.book}]" if show_book else ""
        return f"  - {self.account_path}{where}: {self.months_text}. {self.meaning}"


@dataclass
class BookSummary:
    fy_key: str
    inferred: bool
    capped_to: str               # YYYY-MM when the year is cut short by today, else ""
    core_checked: int = 0
    core_with_gaps: int = 0
    other_checked: int = 0
    other_with_gaps: int = 0


@dataclass
class ScanResult:
    results: list
    summaries: list
    warnings: list
    books_scanned: int
    include_other: bool

    def opening_line(self) -> str:
        """One plain sentence per financial year covered."""
        merged: dict = {}
        for s in self.summaries:
            key = (s.fy_key, s.inferred, s.capped_to)
            m = merged.setdefault(key, BookSummary(*key))
            m.core_checked += s.core_checked
            m.core_with_gaps += s.core_with_gaps
            m.other_checked += s.other_checked
            m.other_with_gaps += s.other_with_gaps
        if not merged:
            return "No bank or card accounts could be checked."
        sentences = []
        for (fy_key, inferred, capped_to), s in merged.items():
            span = fy_span_text(fy_key)
            if capped_to:
                span += f", checked up to {month_label(capped_to)}"
            head = (f"{_plural(s.core_checked, 'bank and card account', 'bank and card accounts')} "
                    f"checked for {fy_title(fy_key)} ({span})")
            if s.core_with_gaps == 0:
                tail = "no account has months with no transactions."
            elif s.core_with_gaps == 1:
                tail = "1 has months with no transactions."
            else:
                tail = f"{s.core_with_gaps} have months with no transactions."
            text = f"{head}; {tail}"
            if self.include_other:
                text += (f" Also checked {_plural(s.other_checked, 'other account', 'other accounts')}"
                         f"; {s.other_with_gaps} with empty months (listed separately).")
            if inferred:
                text += (" This book is not registered to a financial year, so the "
                         "year of its latest transaction was used.")
            sentences.append(text)
        return " ".join(sentences)

    def sections(self) -> list:
        """[(section title, [AccountResult with gaps])] -- trailing gaps first."""
        out = []
        titles = [SECTION_CORE] + ([SECTION_OTHER] if self.include_other else [])
        for title in titles:
            rows = [r for r in self.results if r.section == title and r.has_gaps]
            rows.sort(key=lambda r: (not r.trailing_months, r.entity, r.book, r.account_path))
            out.append((title, rows))
        return out

    def reply_text(self, output_path: str) -> str:
        show_book = self.books_scanned > 1
        lines = [self.opening_line()]
        any_gap = False
        for title, rows in self.sections():
            if not rows:
                continue
            any_gap = True
            lines.append("")
            lines.append(f"{title}:")
            lines.extend(r.reply_line(show_book) for r in rows)
        if not any_gap:
            lines.append("No account has months with no transactions.")
        suppressed = sum(r.suppressed for r in self.results)
        if suppressed:
            lines.append("")
            lines.append(f"{_plural(suppressed, 'month', 'months')} left out because the "
                         f"neighbouring year's book has transactions for that month "
                         f"(filed in the wrong year).")
        if self.warnings:
            lines.append("")
            lines.extend(self.warnings)
        lines.append("")
        lines.append(f"Workbook: {output_path}")
        return "\n".join(lines)


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
    (its first or last calendar month). Cheap: only reads one extra book,
    and only on a boundary hit."""
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
    account: GncAccount, dates_with_ob: list, fy_key: str,
    fy_start: date, fy_end: date, window_end: date,
    entity_label: str, entity_key: Optional[str],
    entities_path: Optional[Path], book_path: Path,
) -> Optional[AccountResult]:
    """Check one account over the financial year only. None if it has no
    transaction on or before the window end (an unused account is not a
    coverage gap, it is simply unused)."""
    window_end_iso = window_end.isoformat()
    dates = sorted(d for d, _ob in dates_with_ob if d <= window_end_iso)
    if not dates:
        return None

    first_ever, last_str = dates[0], dates[-1]
    fy_start_iso = fy_start.isoformat()
    in_fy = [d for d in dates if d >= fy_start_iso]
    non_ob_in_fy = [d for d, ob in dates_with_ob
                    if not ob and fy_start_iso <= d <= window_end_iso]

    # The window is the financial year only. The one exception to "starts on
    # 1 April": an account whose first transaction is inside the year is not
    # missing the months before it.
    start_month = max(_month_key(first_ever), _month_key(fy_start_iso))
    months = _month_range(start_month, _month_key(window_end_iso))
    if not months:
        return None

    counts = Counter(_month_key(d) for d in in_fy)
    last_month = _month_key(last_str)
    fy_start_month = f"{fy_start.year:04d}-04"
    fy_end_month = f"{fy_end.year:04d}-03"

    gap_months, trailing_months, suppressed = [], [], 0
    for m in months:
        if counts.get(m, 0) > 0:
            continue
        if entity_key and m in (fy_start_month, fy_end_month):
            if _boundary_has_adjacent_evidence(
                m, fy_key, fy_start_month, fy_end_month,
                entity_key, entities_path, account.path, book_path,
            ):
                suppressed += 1
                continue
        (trailing_months if m > last_month else gap_months).append(m)

    return AccountResult(
        entity=entity_label, book=book_path.name, account_path=account.path,
        section=SECTION_CORE if account.type in CORE_TYPES else SECTION_OTHER,
        fy_key=fy_key,
        txns_in_fy=len(in_fy), months_checked=len(months),
        months_with_txns=sum(1 for m in months if counts.get(m, 0) > 0),
        first_in_fy=in_fy[0] if in_fy else "",
        last_in_fy=in_fy[-1] if in_fy else "",
        last_month=last_month,
        gap_months=gap_months, trailing_months=trailing_months,
        quiet=(len(non_ob_in_fy) / len(months)) < QUIET_TXNS_PER_MONTH,
        suppressed=suppressed,
    )


def _fy_key_of_latest_transaction(acc_dates: dict, today: date) -> Optional[str]:
    """FY key ("2025-26") of the book's latest transaction on or before
    today (or the latest overall if every transaction is post-dated)."""
    all_d = [d for lst in acc_dates.values() for d, _ob in lst]
    if not all_d:
        return None
    past = [d for d in all_d if d <= today.isoformat()]
    latest = max(past) if past else max(all_d)
    y, m = int(latest[:4]), int(latest[5:7])
    start_year = y if m >= 4 else y - 1
    return f"{start_year:04d}-{str(start_year + 1)[-2:]}"


def _truthy(value) -> bool:
    return str(value).strip().lower() in ("yes", "true", "1", "y", "on")


def scan_books(paths: list, entities_path: Optional[Path], include_other: bool,
               today: Optional[date] = None) -> ScanResult:
    today = today or date.today()
    scope = SCOPE_TYPES if include_other else CORE_TYPES
    results, summaries, warnings = [], [], []
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
        in_scope = [a for a in postable_accounts(accounts) if a.type in scope]
        ob_ids = frozenset(a.id for a in accounts if "opening-balance" in a.special_flags)
        acc_dates = _collect_account_dates(root, ob_ids)
        entity_label, entity_key, fy_key = _resolve_entity(book_path, entities_path)

        bounds = _fy_bounds(fy_key)
        inferred = False
        if bounds is None:
            # Never fall back to the account's first-ever transaction: use
            # the year of the book's latest transaction, and say so.
            fy_key = _fy_key_of_latest_transaction(acc_dates, today)
            bounds = _fy_bounds(fy_key)
            inferred = True
        if bounds is None:
            warnings.append(f"WARNING: {book_path.name} has no transactions, skipped.")
            continue
        fy_start, fy_end = bounds
        window_end = min(fy_end, today)
        if window_end < fy_start:
            warnings.append(f"WARNING: {book_path.name}: {fy_title(fy_key)} has not started yet, skipped.")
            continue

        summary = BookSummary(
            fy_key=fy_key, inferred=inferred,
            capped_to=_month_key(window_end.isoformat()) if window_end < fy_end else "",
        )
        for account in in_scope:
            r = _process_account(
                account, acc_dates.get(account.id, []), fy_key,
                fy_start, fy_end, window_end,
                entity_label, entity_key, entities_path, book_path,
            )
            if r is None:
                continue
            results.append(r)
            if r.section == SECTION_CORE:
                summary.core_checked += 1
                summary.core_with_gaps += int(r.has_gaps)
            else:
                summary.other_checked += 1
                summary.other_with_gaps += int(r.has_gaps)
        summaries.append(summary)

    return ScanResult(results, summaries, warnings, books_scanned, include_other)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(
    books,
    output_path: str,
    entities_path: str = "Data/itr/entities.yaml",
    config_path: str = "config.yaml",
    model_override: str = None,
    include_other: str = "no",
) -> str:
    """
    Check every selected .gnucash book's bank and card accounts for months
    with no transactions inside the book's financial year, and write one
    workbook to output_path. Returns the plain-language text for the UI.
    Read-only -- never writes to or modifies a .gnucash file.

    `books` is either a list of .gnucash paths or a single newline-separated
    string of them (the UI's multi-book field is a path textbox holding one
    path per line, and run_args substitution makes every kwarg a string on
    the way in -- same convention as the Inter-entity Matrix skill).
    `include_other` ("yes"/"no") also checks ASSET/LIABILITY accounts, in a
    separate labelled section.
    """
    if isinstance(books, str):
        paths = [ln.strip() for ln in books.splitlines() if ln.strip()]
    else:
        paths = [str(b).strip() for b in (books or []) if str(b).strip()]
    paths = list(dict.fromkeys(paths))
    if not paths:
        return "ERROR: select at least one .gnucash book."

    entities_path_obj = Path(entities_path) if entities_path else None
    scan = scan_books(paths, entities_path_obj, _truthy(include_other))
    XL.write_report_workbook(scan, output_path)
    return scan.reply_text(output_path)
