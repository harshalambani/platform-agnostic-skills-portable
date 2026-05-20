"""Stage 3: Enrich cleaned transactions and tidy the description text.

For every row that isn't the brought-forward opener we do three things:

 1. **Extract a Transaction Date** from embedded tokens like `01Apr2025`,
    `O5Apr2025` (OCR slip of 0→O), `01-Apr-2025`, or `2025/04/01`. This is
    the real value-date of the transaction, which often differs from the
    posting date HSBC prints in the left column (especially for IMPS /
    NEFT receipts, debit-card auths, etc.). If no embedded date is found
    we fall back to the posting date.

 2. **Extract a Transaction Number** by running a prioritised regex cascade
    covering every ID prefix we've seen across 12 months of HSBC Premier
    Savings statements: UPI, INO / IN, CMS, HIB, HSBCN, HSBCR, ICIN,
    ICICN / ICICR, KKBKN, GS, NEFT, IMPS / IMPSREF, UTIB, LP, ATM trailing
    refs, and country-code+digits debit-card AUTH codes. The full catalog
    lives in `references/patterns.md`; extend that file when a statement
    surfaces a new prefix.

 3. **Clean the description** by stripping everything that's redundant once
    (1) and (2) are in their own columns, and moving annotations the user
    still wants to see into a separate `Extra Information` field. Concretely:

      - Remove INR/USD amount echoes that duplicate the Deposit/Withdrawal
        columns (e.g. `INR 6,144.00`).
      - Remove canonical + raw transaction-ID tokens so they don't appear
        twice.
      - Capture `DDMMMYY ELECTRO HH:MM:SS`-style stamps, bare channel+time
        fragments (`NFS 12:34:56`, `CASHNET 08:15`, `POS 23:01:17`), and
        leading bare `IMPS` markers into Extra Information.
      - Delete remaining redundant date tokens (they're in Transaction Date now).
      - Collapse GST rate strings like `@ 9.00000000` to `@ 9%`.
      - Dedupe pipe-delimited segments (case-insensitive).
      - If after cleanup a row starts with a bare numeric segment followed
        by text (e.g. `545856898109 | HMD CONSULTANCY`), move the leading
        digits to the end so the human-readable name comes first. This
        iterates — some rows have two or three leading numeric segments.

The output JSON has the original fields plus `txn_date`, `txn_ids` (list),
`txn_no` (joined with `; `), `cleaned_desc`, and `extra_info`.

Usage:
    python enrich.py --in cleaned.json --out enriched.json
"""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path


MONTH_MAP = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
}


def parse_date_token(d, m, y):
    try:
        day = int(d.replace('O', '0'))
    except ValueError:
        return None
    m_upper = m.upper()
    if m_upper not in MONTH_MAP:
        return None
    mon = MONTH_MAP[m_upper]
    try:
        year = int(y.replace('O', '0'))
    except ValueError:
        return None
    if year < 100:
        year += 2000
    try:
        return datetime(year, mon, day).date()
    except ValueError:
        return None


DATE_PATTERNS = [
    re.compile(r'\b([O0]?\d|\d[O0]?)([A-Z][a-z]{2})(\d{4})\b'),   # 01Apr2025, O5Apr2025
    re.compile(r'\b(\d{2})([A-Z]{3})(\d{2,4})\b'),                # 01APR25, 30MAR25
    re.compile(r'\b(\d{4})/(\d{2})/(\d{2})\b'),                   # 2025/04/01
    re.compile(r'\b(\d{2})-([A-Z][a-z]{2})-(\d{4})\b'),           # 01-Apr-2025
]


def extract_date(desc, fallback_date):
    """Return the earliest unambiguous date found in `desc`, else `fallback_date`."""
    for pat in DATE_PATTERNS:
        m = pat.search(desc)
        if not m:
            continue
        g = m.groups()
        if '/' in pat.pattern:
            try:
                year, mon, day = int(g[0]), int(g[1]), int(g[2])
                return datetime(year, mon, day).date().isoformat()
            except Exception:
                continue
        else:
            d = parse_date_token(*g)
            if d:
                return d.isoformat()
    return fallback_date


# Transaction-ID regexes, ordered by specificity — more specific patterns run
# first so e.g. "INO..." wins over "IN...". See references/patterns.md for
# rationale, sample inputs, and advice for extending.
TXN_PATTERNS = [
    # UPI digits may be split by whitespace; also tolerate OCR misreads "UP1", "UPl", "UP|".
    ('UPI',    re.compile(r'\bUP[I1lL|]\s*(\d{4,}\s?\d{6,})')),
    # IN / INO — OCR can slip the 3rd char to S/B (OSB0 alternation covers it).
    ('INO',    re.compile(r'(?<![A-Z])IN[O0][OSB0]?\d{4,10}(?=INR|\b|[A-Z]{3})')),
    ('IN',     re.compile(r'(?<![A-Z])IN\d{5,10}(?=INR|\b|[A-Z]{3})')),
    ('CMS',    re.compile(r'\bCMS\d{5,}\b')),
    ('HIB',    re.compile(r'\bHIB[-\s]*\d{7,}[A-Z0-9]*\b')),
    ('HSBCN',  re.compile(r'\bHSBCN\d{7,}\b')),
    ('HSBCR',  re.compile(r'\bHSBCR\d{7,}\b')),
    # Debit-card auth codes like US010607 / HK063652 — country code + 6 digits glued to currency.
    ('AUTH',   re.compile(r'\b(?:US|HK|GB|EU|SG|AE|TH|JP)\d{6}(?=USD|INR|HKD|SGD|GBP|AED|THB|JPY|EUR)')),
    ('ICIN',   re.compile(r'\bICIN\d{7,}\b')),
    ('ICICR',  re.compile(r'\bICICR\d{10,}\b')),
    ('ICICN',  re.compile(r'\bICICN\d{10,}\b')),
    ('UTIB',   re.compile(r'\bUTIB[O0]\d{4,}/?\d*\b')),
    ('KKBKN',  re.compile(r'\bKKBKN\d{7,}\b')),
    ('GS',     re.compile(r'\bGS\d{10,}\b')),
    ('NEFT',   re.compile(r'\bNEFT[A-Z0-9]{8,}\b')),
    ('IMPS',   re.compile(r'\bIMPS\s*/\s*(\d{9,})\b')),
    ('IMPSREF', re.compile(r'(?:^|\n|\|)\s*(5\d{11})\b')),
    ('LP',     re.compile(r'\bLP\s*[A-Z0-9]{8,}\b')),
    ('ATM',    re.compile(r'\b(9\d{11})\s+(\d{6})\b')),
]


def extract_txn_ids(desc):
    """Return (canonical_ids, raw_matches). `raw_matches` is used later to erase
    the ID text from the description so it doesn't appear twice."""
    ids = []
    seen = set()
    raw_matches = []
    for tag, pat in TXN_PATTERNS:
        for m in pat.finditer(desc):
            raw = m.group(0)
            raw_matches.append(raw)
            if m.groups():
                tok = raw if tag not in ('UPI', 'IMPS', 'IMPSREF') \
                    else f"{tag if tag != 'IMPSREF' else 'IMPS'}{m.group(1)}"
            else:
                tok = raw
            tok = tok.replace(' ', '').replace('-', '').replace('/', '')
            if tok not in seen:
                seen.add(tok)
                ids.append(tok)
    return ids, raw_matches


# Cleanup patterns (see SKILL.md for the why of each).
INR_AMOUNT_RE = re.compile(r'INR\s*\d[\d,]*(?:\.\d+)?')
USD_AMOUNT_RE = re.compile(r'USD\s*\d[\d,]*(?:\.\d+)?')
US_CODE_RE    = re.compile(r'\bUS\d{6}(?=USD)')
WHITESPACE_RE = re.compile(r'\s{2,}')

# Month triplet must contain at least one letter — prevents matching pure digit
# runs like 00000000 from GST-rate strings.
DATE_SHORT_RE = re.compile(r'\b[O0]?\d{1,2}(?:[A-Z]{3}|0[A-Z]{2}|[A-Z]0[A-Z]|[A-Z]{2}0)\d{2,4}\b')
DATE_LONG_RE  = re.compile(r'\b[O0]?\d{1,2}[A-Z][a-z]{2}\d{4}\b')
DATE_SLASH_RE = re.compile(r'\b\d{4}/\d{2}/\d{2}(?:\s*\d{6})?\b')
DATE_DASH_RE  = re.compile(r'\b\d{2}-[A-Z][a-z]{2}-\d{4}\b')
CHANNEL_TIME_RE = re.compile(
    r'\b(?:ELECTRO|NFS|CASHNET|POS|ECOM|INB|ATM\s*TXN|ATMA\d+)\s+\d{1,2}:\d{2}(?::\d{2})?\b')
DATE_CHANNEL_TIME_RE = re.compile(
    r'\b[O0]?\d{1,2}[A-Z0]{3}\d{2,4}\s+'
    r'(?:ELECTRO|NFS|CASHNET|POS|ECOM|INB|ATM\s*TXN|ATMA\d+)\s+\d{1,2}:\d{2}(?::\d{2})?\b')
STRAY_DECIMAL_RE = re.compile(r'(?<!\d)\.\d{1,2}(?!\d)')
GST_RATE_RE = re.compile(r'(@\s*\d+)\.0{3,}')
LEADING_IMPS_RE = re.compile(r'^\s*IMPS\s*(?:\||$)', re.I)


def dedupe_segments(desc):
    parts = [WHITESPACE_RE.sub(' ', p.strip()) for p in desc.split('|')]
    seen = set()
    out = []
    for p in parts:
        if not p:
            continue
        key = p.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return ' | '.join(out)


def clean_desc(desc, txn_ids, raw_matches):
    """Strip noise; return (cleaned_desc, extra_info).

    Extra information holds pieces we don't want in the main description
    but the user may still want to see — DDMMMYY+channel+time combos,
    leading bare `IMPS` markers, and bare channel+time fragments.
    """
    extras = []
    d = desc

    d = INR_AMOUNT_RE.sub('', d)
    d = USD_AMOUNT_RE.sub('', d)
    d = US_CODE_RE.sub('', d)
    for raw in raw_matches:
        d = d.replace(raw, '')
    for t in txn_ids:
        d = re.sub(re.escape(t), '', d)
    d = re.sub(r'\bUP[I1lL|]\d*\b', '', d)

    for m in DATE_CHANNEL_TIME_RE.finditer(d):
        extras.append(m.group(0).strip())
    d = DATE_CHANNEL_TIME_RE.sub('', d)

    # Slash form first so trailing HHMMSS tokens get consumed too.
    d = DATE_SLASH_RE.sub('', d)
    d = DATE_LONG_RE.sub('', d)
    d = DATE_DASH_RE.sub('', d)

    for m in DATE_SHORT_RE.finditer(d):
        extras.append(m.group(0).strip())
    d = DATE_SHORT_RE.sub('', d)

    for m in CHANNEL_TIME_RE.finditer(d):
        extras.append(m.group(0).strip())
    d = CHANNEL_TIME_RE.sub('', d)

    d = STRAY_DECIMAL_RE.sub('', d)
    d = GST_RATE_RE.sub(r'\1%', d)

    d = dedupe_segments(d)
    d = re.sub(r'\|\s*\|', '|', d)
    d = re.sub(r'^\s*\|\s*', '', d)
    d = re.sub(r'\s*\|\s*$', '', d)
    d = WHITESPACE_RE.sub(' ', d).strip()

    # Trim trailing stray punctuation inside each segment.
    parts2 = [re.sub(r'[\s@.,:]+$', '', p).strip() for p in d.split('|')]
    parts2 = [re.sub(r'^[\s@.,:|]+', '', p).strip() for p in parts2]
    d = ' | '.join(p for p in parts2 if p)

    if LEADING_IMPS_RE.match(d):
        extras.append('IMPS')
        d = re.sub(r'^\s*IMPS\s*\|?\s*', '', d, count=1)
    d = d.strip()

    # Iteratively swap leading numeric segments to the tail so text comes first.
    parts = [p.strip() for p in d.split('|') if p.strip()]
    moved_tail = []
    while len(parts) >= 2 and re.fullmatch(r'[\d][\d,\s]*', parts[0]):
        moved_tail.append(parts[0])
        parts = parts[1:]
    if moved_tail:
        parts = parts + moved_tail
        d = ' | '.join(parts)

    # Dedupe extras preserving order.
    seen = set()
    extra_parts = []
    for e in extras:
        key = re.sub(r'\s+', ' ', e).strip().upper()
        if key and key not in seen:
            seen.add(key)
            extra_parts.append(re.sub(r'\s+', ' ', e).strip())
    return d, ' | '.join(extra_parts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--in", dest="in_path", required=True, type=Path,
                    help="cleaned.json produced by parse_tsv.py")
    ap.add_argument("--out", dest="out_path", required=True, type=Path,
                    help="Where to write enriched.json")
    args = ap.parse_args()

    with open(args.in_path) as f:
        rows = json.load(f)

    enriched = []
    for r in rows:
        desc = r.get('desc') or ''
        desc_for_search = desc.replace('\n', ' | ')  # tolerate either separator
        row_date = r.get('date')
        txn_date = extract_date(desc_for_search, row_date)
        txn_ids, raw_matches = extract_txn_ids(desc_for_search)
        cleaned_desc, extra_info = clean_desc(desc_for_search, txn_ids, raw_matches)
        enriched.append({
            **r,
            'txn_date': txn_date,
            'txn_ids': txn_ids,
            'txn_no': '; '.join(txn_ids) if txn_ids else '',
            'cleaned_desc': cleaned_desc,
            'extra_info': extra_info,
        })

    with open(args.out_path, 'w') as f:
        json.dump(enriched, f, indent=2, default=str)

    data_only = [e for e in enriched if e.get('type') != 'brought_forward']
    with_txn = sum(1 for e in data_only if e['txn_ids'])
    with_extra = sum(1 for e in data_only if e['extra_info'])
    total = max(len(data_only), 1)
    print(f"Enriched {len(data_only)} transactions.")
    print(f"  With Txn Number:      {with_txn} ({with_txn*100//total}%)")
    print(f"  With Extra Information: {with_extra} ({with_extra*100//total}%)")
    print(f"Wrote {args.out_path}")


if __name__ == "__main__":
    main()
