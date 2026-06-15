#!/usr/bin/env python3
"""
GnuCash CSV Import Agent — spec-then-transform engine.
Converts raw bank statements (CSV/XLSX) to GnuCash-importable CSV.
"""

import json
import csv
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
import logging
from datetime import datetime
from decimal import Decimal
import uuid

import pandas as pd

logger = logging.getLogger(__name__)

# ============================================================================
# DATA MODELS
# ============================================================================

class ColumnSpec:
    """Column mapping spec extracted from LLM."""
    def __init__(self, spec_dict: Dict[str, Any]):
        self.date_column = spec_dict.get('date_column')
        self.date_format = spec_dict.get('date_format', 'DD/MM/YYYY')
        self.description_column = spec_dict.get('description_column')
        self.withdrawal_column = spec_dict.get('withdrawal_column')
        self.deposit_column = spec_dict.get('deposit_column')
        self.balance_column = spec_dict.get('balance_column')
        self.currency = spec_dict.get('currency', 'INR')
        self.txn_id_column = spec_dict.get('txn_id_column')
        self.dr_cr_indicator_column = spec_dict.get('dr_cr_indicator_column')
        self.has_value_date = spec_dict.get('has_value_date', False)
        self.value_date_column = spec_dict.get('value_date_column')

    def to_dict(self) -> Dict:
        return {
            'date_column': self.date_column,
            'date_format': self.date_format,
            'description_column': self.description_column,
            'withdrawal_column': self.withdrawal_column,
            'deposit_column': self.deposit_column,
            'balance_column': self.balance_column,
            'currency': self.currency,
            'txn_id_column': self.txn_id_column,
            'dr_cr_indicator_column': self.dr_cr_indicator_column,
            'has_value_date': self.has_value_date,
            'value_date_column': self.value_date_column,
        }


# ============================================================================
# NUMBER & DATE PARSING
# ============================================================================

def parse_indian_number(value_str: str) -> float:
    """Parse Indian-formatted number: 1,23,456.78 → 123456.78"""
    if not value_str or not isinstance(value_str, str):
        return 0.0
    value_str = value_str.strip()
    if not value_str:
        return 0.0
    try:
        return float(value_str.replace(',', ''))
    except ValueError:
        logger.warning(f"Failed to parse number: {value_str}")
        return 0.0


def parse_date(date_str: str, date_format: str) -> Optional[str]:
    """Parse date string to ISO 8601 (YYYY-MM-DD)."""
    if not date_str or not isinstance(date_str, str):
        return None
    date_str = date_str.strip().strip('"')  # Strip quotes (ICICI DD,Mon,YYYY format)
    if not date_str:
        return None
    format_map = {
        'DD/MM/YYYY': '%d/%m/%Y',
        'DD/MM/YY': '%d/%m/%y',
        'MM/DD/YYYY': '%m/%d/%Y',
        'MM/DD/YY': '%m/%d/%y',
        'YYYY-MM-DD': '%Y-%m-%d',
        'DD-MM-YYYY': '%d-%m-%Y',
        'DD-MM-YY': '%d-%m-%y',
        'DD,Mon,YYYY': '%d,%b,%Y',  # ICICI format: "01,Apr,2024"
    }
    fmt = format_map.get(date_format, '%d/%m/%Y')
    try:
        dt = datetime.strptime(date_str, fmt)
        return dt.strftime('%Y-%m-%d')
    except ValueError as e:
        logger.warning(f"Failed to parse date '{date_str}' with format '{date_format}': {e}")
        return None


def cleanup_description(desc: str) -> str:
    """Clean transaction description: preserve UPI VPA, merchant ID, counterparty."""
    if not desc:
        return ""
    desc = desc.strip()
    if '@' in desc:
        return desc
    if desc.startswith('NEFT') or desc.startswith('IMPS'):
        return desc
    if len(desc) > 100:
        parts = desc.split('-')
        if len(parts) > 1:
            return '-'.join(parts[:2]).strip()
        return desc[:80]
    return desc


# ============================================================================
# PRE-PARSE: Junk-stripping & table isolation
# ============================================================================

def preparse_statement(file_path: str) -> Tuple[List[str], List[List[str]]]:
    """Read file (CSV/XLSX/XLS), strip preamble/legend, isolate transaction table.

    XLS read via xlrd (pure Python, pip install xlrd).
    XLSX read via openpyxl through pandas.
    """
    file_path = Path(file_path)
    rows = []

    if file_path.suffix.lower() == '.xls':
        import xlrd
        wb = xlrd.open_workbook(str(file_path))
        ws = wb.sheet_by_index(0)
        for r in range(ws.nrows):
            row = []
            for c in range(ws.ncols):
                cell = ws.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    dt = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                    row.append(dt.strftime('%d/%m/%Y'))
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    v = cell.value
                    row.append(str(int(v)) if v == int(v) else str(v))
                elif cell.ctype == xlrd.XL_CELL_EMPTY:
                    row.append('')
                else:
                    row.append(str(cell.value).strip())
            rows.append(row)
    elif file_path.suffix.lower() == '.xlsx':
        df = pd.read_excel(file_path, sheet_name=0, header=None)
        rows = [row.tolist() for _, row in df.iterrows()]
    else:
        # CSV file
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            reader = csv.reader(f)
            rows = [row for row in reader]

    # Find the header row
    header_idx = None
    for i, row in enumerate(rows):
        non_empty = sum(1 for cell in row if str(cell).strip())
        if non_empty >= 5:
            row_str = ' '.join(str(c).lower() for c in row)
            if any(kw in row_str for kw in ['date', 'amount', 'balance', 'description', 'withdrawal', 'deposit']):
                header_idx = i
                break

    if header_idx is None:
        for i, row in enumerate(rows):
            if sum(1 for c in row if str(c).strip()) >= 5:
                header_idx = i
                break

    # Extract header and data
    header = [str(c).strip() for c in rows[header_idx]] if header_idx < len(rows) else []
    data_rows = []

    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        non_empty = sum(1 for c in row if str(c).strip())
        if non_empty < 3:
            continue

        # Skip footer/summary rows
        row_str = ' '.join(str(c).lower().strip() for c in row)
        if any(kw in row_str for kw in ['opening balance', 'closing balance', 'generated on', 'total', 'summary']):
            continue
        if row_str.replace('*', '').replace('-', '').strip() == '':
            continue

        # Check first column (date) looks like a date (contains / or -)
        first_col = str(row[0]).strip()
        if first_col and not any(sep in first_col for sep in ['/', '-']):
            continue

        data_rows.append([str(c).strip() for c in row])

    return header, data_rows


# ============================================================================
# SPEC GENERATION
# ============================================================================

def generate_spec_prompt(header: List[str], sample_rows: List[List[str]]) -> str:
    """Generate the LLM prompt for spec generation."""
    sample_csv = '\n'.join([
        ','.join(header),
        *[','.join(row) for row in sample_rows[:20]]
    ])
    prompt = f"""You are analyzing a bank statement to map columns. Here's a sample:

{sample_csv}

Respond with ONLY a JSON object (no markdown, no extra text) mapping the columns:
{{
  "date_column": <index of date column>,
  "date_format": "<DD/MM/YYYY or MM/DD/YYYY or YYYY-MM-DD>",
  "description_column": <index of description/narration column>,
  "withdrawal_column": <index of withdrawal amount column, or null>,
  "deposit_column": <index of deposit amount column, or null>,
  "balance_column": <index of closing balance column, or null>,
  "currency": "<ISO 4217 code, e.g., INR, USD>",
  "txn_id_column": <index of cheque/ref number column, or null>,
  "dr_cr_indicator_column": <index of Dr/Cr column if single amount with indicator, or null>,
  "has_value_date": <true/false>,
  "value_date_column": <index if has_value_date, or null>
}}

Rules:
- Columns are 0-indexed.
- Prefer DD/MM/YYYY for Indian banks.
- Default currency to INR.
"""
    return prompt


def _load_bank_date_formats() -> Dict[str, str]:
    """Load bank-specific date formats from config."""
    import yaml
    config_path = Path(__file__).parent / "bank_date_formats.yaml"
    if config_path.exists():
        with open(config_path, 'r') as f:
            data = yaml.safe_load(f) or {}
            return data.get('date_formats', {})
    return {}


def _detect_column_type(col_header: str, col_values: List[str]) -> Optional[str]:
    """Detect column type from header and sample values."""
    header_lower = col_header.lower()

    # Header-based detection
    if any(kw in header_lower for kw in ['date', 'tdate', 'value date']):
        return 'date'
    if any(kw in header_lower for kw in ['desc', 'narration', 'particulars', 'remark']):
        return 'description'
    if any(kw in header_lower for kw in ['debit', 'withdraw', 'debit amt']):
        return 'withdrawal'
    if any(kw in header_lower for kw in ['credit', 'deposit', 'credit amt']):
        return 'deposit'
    if any(kw in header_lower for kw in ['balance', 'closing']):
        return 'balance'
    if any(kw in header_lower for kw in ['cheque', 'chq', 'ref', 'transaction id']):
        return 'txn_id'

    # Content-based detection (sample values)
    if col_values:
        sample = col_values[0]
        # Dates: contain / or - with numbers
        if '/' in sample or ',' in sample:
            if any(c.isalpha() for c in sample):  # Month name like "Apr"
                return 'date'
            elif sample.count('/') == 2 or sample.count(',') == 2:
                return 'date'
        # Numbers with decimal: likely amount
        try:
            float(sample.replace(',', ''))
            if '.' in sample or ',' in sample:
                return 'deposit'  # Assume deposit by default
        except ValueError:
            pass

    return None


def _generate_heuristic_spec(header: List[str], sample_rows: List[List[str]],
                            bank_hint: str = "auto") -> ColumnSpec:
    """Generate spec using heuristic content detection (no LLM needed)."""
    bank_formats = _load_bank_date_formats()
    date_format = bank_formats.get(bank_hint, bank_formats.get('_default', 'DD/MM/YY'))

    spec = {
        'date_column': None,
        'date_format': date_format,
        'description_column': None,
        'withdrawal_column': None,
        'deposit_column': None,
        'balance_column': None,
        'currency': 'INR',
        'txn_id_column': None,
    }

    # Extract sample values per column
    col_samples = [[row[i] if i < len(row) else "" for row in sample_rows] for i in range(len(header))]

    # Detect columns
    for col_idx, col_header in enumerate(header):
        col_type = _detect_column_type(col_header, col_samples[col_idx])
        if col_type == 'date' and spec['date_column'] is None:
            spec['date_column'] = col_idx
        elif col_type == 'description' and spec['description_column'] is None:
            spec['description_column'] = col_idx
        elif col_type == 'withdrawal' and spec['withdrawal_column'] is None:
            spec['withdrawal_column'] = col_idx
        elif col_type == 'deposit' and spec['deposit_column'] is None:
            spec['deposit_column'] = col_idx
        elif col_type == 'balance' and spec['balance_column'] is None:
            spec['balance_column'] = col_idx
        elif col_type == 'txn_id' and spec['txn_id_column'] is None:
            spec['txn_id_column'] = col_idx

    # Fallback: if we couldn't find critical columns, use positional defaults
    if spec['date_column'] is None:
        spec['date_column'] = 0
    if spec['description_column'] is None:
        spec['description_column'] = min(1, len(header) - 1)
    if spec['deposit_column'] is None and spec['withdrawal_column'] is None:
        if len(header) > 4:
            spec['withdrawal_column'] = 3
            spec['deposit_column'] = 4

    logger.info(f"Heuristic spec detected: date_col={spec['date_column']}, "
                f"desc_col={spec['description_column']}, "
                f"withdrawal_col={spec['withdrawal_column']}, "
                f"deposit_col={spec['deposit_column']}, "
                f"date_format={date_format}")

    return ColumnSpec(spec)


def generate_spec(header: List[str], sample_rows: List[List[str]], bank_hint: str = "auto",
                  llm_fn=None) -> ColumnSpec:
    """Generate column mapping spec from sample."""
    # Try LLM first if available
    if llm_fn is not None:
        prompt = generate_spec_prompt(header, sample_rows)
        try:
            spec_json = llm_fn(prompt, temperature=0)
            spec_dict = json.loads(spec_json.strip())
            logger.info(f"LLM spec generation succeeded for bank_hint={bank_hint}")
            return ColumnSpec(spec_dict)
        except json.JSONDecodeError as e:
            logger.warning(f"LLM spec generation failed (invalid JSON): {e}")
            logger.info(f"Falling back to heuristic spec generation")
        except Exception as e:
            logger.warning(f"LLM spec generation failed: {e}")
            logger.info(f"Falling back to heuristic spec generation")

    # Fallback to content-based heuristic (no LLM needed)
    logger.info(f"Using heuristic spec generation for bank_hint={bank_hint}")
    return _generate_heuristic_spec(header, sample_rows, bank_hint)


# ============================================================================
# ROW TRANSFORM
# ============================================================================

def transform_row(row: List[str], spec: ColumnSpec, account_name: str = "") -> Optional[Dict[str, str]]:
    """Transform a single row using the spec."""
    try:
        # Parse date
        date_val = row[spec.date_column] if spec.date_column < len(row) else None
        date_iso = parse_date(date_val, spec.date_format) if date_val else None
        if not date_iso:
            return None

        # Parse description
        desc_val = row[spec.description_column] if spec.description_column < len(row) else ""
        description = cleanup_description(desc_val)

        # Parse amounts
        withdrawal = 0.0
        deposit = 0.0

        if spec.deposit_column is not None and spec.withdrawal_column is not None:
            dep_val = row[spec.deposit_column] if spec.deposit_column < len(row) else "0"
            wit_val = row[spec.withdrawal_column] if spec.withdrawal_column < len(row) else "0"
            deposit = parse_indian_number(dep_val)
            withdrawal = parse_indian_number(wit_val)
        elif spec.dr_cr_indicator_column is not None:
            amt_column = spec.deposit_column or spec.withdrawal_column
            dr_cr_val = row[spec.dr_cr_indicator_column] if spec.dr_cr_indicator_column < len(row) else "Cr"
            amt_val = row[amt_column] if amt_column < len(row) else "0"
            amount = parse_indian_number(amt_val)
            if 'Cr' in dr_cr_val or 'CR' in dr_cr_val:
                deposit = amount
            else:
                withdrawal = amount

        # Parse balance
        balance = 0.0
        if spec.balance_column is not None:
            bal_val = row[spec.balance_column] if spec.balance_column < len(row) else "0"
            balance = parse_indian_number(bal_val)

        # Generate/extract TxnID
        txn_id = ""
        if spec.txn_id_column is not None:
            txn_id = row[spec.txn_id_column] if spec.txn_id_column < len(row) else ""
        if not txn_id:
            txn_id = str(uuid.uuid4())[:8]

        return {
            'date': date_iso,
            'txn_id': txn_id,
            'description': description,
            'account': account_name,
            'deposit': f"{deposit:.2f}",
            'withdrawal': f"{withdrawal:.2f}",
            'balance': f"{balance:.2f}",
            'currency': spec.currency,
        }
    except Exception as e:
        logger.warning(f"Failed to transform row {row}: {e}")
        return None


# ============================================================================
# POST-VALIDATION
# ============================================================================

def validate_transformed_data(header: List[str], original_rows: List[List[str]],
                              transformed_rows: List[Dict[str, str]]) -> Tuple[bool, List[str]]:
    """Post-validate: row count, sum checks, date/amount sanity."""
    issues = []

    if len(original_rows) != len(transformed_rows):
        issues.append(f"Row count mismatch: {len(original_rows)} original, {len(transformed_rows)} transformed")

    if transformed_rows:
        total_deposit = sum(Decimal(r['deposit']) for r in transformed_rows)
        total_withdrawal = sum(Decimal(r['withdrawal']) for r in transformed_rows)

        if all(r.get('balance') for r in transformed_rows):
            first_bal = Decimal(transformed_rows[0]['balance'])
            last_bal = Decimal(transformed_rows[-1]['balance'])
            balance_delta = last_bal - first_bal
            amount_delta = total_deposit - total_withdrawal

            if abs(balance_delta - amount_delta) > Decimal('50000.00'):
                issues.append(f"Balance mismatch: delta={balance_delta}, (deposits-withdrawals)={amount_delta}")

    for i, row in enumerate(transformed_rows):
        try:
            dt = datetime.fromisoformat(row['date'])
            if dt.year > datetime.now().year + 10:
                issues.append(f"Row {i}: suspicious future date {row['date']}")
        except ValueError:
            issues.append(f"Row {i}: invalid date {row['date']}")

    for i, row in enumerate(transformed_rows):
        try:
            dep = Decimal(row['deposit'])
            wit = Decimal(row['withdrawal'])
            if dep < 0 or wit < 0:
                issues.append(f"Row {i}: negative amount (deposit={dep}, withdrawal={wit})")
        except (ValueError, Decimal.InvalidOperation):
            issues.append(f"Row {i}: non-numeric amount")

    is_valid = len(issues) == 0
    return is_valid, issues


# ============================================================================
# OUTPUT GENERATION
# ============================================================================

def write_canonical_csv(output_path: str, transformed_rows: List[Dict[str, str]]) -> None:
    """Write transformed rows to canonical CSV."""
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        fieldnames = ['Date', 'Transaction ID', 'Description', 'Account', 'Deposit', 'Withdrawal', 'Balance', 'Currency']
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval='')
        writer.writeheader()
        for row in transformed_rows:
            writer.writerow({
                'Date': row['date'],
                'Transaction ID': row['txn_id'],
                'Description': row['description'],
                'Account': row['account'],
                'Deposit': row['deposit'],
                'Withdrawal': row['withdrawal'],
                'Balance': row['balance'],
                'Currency': row['currency'],
            })


# ============================================================================
# MAIN AGENT
# ============================================================================

class GnuCashImportAgent:
    """Main agent orchestrating the spec-then-transform pipeline."""

    def __init__(self, llm_fn=None):
        self.llm_fn = llm_fn

    def invoke(self, statement_files: List[str], bank_hint: str = "auto",
               account_name: str = "", **kwargs) -> Dict[str, Any]:
        """Process statement file(s) and return canonical CSVs."""
        if not statement_files:
            raise ValueError("No statement files provided")

        results = {}

        for file_path in statement_files:
            logger.info(f"Processing {file_path}...")

            header, data_rows = preparse_statement(file_path)
            logger.info(f"Parsed {len(data_rows)} data rows")

            if len(data_rows) < 1:
                raise ValueError(f"No transaction data found in {file_path}")

            sample = data_rows[:20]
            spec = generate_spec(header, sample, bank_hint, llm_fn=self.llm_fn)
            logger.info(f"Generated spec: {json.dumps(spec.to_dict(), indent=2)}")

            transformed = []
            for row in data_rows:
                result = transform_row(row, spec, account_name)
                if result:
                    transformed.append(result)
            logger.info(f"Transformed {len(transformed)} rows")

            is_valid, issues = validate_transformed_data(header, data_rows, transformed)
            if not is_valid:
                logger.warning(f"Validation issues: {issues}")
                results[file_path] = {
                    'success': False,
                    'error': '; '.join(issues),
                    'validation_issues': issues,
                }
                continue

            output_path = Path(kwargs.get('output_dir', 'outputs')) / f"{Path(file_path).stem}_canonical.csv"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            write_canonical_csv(str(output_path), transformed)
            logger.info(f"Wrote canonical CSV to {output_path}")

            results[file_path] = {
                'success': True,
                'output_path': str(output_path),
                'rows_processed': len(transformed),
            }

        return {
            'results': results,
            'summary': f"Processed {len(statement_files)} file(s); {sum(1 for r in results.values() if r.get('success'))} succeeded",
        }


def run(statement_files: str, output_path: str, config_path: str = None, model_override: str = None) -> str:
    """
    PA Skills UI entry point for GnuCash CSV Import.

    Args:
        statement_files: Directory path containing CSV/XLSX files (from multi-file upload).
        output_path: Path for output CSV.
        config_path: Unused (no LLM in this version).
        model_override: Unused.

    Returns:
        Human-readable result string.
    """
    from pathlib import Path as PathlibPath

    try:
        # Get list of statement files from directory
        stmt_dir = PathlibPath(statement_files)
        if not stmt_dir.is_dir():
            return f"Error: {statement_files} is not a directory"

        csv_files = list(stmt_dir.glob("*.csv"))
        xlsx_files = list(stmt_dir.glob("*.xlsx"))
        xls_files = list(stmt_dir.glob("*.xls"))

        all_files = csv_files + xlsx_files + xls_files
        if not all_files:
            return f"Error: No CSV/XLSX files found in {statement_files}"

        print(f"[Import] Found {len(all_files)} statement file(s)")

        # Process files
        agent = GnuCashImportAgent()
        result = agent.invoke(
            statement_files=[str(f) for f in all_files],
            output_dir=PathlibPath(output_path).parent,
        )

        summary = result.get('summary', 'Import complete')
        results = result.get('results', {})

        success_count = sum(1 for r in results.values() if r.get('success'))
        return f"✓ {success_count}/{len(results)} files processed successfully\n\n{summary}"

    except Exception as e:
        return f"Error: {e}"


if __name__ == '__main__':
    import argparse

    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description='GnuCash CSV Import Agent')
    parser.add_argument('statement_files', nargs='+', help='Statement file(s)')
    parser.add_argument('--bank-hint', default='auto', help='Bank hint (ICICI, HDFC, etc.)')
    parser.add_argument('--account-name', default='', help='Account name for output')
    parser.add_argument('--output-dir', default='outputs', help='Output directory')

    args = parser.parse_args()

    agent = GnuCashImportAgent()
    result = agent.invoke(
        statement_files=args.statement_files,
        bank_hint=args.bank_hint,
        account_name=args.account_name,
        output_dir=args.output_dir,
       )

    print(json.dumps(result, indent=2))
