"""Stage 2: Turn per-page Tesseract TSVs into a reconciled transaction list.

Two things happen here, in sequence:

A. **Coordinate-based parsing** — Each page's TSV carries x/y pixel
   coordinates for every OCR'd word. We group words into lines, find the
   header line ("Deposit / Withdrawals / Balance"), and from that work out
   the right-edge of each money column. Every money token on a transaction
   line is then classified by its right edge: leftmost money = deposit,
   middle = withdrawal, rightmost = balance. This is far more robust than
   guessing from whitespace alignment, which Tesseract mangles.

B. **Balance reconciliation** — For each transaction row we check
   `prev_balance + deposit - withdrawal == balance`. If the equation fails
   we look at the *next* row, reconstruct `bal[i]` from `bal[i+1] - dep[i+1]
   + wd[i+1]`, and if that candidate reconciles with `bal[i-1]` we replace
   the broken OCR'd balance. Rows fixed this way carry
   `"balance_corrected": true` so the final Excel can highlight them.

Output is `cleaned.json` in the work dir, ready for the enrichment stage.

Usage:
    python parse_tsv.py --work-dir /path/to/work [--out cleaned.json]
"""
import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# This script always runs as a standalone subprocess (spawned via
# ``sys.executable``, both in dev and in the PyInstaller-frozen build where
# the whole ``agents`` tree ships as raw .py data files under _MEIPASS) --
# it never inherits a caller's sys.path/PYTHONPATH. Bootstrap our own path
# to ``src`` (or _MEIPASS, in frozen mode) so ``agents.bank_common`` is
# importable regardless of how this script was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from agents.bank_common.consolidate import StatementGroup, consolidate  # noqa: E402

# Fallback column right-edges (tuned for HSBC Premier Savings statements at
# 300 DPI, portrait A4). Auto-detection from the header row normally wins;
# these are only used if the header can't be found on page 1.
DEFAULT_DEPOSIT_RIGHT = 1527
DEFAULT_WITHDRAWAL_RIGHT = 1879
DEFAULT_BALANCE_RIGHT = 2254

MONEY_RE = re.compile(r'^\(?-?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?\)?$')
DATE_RE = re.compile(r'^(\d{2})([A-Za-z0-9]{3})(\d{4})$')
MONTH_MAP = {m: i + 1 for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])}

# OCR-split money tokens: e.g. a comma gets eaten and "247,370.83" becomes
# two tokens "247" and "370.83" — we re-glue them.
NUM_FRAGMENT_LEFT = re.compile(r'^\d{1,3}(?:,\d{3})*$')
NUM_FRAGMENT_RIGHT = re.compile(r'^\d{3}(?:[,.]\d+)*$')
NUM_FRAGMENT_RIGHT_COMMA = re.compile(r'^,\d{3}(?:[,.]\d+)*$')


def normalize_month(s: str):
    """Map common OCR misreads of month abbreviations back to the canonical form."""
    for cand in [s.title(),
                 s.replace('0', 'O').title(),
                 s.replace('0', 'o').title(),
                 s.replace('1', 'l').title()]:
        if cand in MONTH_MAP:
            return cand
    return None


def load_tsv_lines(tsv_path: Path):
    """Group word-level rows from a Tesseract TSV into lines, sorted top-to-bottom."""
    lines = defaultdict(list)
    with open(tsv_path) as f:
        reader = csv.DictReader(f, delimiter='\t')
        for row in reader:
            try:
                if int(row['level']) != 5:  # word level only
                    continue
                txt = row['text']
                if not txt or not txt.strip():
                    continue
                left = int(row['left']); top = int(row['top'])
                w = int(row['width']); h = int(row['height'])
                # Some Tesseract builds emit sub-pixel confidence as a float
                # string (e.g. "96.637047") rather than a plain int.
                conf = int(float(row['conf']))
                key = (int(row['block_num']), int(row['par_num']), int(row['line_num']))
                lines[key].append({
                    'text': txt, 'left': left, 'top': top,
                    'width': w, 'height': h, 'right': left + w,
                    'conf': conf,
                })
            except (ValueError, KeyError):
                continue
    flat = []
    for _, words in lines.items():
        if not words:  # defaultdict access can leave a phantom empty entry
            continue    # if a later field in this row failed to parse
        words.sort(key=lambda w: w['left'])
        avg_top = sum(w['top'] for w in words) / len(words)
        flat.append({'top': avg_top, 'words': words})
    flat.sort(key=lambda L: L['top'])
    return flat


def detect_column_edges(lines):
    """Find the Deposit / Withdrawals / Balance header line and read their right
    edges. Returns (dep_right, wd_right, bal_right) or None if not found.

    The header OCRs reliably as three words spaced out horizontally. We look
    for a line containing Deposit + (Withdrawals|Withdrawal) + Balance.
    """
    for L in lines:
        tokens = [w['text'].lower().rstrip(',') for w in L['words']]
        line_text = ' '.join(tokens)
        if 'deposit' in line_text and 'balance' in line_text and (
                'withdrawal' in line_text or 'withdrawals' in line_text):
            dep_right = wd_right = bal_right = None
            for w in L['words']:
                tl = w['text'].lower().rstrip(',')
                if tl.startswith('deposit'):
                    dep_right = w['right']
                elif tl.startswith('withdrawal'):
                    wd_right = w['right']
                elif tl.startswith('balance'):
                    bal_right = w['right']
            if dep_right and wd_right and bal_right:
                return dep_right, wd_right, bal_right
    return None


def parse_money(s: str):
    s = s.strip()
    neg = False
    if s.startswith('(') and s.endswith(')'):
        neg = True; s = s[1:-1]
    if s.startswith('-'):
        neg = True; s = s[1:]
    s = s.replace(',', '')
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def is_money_token(txt: str):
    t = txt.strip().rstrip(',').rstrip('.')
    return bool(MONEY_RE.match(t))


def merge_split_numbers(words):
    """Re-glue OCR-split money like ['247', '370.83'] -> '247,370.83'."""
    merged = []
    i = 0
    while i < len(words):
        w = words[i]
        if i + 1 < len(words):
            nxt = words[i + 1]
            gap = nxt['left'] - w['right']
            if (NUM_FRAGMENT_LEFT.match(w['text']) and
                    (NUM_FRAGMENT_RIGHT.match(nxt['text']) or
                     NUM_FRAGMENT_RIGHT_COMMA.match(nxt['text'])) and
                    0 <= gap <= 50):
                sep = '' if nxt['text'].startswith(',') else ','
                combined = f"{w['text']}{sep}{nxt['text']}"
                if MONEY_RE.match(combined) or re.match(
                        r'^\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?$', combined):
                    merged.append({
                        'text': combined, 'left': w['left'], 'top': w['top'],
                        'width': nxt['right'] - w['left'],
                        'height': max(w['height'], nxt['height']),
                        'right': nxt['right'],
                        'conf': min(w['conf'], nxt['conf']),
                    })
                    i += 2
                    continue
        merged.append(w)
        i += 1
    return merged


def extract_date(words):
    """If the leftmost word is DDMMMYYYY, return (iso_date, words_without_date)."""
    if not words:
        return None, words
    m = DATE_RE.match(words[0]['text'])
    if m:
        day, mon, year = m.groups()
        mon_clean = normalize_month(mon)
        if mon_clean:
            return f"{year}-{MONTH_MAP[mon_clean]:02d}-{int(day):02d}", words[1:]
    return None, words


def classify_col(right_x, dep_right, wd_right, bal_right):
    """Decide which column a money token belongs to, using midpoints."""
    split_dep_wd = (dep_right + wd_right) / 2
    split_wd_bal = (wd_right + bal_right) / 2
    if right_x < split_dep_wd:
        return 'deposit'
    if right_x < split_wd_bal:
        return 'withdrawal'
    return 'balance'


def extract_transactions_multi_page(tsv_paths, dep_right, wd_right, bal_right):
    """Walk through all pages of one statement, emitting transaction dicts.

    State is carried across pages so that descriptions split over the
    "Balance Carried/Brought Forward" page boundary are joined correctly.
    """
    transactions = []
    in_table = False
    pending_desc_lines = []
    current_date = None
    brought_fwd_recorded = False

    for tsv_path in tsv_paths:
        lines = load_tsv_lines(tsv_path)
        for L in lines:
            L['words'] = merge_split_numbers(L['words'])
            line_text = ' '.join(w['text'] for w in L['words']).strip()
            low = line_text.lower()

            # Table starts at the "Details of your accounts" banner.
            if 'details of your accounts' in low:
                in_table = True
                continue
            if not in_table:
                # On continuation pages the table re-opens with "Balance Brought Forward".
                if 'balance brought forward' in low or 'balance broughtforward' in low:
                    in_table = True
                else:
                    continue

            # Skip page/section noise lines.
            if any(kw in low for kw in [
                'savings account-res', 'nominee registered', 'micr code', 'ifsc code',
                'dr=debit', 'deposits and investments', 'total deposits', 'borrowings',
                'summary of your portfolio', 'premier account statement',
                'total borrowings', 'currency / unit', 'page ', 'hsbc premier',
                'date transaction details', 'transaction details',
            ]):
                continue

            # "Balance Carried Forward" marks the end of a page; discard any
            # pending description so it doesn't bleed into the next tx.
            _nospace = low.replace(' ', '')
            if ('carriedforward' in _nospace or 'cartiedforward' in _nospace
                    or 'cariedforward' in _nospace
                    or ('balance' in low and 'carried' in low and 'forward' in low)):
                pending_desc_lines = []
                continue

            money_words = [w for w in L['words'] if is_money_token(w['text'])]
            non_money = [w for w in L['words'] if not is_money_token(w['text'])]

            date_str, _ = extract_date(L['words'])
            if date_str:
                current_date = date_str
                non_money = [w for w in non_money if not DATE_RE.match(w['text'])]

            # Record the opening balance exactly once per statement.
            if 'balance brought forward' in low or 'balance broughtforward' in low:
                pending_desc_lines = []
                if money_words and not brought_fwd_recorded:
                    transactions.append({
                        'type': 'brought_forward',
                        'date': current_date,
                        'desc': 'BALANCE BROUGHT FORWARD',
                        'deposit': None, 'withdrawal': None,
                        'balance': parse_money(money_words[-1]['text']),
                        'source_page': tsv_path.stem,
                    })
                    brought_fwd_recorded = True
                continue

            classified = [(classify_col(mw['right'], dep_right, wd_right, bal_right),
                           parse_money(mw['text']), mw) for mw in money_words]
            has_balance = any(c[0] == 'balance' for c in classified)

            if has_balance:
                deposit = next((c[1] for c in classified if c[0] == 'deposit'), None)
                withdrawal = next((c[1] for c in classified if c[0] == 'withdrawal'), None)
                balance = next((c[1] for c in classified if c[0] == 'balance'), None)

                this_line_text = ' '.join(w['text'] for w in non_money).strip()
                parts = pending_desc_lines[:]
                if this_line_text:
                    parts.append(this_line_text)
                desc = ' | '.join(p for p in parts if p)
                pending_desc_lines = []

                transactions.append({
                    'type': 'transaction',
                    'date': current_date,
                    'desc': desc,
                    'deposit': deposit,
                    'withdrawal': withdrawal,
                    'balance': balance,
                    'source_page': tsv_path.stem,
                })
            else:
                # Description-only line: accumulate.
                pieces = [w['text'] for w in L['words'] if not DATE_RE.match(w['text'])]
                t = ' '.join(pieces).strip()
                if t:
                    pending_desc_lines.append(t)

    return transactions


def recon_check(tx_list):
    """Return list of (idx, expected_balance, actual_balance) for rows that don't reconcile."""
    errors = []
    prev = tx_list[0]['balance']
    for i in range(1, len(tx_list)):
        t = tx_list[i]
        expected = prev + (t.get('deposit') or 0) - (t.get('withdrawal') or 0)
        if abs(expected - t['balance']) >= 0.01:
            errors.append((i, expected, t['balance']))
        prev = t['balance']
    return errors


def try_fix_pair(tx_list, i, fixes_log):
    """If row i's balance is broken, try reconstructing it from row i+1 math."""
    if i + 1 >= len(tx_list) or i == 0:
        return False
    t, nxt = tx_list[i], tx_list[i + 1]
    reconstructed = nxt['balance'] + (nxt.get('withdrawal') or 0) - (nxt.get('deposit') or 0)
    prev_bal = tx_list[i - 1]['balance']
    expected = prev_bal + (t.get('deposit') or 0) - (t.get('withdrawal') or 0)
    if abs(expected - reconstructed) < 0.01:
        fixes_log.append({
            'idx': i, 'old_balance': t['balance'],
            'new_balance': round(reconstructed, 2),
            'desc': t['desc'][:80],
            'source_page': t.get('source_page'),
            'reason': 'bal_ocr_fix_from_next_row',
        })
        t['balance'] = round(reconstructed, 2)
        t['balance_corrected'] = True
        return True
    return False


def check_statement_continuity(stmt_periods):
    """Flag likely-missing or overlapping statements.

    Args:
        stmt_periods: list of (name, period_start, period_end), already sorted
            by period_start; period_start/period_end are ISO date strings or
            None (e.g. a statement with no parseable transaction dates).

    Returns a list of human-readable warning strings (empty if none).

    Thin wrapper over the shared ``bank_common.consolidate.check_continuity``
    (this bank's original algorithm, now promoted to be the single shared
    implementation BoB and ICICI also use) -- kept as a standalone function
    so existing direct callers/tests of this exact signature keep working.
    """
    groups = [StatementGroup(name, [], start, end) for name, start, end in stmt_periods]
    from agents.bank_common.consolidate import check_continuity  # noqa: PLC0415
    return check_continuity(groups)


def is_carried_forward(desc):
    low = (desc or '').lower().replace(' ', '')
    return 'carriedforward' in low or 'cartiedforward' in low or 'cariedforward' in low


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--work-dir", required=True, type=Path,
                    help="Folder that contains tsv/<statement>/page-*.tsv.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output JSON (default: <work-dir>/cleaned.json).")
    ap.add_argument("--fixes-log", type=Path, default=None,
                    help="Optional JSON dump of reconciliation fixes.")
    ap.add_argument("--deposit-right", type=int, default=None,
                    help="Override auto-detected Deposit column right-edge x-coordinate.")
    ap.add_argument("--withdrawal-right", type=int, default=None,
                    help="Override auto-detected Withdrawal column right-edge.")
    ap.add_argument("--balance-right", type=int, default=None,
                    help="Override auto-detected Balance column right-edge.")
    args = ap.parse_args()

    tsv_root = args.work_dir / "tsv"
    if not tsv_root.exists():
        raise SystemExit(f"No TSV folder at {tsv_root}; run ocr_to_tsv.py first.")

    # Filename order is untrustworthy: Google-Drive-style "(1)".."(11)" download
    # suffixes reflect download order, not statement period. We parse every
    # statement first, then order the results by the actual transaction dates
    # found inside each one (falling back to filename order only if a
    # statement yields no dates at all, e.g. a truly empty/unreadable file).
    def natural_key(p):
        return [int(tok) if tok.isdigit() else tok.lower()
                for tok in re.findall(r'\d+|\D+', p.name)]
    statement_dirs = sorted((p for p in tsv_root.iterdir() if p.is_dir()),
                            key=natural_key)
    if not statement_dirs:
        raise SystemExit(f"No statement subfolders under {tsv_root}.")

    stmt_results = []  # (stmt_dir, tx, period_start, period_end)
    for stmt_dir in statement_dirs:
        pages = sorted(
            stmt_dir.glob("page-*.tsv"),
            key=lambda p: int(re.search(r'page-(\d+)', p.stem).group(1)),
        )
        if not pages:
            continue

        # Auto-detect column edges from page 1, fall back to defaults/overrides.
        edges = detect_column_edges(load_tsv_lines(pages[0]))
        if edges:
            dep_r, wd_r, bal_r = edges
        else:
            dep_r = args.deposit_right or DEFAULT_DEPOSIT_RIGHT
            wd_r = args.withdrawal_right or DEFAULT_WITHDRAWAL_RIGHT
            bal_r = args.balance_right or DEFAULT_BALANCE_RIGHT
        # Explicit CLI overrides always win.
        if args.deposit_right: dep_r = args.deposit_right
        if args.withdrawal_right: wd_r = args.withdrawal_right
        if args.balance_right: bal_r = args.balance_right

        tx = extract_transactions_multi_page(pages, dep_r, wd_r, bal_r)
        for t in tx:
            t['source_pdf'] = stmt_dir.name
        dates = sorted(t['date'] for t in tx if t.get('date'))
        period_start = dates[0] if dates else None
        period_end = dates[-1] if dates else None
        stmt_results.append(StatementGroup(stmt_dir.name, tx, period_start, period_end))

    # Order by actual statement period (undated statements sort last, in
    # filename order) and flag gaps/overlaps -- the shared bank_common
    # helper (this bank's original algorithm, now promoted to be the single
    # shared implementation BoB and ICICI also use).
    consolidated = consolidate(stmt_results)
    continuity_warnings = consolidated.warnings
    all_tx = consolidated.rows

    # Drop "Balance Carried Forward" noise and keep only the first brought_forward.
    cleaned = []
    first_bf = False
    for t in all_tx:
        if is_carried_forward(t.get('desc') or ''):
            continue
        if t.get('type') == 'brought_forward':
            if not first_bf:
                cleaned.append(t); first_bf = True
            continue
        if t.get('balance') is None:
            continue
        cleaned.append(t)

    # Iteratively attempt OCR-error balance fixes.
    fixes_log = []
    for _ in range(3):
        errs = recon_check(cleaned)
        if not errs:
            break
        for idx, _, _ in list(errs):
            try_fix_pair(cleaned, idx, fixes_log)

    remaining = recon_check(cleaned)
    print(f"Parsed {len(cleaned)} rows (brought-forward + transactions).")
    print(f"OCR balance corrections: {len(fixes_log)}")
    print(f"Unresolved reconciliation errors: {len(remaining)}")
    for idx, expected, got in remaining[:5]:
        t = cleaned[idx]
        print(f"  [{idx}] {t.get('date')} dep={t.get('deposit')} wd={t.get('withdrawal')} "
              f"bal={got} expected={round(expected,2)} | {t.get('desc','')[:60]}")

    print(f"Statement continuity: {len(continuity_warnings)} warning(s)")
    for w in continuity_warnings:
        print(f"  WARNING: {w}")

    out = args.out or (args.work_dir / "cleaned.json")
    with open(out, 'w') as f:
        json.dump(cleaned, f, indent=2, default=str)
    print(f"Wrote {out}")

    if continuity_warnings:
        warnings_path = out.parent / "continuity_warnings.json"
        with open(warnings_path, 'w') as f:
            json.dump(continuity_warnings, f, indent=2)
        print(f"Wrote {warnings_path}")

    if args.fixes_log or fixes_log:
        flog = args.fixes_log or (args.work_dir / "fixes_log.json")
        with open(flog, 'w') as f:
            json.dump(fixes_log, f, indent=2, default=str)
        print(f"Wrote {flog}")


if __name__ == "__main__":
    main()
