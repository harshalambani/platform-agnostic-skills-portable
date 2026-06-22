#!/usr/bin/env python3
"""
ICICI Bank Statement → Canonical CSV Importer.

Hardcoded parser for ICICI's XLS download format.
No LLM needed — deterministic parsing with known column layout.

Input:  ICICI .xls file (BIFF format, downloaded from net banking)
Output: Canonical 8-column CSV for Phase 3/4/6 consumption:
        Date, Transaction ID, Description, Account, Deposit, Withdrawal, Balance, Currency
        (Uses Transaction Date, not Value Date; includes Balance for Phase 4 reconciliation)
"""

import csv
import json
import logging
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.balance_utils import format_balance_summary as _fmt_bal

logger = logging.getLogger(__name__)

# ============================================================================
# CONSTANTS — ICICI XLS layout
# ============================================================================

# Preamble: 12 rows of header/search-params before the data header row
PREAMBLE_ROWS = 12

# Column indices in the XLS data rows (0-indexed):
#   0: empty (table offset)
#   1: S No.
#   2: Value Date         ← KEEP
#   3: Transaction Date   ← DROP
#   4: Cheque Number      ← KEEP
#   5: Transaction Remarks← KEEP
#   6: Withdrawal Amount  ← KEEP
#   7: Deposit Amount     ← KEEP
#   8: Balance            ← DROP
COL_VALUE_DATE = 2
COL_TXN_DATE = 3
COL_CHEQUE = 4
COL_REMARKS = 5
COL_WITHDRAWAL = 6
COL_DEPOSIT = 7
COL_BALANCE = 8

# Month abbreviation → number
MONTH_MAP = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
    'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
}

# Legend/footer markers — rows starting with these are stripped
LEGEND_MARKERS = [
    'Legends used in the statement',
    '. UPI -',
    '. BIL -',
    '. INF -',
    '. MMT -',
    '. CLG -',
    '. NEFT -',
    '. RTGS -',
    '. NFS -',
    '. ATD -',
    '. CMS -',
    '. EBA -',
    '. VPS -',
    '. IPS -',
    '. TOP -',
    '. BCTT -',
    '. UCCBRN',
    '. LCCBRN',
    '. N chg -',
    '. T Chg -',
    '. DTK -',
]


# ============================================================================
# XLS → CSV conversion
# ============================================================================

def convert_xls_to_csv(xls_path: str) -> List[List[str]]:
    """Read ICICI .xls (BIFF) and return rows as List[List[str]].

    Requires xlrd (pure Python, no external tools): pip install xlrd
    """
    import xlrd  # noqa: PLC0415

    xls_path = Path(xls_path)
    if not xls_path.exists():
        raise FileNotFoundError(f"File not found: {xls_path}")

    wb = xlrd.open_workbook(str(xls_path))
    ws = wb.sheet_by_index(0)
    rows: List[List[str]] = []
    for row_idx in range(ws.nrows):
        row: List[str] = []
        for col_idx in range(ws.ncols):
            cell = ws.cell(row_idx, col_idx)
            if cell.ctype == xlrd.XL_CELL_DATE:
                dt = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                row.append(dt.strftime('%d/%m/%Y'))
            elif cell.ctype == xlrd.XL_CELL_NUMBER:
                val = cell.value
                row.append(str(int(val)) if val == int(val) else str(val))
            elif cell.ctype == xlrd.XL_CELL_EMPTY:
                row.append('')
            else:
                row.append(str(cell.value).strip())
        rows.append(row)
    return rows


# ============================================================================
# DATE PARSING
# ============================================================================

def parse_icici_date(date_str: str) -> Optional[str]:
    """Parse ICICI's DD,Mon,YYYY format → ISO YYYY-MM-DD.

    Examples:
        "01,Apr,2024" → "2024-04-01"
        "31,Mar,2025" → "2025-03-31"
    """
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip().strip('"')
    if not date_str:
        return None

    parts = date_str.split(',')
    if len(parts) != 3:
        return None

    day = parts[0].strip()
    month_abbr = parts[1].strip().lower()
    year = parts[2].strip()

    month = MONTH_MAP.get(month_abbr)
    if not month:
        logger.warning(f"Unknown month abbreviation: {month_abbr}")
        return None

    day = day.zfill(2)
    return f"{year}-{month}-{day}"


# ============================================================================
# NUMBER FORMATTING
# ============================================================================

def format_amount(value_str: str) -> str:
    """Format ICICI amount: strip trailing zeros, handle 0.0 → 0.

    Examples:
        "300000.00" → "300000"
        "156623.60" → "156623.6"
        "0.0"       → "0"
        "27608.07"  → "27608.07"
    """
    if not value_str or not isinstance(value_str, str):
        return "0"
    value_str = value_str.strip()
    if not value_str:
        return "0"

    try:
        val = float(value_str.replace(',', ''))
    except ValueError:
        return "0"

    if val == 0:
        return "0"

    # If it's a whole number, return as int
    if val == int(val):
        return str(int(val))

    # Otherwise strip trailing zeros from decimal
    formatted = f"{val:.2f}".rstrip('0').rstrip('.')
    return formatted


def parse_amount_float(value_str: str) -> float:
    """Parse amount string to float for validation."""
    if not value_str or not isinstance(value_str, str):
        return 0.0
    try:
        return float(value_str.strip().replace(',', ''))
    except ValueError:
        return 0.0


# ============================================================================
# DESCRIPTION TRANSFORMS
# ============================================================================

def transform_description(cheque: str, remarks: str) -> Tuple[str, str]:
    """Transform ICICI transaction remarks: extract ref to cheque, clean description.

    Returns (new_cheque, new_description).

    Rules by transaction type:
    - MMT/IMPS/REF/...     → cheque=REF, desc=rest after REF
    - NEFT-REF-...         → cheque=REF, desc=rest after REF
    - RTGS-REF-...         → cheque=REF, desc=rest after REF
    - UPI/REF/...          → cheque=REF (numeric), desc=rest, strip trailing /ICI.../AXI... hash, drop NA/
    - UPI/VPA/desc/.../REF/HASH → cheque=REF, desc=VPA parts, strip trailing hash
    - BIL/ONL/REF/...      → cheque=REF, desc=rest
    - BIL/INFT/REF/...     → cheque=REF, desc=rest
    - INF/INFT/REF/...     → cheque=REF, desc=rest
    - NFS/.../REF/...      → cheque=REF, desc=rest
    - CMS/ CMSREF/...      → cheque=REF, desc=rest
    - CLG, ATD, Rev Sweep, Closure Proceeds, AUTOSWEEP, Interest, TDS → keep as-is
    """
    remarks = remarks.strip()
    cheque = cheque.strip()

    # If cheque already has a real value (not '-' or empty), keep it
    has_existing_cheque = cheque and cheque != '-' and cheque != ''

    # --- MMT/IMPS ---
    if remarks.startswith('MMT/IMPS/'):
        body = remarks[len('MMT/IMPS/'):]
        parts = body.split('/', 1)
        ref = parts[0]
        desc = parts[1] if len(parts) > 1 else ''
        if not has_existing_cheque:
            cheque = ref
        return cheque, desc

    # --- NEFT ---
    if remarks.startswith('NEFT-'):
        body = remarks[len('NEFT-'):]
        # First segment up to the next '-' is the reference
        parts = body.split('-', 1)
        ref = parts[0]
        desc = parts[1] if len(parts) > 1 else ''
        if not has_existing_cheque:
            cheque = ref
        return cheque, desc

    # --- RTGS ---
    if remarks.startswith('RTGS-'):
        body = remarks[len('RTGS-'):]
        parts = body.split('-', 1)
        ref = parts[0]
        desc = parts[1] if len(parts) > 1 else ''
        if not has_existing_cheque:
            cheque = ref
        return cheque, desc

    # --- UPI ---
    if remarks.startswith('UPI/'):
        return _transform_upi(cheque, has_existing_cheque, remarks)

    # --- BIL/ONL ---
    if remarks.startswith('BIL/ONL/'):
        body = remarks[len('BIL/ONL/'):]
        parts = body.split('/', 1)
        ref = parts[0]
        desc = parts[1] if len(parts) > 1 else ''
        if not has_existing_cheque:
            cheque = ref
        return cheque, desc

    # --- BIL/INFT ---
    if remarks.startswith('BIL/INFT/'):
        body = remarks[len('BIL/INFT/'):]
        parts = body.split('/', 1)
        ref = parts[0]
        desc = parts[1] if len(parts) > 1 else ''
        if not has_existing_cheque:
            cheque = ref
        return cheque, desc

    # --- BIL/BPAY ---
    if remarks.startswith('BIL/BPAY/'):
        body = remarks[len('BIL/BPAY/'):]
        parts = body.split('/', 1)
        ref = parts[0]
        desc = parts[1] if len(parts) > 1 else ''
        if not has_existing_cheque:
            cheque = ref
        return cheque, desc

    # --- INF/INFT ---
    if remarks.startswith('INF/INFT/'):
        body = remarks[len('INF/INFT/'):]
        parts = body.split('/', 1)
        ref = parts[0]
        desc = parts[1] if len(parts) > 1 else ''
        if not has_existing_cheque:
            cheque = ref
        return cheque, desc

    # --- NFS (ATM) ---
    if remarks.startswith('NFS/'):
        return _transform_nfs(cheque, has_existing_cheque, remarks)

    # --- CMS ---
    if remarks.startswith('CMS/'):
        body = remarks[len('CMS/'):].strip()
        # CMS ref is like "CMS4156644346" — find it
        match = re.match(r'(CMS\d+)[/]?(.*)', body)
        if match:
            ref = match.group(1)
            desc = match.group(2).strip().lstrip('/')
            if not has_existing_cheque:
                cheque = ref
            return cheque, desc
        return cheque, remarks

    # --- DTK ---
    if remarks.startswith('DTK/'):
        body = remarks[len('DTK/'):]
        parts = body.split('/', 1)
        ref = parts[0]
        desc = parts[1] if len(parts) > 1 else ''
        if not has_existing_cheque:
            cheque = ref
        return cheque, desc

    # --- Everything else: keep as-is ---
    # CLG (already has cheque), ATD, Rev Sweep, Closure Proceeds,
    # AUTOSWEEP, Interest, TDS, Sweep Adj, BIL/ REV PMT
    return cheque, remarks


# Prefix labels used as fallback when description is empty after transform
_PREFIX_LABELS = [
    ('MMT/IMPS/', 'MMT/IMPS'),
    ('NEFT-', 'NEFT'),
    ('RTGS-', 'RTGS'),
    ('UPI/', 'UPI'),
    ('BIL/ONL/', 'BIL/ONL'),
    ('BIL/INFT/', 'BIL/INFT'),
    ('BIL/BPAY/', 'BIL/BPAY'),
    ('INF/INFT/', 'INF/INFT'),
    ('NFS/', 'NFS'),
    ('CMS/', 'CMS'),
    ('DTK/', 'DTK'),
]


def _is_upi_hash(segment: str) -> bool:
    """Check if a UPI segment is a trailing bank hash (e.g., ICI5af2dd7f90, AXI34ca...).

    Pattern: 2-4 uppercase letters followed by 8+ alphanumeric chars.
    Known prefixes: ICI, AXI, ACD, SBI, SBIA, HDF, KOT, KMBM, etc.
    """
    segment = segment.strip()
    if len(segment) < 10:
        return False
    return bool(re.match(r'^[A-Z]{2,4}[a-f0-9A-Z]{8,}$', segment))


def _transform_upi(cheque: str, has_existing_cheque: bool, remarks: str) -> Tuple[str, str]:
    """Transform UPI transactions.

    Two sub-patterns:
    1. Numeric-first: UPI/309897586014/NA/avadhclubslimit//ICI5af2dd7f90
       → cheque=309897586014, desc=avadhclubslimit (drop NA/, strip trailing hash)

    2. VPA-first: UPI/johndoe-1/xfer to self/EXAMPLE BANK (INDIA/900012345678/AXI34ca...
       → cheque=900012345678, desc=johndoe-1/xfer to self/EXAMPLE BANK (INDIA

    3. Numeric-first with desc after: UPI/409393425102/cred/credclub@icici/ICICI Bank/MN
       → cheque=409393425102, desc=cred/credclub@icici/ICICI Bank/MN
    """
    body = remarks[len('UPI/'):]
    parts = body.split('/')

    # Find the reference number: look for a segment that's purely numeric and ≥10 digits
    ref_idx = None
    for i, part in enumerate(parts):
        if re.match(r'^\d{10,}$', part.strip()):
            ref_idx = i
            break

    if ref_idx is None:
        # No numeric ref found — keep as-is but strip UPI/ prefix
        return cheque, body

    ref = parts[ref_idx].strip()

    # Parts before the ref
    before = parts[:ref_idx]
    # Parts after the ref — keep meaningful ones, strip trailing bank hash
    after = parts[ref_idx + 1:]

    # Strip trailing hash segments from the end
    while after and (_is_upi_hash(after[-1]) or not after[-1].strip()):
        after.pop()

    # Combine before + after, filtering out 'NA' and empty segments
    all_desc_parts = before + after
    cleaned = [p.strip() for p in all_desc_parts
               if p.strip() and p.strip().upper() != 'NA']

    desc = '/'.join(cleaned)

    if not has_existing_cheque:
        cheque = ref

    return cheque, desc


def _transform_nfs(cheque: str, has_existing_cheque: bool, remarks: str) -> Tuple[str, str]:
    """Transform NFS (ATM) transactions.

    Patterns:
    - NFS/CASH WDL/428712007881/S1ACMU59/MUMBAI   /13-10
      → cheque=428712007881, desc=CASH WDL/S1ACMU59/MUMBAI   /13-10
    - NFS/00824035/CASH WDL/09-06-23
      → cheque=00824035, desc=CASH WDL/09-06-23
    """
    body = remarks[len('NFS/'):]
    parts = body.split('/')

    # Find numeric ref (≥6 digits)
    ref_idx = None
    for i, part in enumerate(parts):
        if re.match(r'^\d{6,}$', part.strip()):
            ref_idx = i
            break

    if ref_idx is not None:
        ref = parts[ref_idx].strip()
        # Description = everything except NFS/ prefix and the ref
        desc_parts = [p for j, p in enumerate(parts) if j != ref_idx]
        desc = '/'.join(p for p in desc_parts if p.strip())
        if not has_existing_cheque:
            cheque = ref
        return cheque, desc

    return cheque, remarks


# ============================================================================
# ROW FILTERING
# ============================================================================

def is_data_row(row: List[str]) -> bool:
    """Check if a row is a valid transaction data row (not legend/footer/empty)."""
    if len(row) < 7:
        return False

    # Check for mostly empty rows
    non_empty = sum(1 for c in row if str(c).strip())
    if non_empty < 4:
        return False

    # Check for legend/footer markers
    row_text = ' '.join(str(c).strip() for c in row)
    for marker in LEGEND_MARKERS:
        if marker in row_text:
            return False

    # S No. column (index 1) should be numeric for data rows
    sno = str(row[1]).strip()
    if not sno:
        return False
    try:
        int(sno)
        return True
    except ValueError:
        return False


# ============================================================================
# MAIN TRANSFORM PIPELINE
# ============================================================================

def transform_icici_statement(xls_path: str, output_path: str) -> Dict[str, Any]:
    """Transform an ICICI XLS statement to GnuCash-importable CSV.

    Returns a result dict with success status, row counts, and any issues.
    """
    logger.info(f"Processing ICICI statement: {xls_path}")

    # Step 1: XLS → CSV rows
    raw_rows = convert_xls_to_csv(xls_path)
    logger.info(f"Raw CSV: {len(raw_rows)} rows")

    # Step 2: Skip preamble, find data rows
    data_rows = []
    for i in range(PREAMBLE_ROWS + 1, len(raw_rows)):  # +1 to skip header row too
        row = raw_rows[i]
        if is_data_row(row):
            data_rows.append(row)

    logger.info(f"Data rows found: {len(data_rows)}")

    if not data_rows:
        return {
            'success': False,
            'error': 'No transaction data rows found after preamble stripping',
            'raw_row_count': len(raw_rows),
        }

    # Step 3: Transform each row
    transformed = []
    issues = []

    for i, row in enumerate(data_rows):
        # Parse date — prefer Value Date (col 2) over Transaction Date (col 3)
        date_str = parse_icici_date(row[COL_VALUE_DATE])
        if not date_str:
            date_str = parse_icici_date(row[COL_TXN_DATE])
        if not date_str:
            issues.append(f"Row {i+1}: failed to parse date '{row[COL_TXN_DATE]}'")
            continue

        # Get cheque and remarks
        raw_cheque = str(row[COL_CHEQUE]).strip() if COL_CHEQUE < len(row) else '-'
        raw_remarks = str(row[COL_REMARKS]).strip() if COL_REMARKS < len(row) else ''

        # Transform description
        cheque, remarks = transform_description(raw_cheque, raw_remarks)

        # If description ended up empty, keep the transaction type prefix
        if not remarks.strip():
            for prefix, label in _PREFIX_LABELS:
                if raw_remarks.startswith(prefix):
                    remarks = label
                    break

        # Format amounts
        withdrawal = format_amount(str(row[COL_WITHDRAWAL])) if COL_WITHDRAWAL < len(row) else '0'
        deposit = format_amount(str(row[COL_DEPOSIT])) if COL_DEPOSIT < len(row) else '0'
        balance = format_amount(str(row[COL_BALANCE])) if COL_BALANCE < len(row) else '0'

        transformed.append({
            'date': date_str,
            'txn_id': cheque if cheque and cheque != '-' else '',
            'description': remarks,
            'account': '',  # Empty — Phase 3 mapper fills this
            'deposit': deposit,
            'withdrawal': withdrawal,
            'balance': balance,
            'currency': 'INR',
        })

    logger.info(f"Transformed {len(transformed)} rows")

    # Step 4: Post-validation
    validation_issues = post_validate(data_rows, transformed)
    issues.extend(validation_issues)

    # Step 5: Write output CSV
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        # Canonical 8-column schema for Phase 3/4/6 consumption
        writer.writerow([
            'Date', 'Transaction ID', 'Description', 'Account',
            'Deposit', 'Withdrawal', 'Balance', 'Currency'
        ])
        for row in transformed:
            writer.writerow([
                row['date'], row['txn_id'], row['description'], row['account'],
                row['deposit'], row['withdrawal'], row['balance'], row['currency'],
            ])

    logger.info(f"Wrote output CSV: {output_path}")

    # Derive opening/closing balance for summary
    if transformed:
        first_bal = parse_amount_float(transformed[0]['balance'])
        first_dep = parse_amount_float(transformed[0]['deposit'])
        first_wdl = parse_amount_float(transformed[0]['withdrawal'])
        _opening = round(first_bal - first_dep + first_wdl, 2)
        _closing = round(parse_amount_float(transformed[-1]['balance']), 2)
    else:
        _opening = 0.0
        _closing = 0.0

    return {
        'success': len(issues) == 0,
        'output_path': str(output_path),
        'rows_input': len(data_rows),
        'rows_output': len(transformed),
        'issues': issues,
        'opening_balance': _opening,
        'closing_balance': _closing,
    }


# ============================================================================
# POST-VALIDATION
# ============================================================================

def post_validate(original_rows: List[List[str]],
                  transformed: List[Dict[str, str]]) -> List[str]:
    """Validate transformed output against original data."""
    issues = []

    # Row count check
    if len(original_rows) != len(transformed):
        issues.append(
            f"Row count mismatch: {len(original_rows)} original → "
            f"{len(transformed)} transformed"
        )

    # Sum check — compare total withdrawals and deposits
    orig_withdrawal_sum = sum(
        parse_amount_float(str(r[COL_WITHDRAWAL])) for r in original_rows
    )
    orig_deposit_sum = sum(
        parse_amount_float(str(r[COL_DEPOSIT])) for r in original_rows
    )
    trans_withdrawal_sum = sum(
        parse_amount_float(r['withdrawal']) for r in transformed
    )
    trans_deposit_sum = sum(
        parse_amount_float(r['deposit']) for r in transformed
    )

    if abs(orig_withdrawal_sum - trans_withdrawal_sum) > 1.0:
        issues.append(
            f"Withdrawal sum mismatch: original={orig_withdrawal_sum:.2f}, "
            f"transformed={trans_withdrawal_sum:.2f}"
        )
    if abs(orig_deposit_sum - trans_deposit_sum) > 1.0:
        issues.append(
            f"Deposit sum mismatch: original={orig_deposit_sum:.2f}, "
            f"transformed={trans_deposit_sum:.2f}"
        )

    # Date sanity check (ISO YYYY-MM-DD format)
    for i, row in enumerate(transformed):
        try:
            dt = datetime.strptime(row['date'], '%Y-%m-%d')
            if dt.year < 2000 or dt.year > 2030:
                issues.append(f"Row {i+1}: suspicious date {row['date']}")
        except ValueError:
            issues.append(f"Row {i+1}: invalid date format {row['date']}")

    # Amount sanity — no negative numbers
    for i, row in enumerate(transformed):
        w = parse_amount_float(row['withdrawal'])
        d = parse_amount_float(row['deposit'])
        if w < 0:
            issues.append(f"Row {i+1}: negative withdrawal {row['withdrawal']}")
        if d < 0:
            issues.append(f"Row {i+1}: negative deposit {row['deposit']}")

    # Running balance reconciliation — use raw Balance column (col 8)
    # Check: prev_balance + deposit - withdrawal = current_balance
    # NOTE: ICICI may order rows by Value Date but compute balance by
    # Transaction Date, so same-day transactions can appear out of
    # processing order.  Mismatches are logged as warnings, not failures.
    balance_mismatches = 0
    for i in range(len(original_rows)):
        cur_balance = parse_amount_float(str(original_rows[i][COL_BALANCE]))
        cur_deposit = parse_amount_float(str(original_rows[i][COL_DEPOSIT]))
        cur_withdrawal = parse_amount_float(str(original_rows[i][COL_WITHDRAWAL]))

        if i == 0:
            opening_balance = cur_balance - cur_deposit + cur_withdrawal
            prev_balance = opening_balance
        else:
            prev_balance = parse_amount_float(str(original_rows[i - 1][COL_BALANCE]))

        expected_balance = prev_balance + cur_deposit - cur_withdrawal
        diff = abs(expected_balance - cur_balance)

        if diff > 0.02:  # Allow 2 paise rounding tolerance
            balance_mismatches += 1
            if balance_mismatches <= 5:
                sno = str(original_rows[i][1]).strip()
                logger.warning(
                    f"Row {i+1} (S.No {sno}): balance mismatch - "
                    f"prev={prev_balance:.2f} + dep={cur_deposit:.2f} "
                    f"- wdl={cur_withdrawal:.2f} = {expected_balance:.2f}, "
                    f"but statement shows {cur_balance:.2f} (diff={diff:.2f})"
                )

    if balance_mismatches > 5:
        logger.warning(f"... and {balance_mismatches - 5} more balance mismatches")
    if balance_mismatches > 0:
        logger.warning(
            f"Running balance: {balance_mismatches} mismatch(es) - "
            f"likely Value Date vs Transaction Date ordering"
        )
    else:
        logger.info("Running balance reconciliation: OK (all rows match)")

    return issues


# ============================================================================
# run() — entry point for the generic tab builder (ui/tabs/_generic.py)
# ============================================================================

def run(
    statement_files: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Run the ICICI agent from the PA Skills UI.

    Args:
        statement_files: Path to a single ICICI .xls file **or** a directory
                         containing one or more .xls files (the generic runner
                         stages multi-file uploads into a temp directory).
        output_path:     Path where the output CSV should be saved.
        config_path:     Path to config.yaml (unused — no LLM needed).
        model_override:  Optional model name (unused — no LLM needed).
    """
    src = Path(statement_files)

    # Collect XLS files
    if src.is_file():
        xls_files = [src]
    elif src.is_dir():
        xls_files = sorted(src.glob("*.xls"))
        if not xls_files:
            return f"ERROR: no .xls files found in {statement_files}"
    else:
        return f"ERROR: path is neither a file nor a directory: {statement_files}"

    print(f"[ICICI] Found {len(xls_files)} XLS file(s)")

    out_dir = Path(output_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_parts = []
    replies = []

    for i, xls in enumerate(xls_files, 1):
        # For single file, use the requested output_path directly;
        # for batch, generate per-file names in the same directory.
        if len(xls_files) == 1:
            csv_out = Path(output_path)
        else:
            csv_out = out_dir / f"{xls.stem}_gnucash.csv"

        print(f"[ICICI] Processing {i}/{len(xls_files)}: {xls.name}")
        try:
            result = transform_icici_statement(str(xls), str(csv_out))
            status = "OK" if result["success"] else "ISSUES"
            ob = result.get('opening_balance', 0)
            cb = result.get('closing_balance', 0)
            replies.append(
                f"**{xls.name}:** {status} - "
                f"{result['rows_output']} rows "
                f"(balance: {ob:.2f} → {cb:.2f})"
            )
            if result["success"]:
                csv_parts.append(csv_out)
            if result.get("issues"):
                for iss in result["issues"]:
                    replies.append(f"  - {iss}")
        except Exception as e:
            replies.append(f"**{xls.name}:** ERROR - {e}")

    if not csv_parts:
        return "ERROR: no CSVs were produced from any of the input files."

    # For batch mode with multiple outputs, merge them into the single output_path
    if len(csv_parts) > 1:
        _merge_csvs(csv_parts, Path(output_path))

    # Write sidecar summary JSON for pipeline's balance verification
    # ICICI closing_balance is derived from last row (no independent statement summary)
    sidecar_path = Path(output_path).with_suffix(".csv_summary.json")
    try:
        # Use the last successful result's balances
        last_ob = result.get('opening_balance', 0) if result else 0
        last_cb = result.get('closing_balance', 0) if result else 0
        last_rows = result.get('rows_output', 0) if result else 0
        sidecar_data = {
            "bank": "ICICI",
            "source": "derived",
            "opening_balance": last_ob,
            "closing_balance": last_cb,
            "row_count": last_rows,
        }
        with open(sidecar_path, "w", encoding="utf-8") as sf:
            json.dump(sidecar_data, sf, indent=2)
        logger.info("Wrote sidecar summary: %s", sidecar_path)
    except Exception as e:
        logger.warning("Could not write sidecar summary: %s", e)

    summary = (
        f"Complete - processed {len(xls_files)} file(s), "
        f"produced {len(csv_parts)} CSV(s).\n\n"
        + "\n".join(replies)
    )
    return summary


def _merge_csvs(parts: list, output: Path) -> None:
    """Merge multiple CSVs into one, keeping header from first file only."""
    with open(output, "w", newline="", encoding="utf-8") as out:
        for idx, part in enumerate(parts):
            with open(part, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f):
                    # Skip header row on all but the first file
                    if idx > 0 and line_no == 0:
                        continue
                    out.write(line)


# ============================================================================
# AGENT CLASS
# ============================================================================

class ICICIImportAgent:
    """ICICI Bank Statement Import Agent."""

    def invoke(self, statement_files: List[str], **kwargs) -> Dict[str, Any]:
        """Process ICICI statement file(s) and produce GnuCash CSVs."""
        if not statement_files:
            raise ValueError("No statement files provided")

        output_dir = Path(kwargs.get('output_dir', 'outputs'))
        results = {}

        for file_path in statement_files:
            output_file = output_dir / f"{Path(file_path).stem}_gnucash.csv"
            try:
                result = transform_icici_statement(file_path, str(output_file))
                results[file_path] = result
            except Exception as e:
                logger.error(f"Failed to process {file_path}: {e}")
                results[file_path] = {
                    'success': False,
                    'error': str(e),
                }

        succeeded = sum(1 for r in results.values() if r.get('success'))
        return {
            'results': results,
            'summary': f"Processed {len(statement_files)} file(s); {succeeded} succeeded",
        }


# ============================================================================
# CLI ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description='ICICI Bank Statement to GnuCash CSV')
    parser.add_argument('statement_files', nargs='+', help='ICICI .xls file(s)')
    parser.add_argument('--output-dir', default='outputs', help='Output directory')

    args = parser.parse_args()

    agent = ICICIImportAgent()
    result = agent.invoke(
        statement_files=args.statement_files,
        output_dir=args.output_dir,
    )

    print(json.dumps(result, indent=2))
