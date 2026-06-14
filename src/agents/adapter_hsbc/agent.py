#!/usr/bin/env python3
"""
HSBC Excel → Canonical Schema Adapter.

Maps skill_hsbc's enriched .xlsx output to the canonical 8-column schema for Phase 3/4/6 consumption.

Input:  skill_hsbc .xlsx workbook (enriched with transaction data)
Output: Canonical 8-column CSV: Date, Transaction ID, Description, Account, Deposit, Withdrawal, Balance, Currency
"""

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.balance_utils import (
    verify_running_balance,
    format_balance_summary,
)

log = logging.getLogger(__name__)

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


def parse_date_hsbc(date_val: Any) -> Optional[str]:
    """Parse HSBC date (can be datetime object or string) to ISO YYYY-MM-DD.

    Args:
        date_val: Date value from Excel (datetime object or string)

    Returns:
        ISO YYYY-MM-DD string, or None if parse fails
    """
    if not date_val:
        return None

    # If already a datetime object (from openpyxl)
    if isinstance(date_val, datetime):
        return date_val.strftime('%Y-%m-%d')

    # Parse string formats
    date_str = str(date_val).strip()
    if not date_str:
        return None

    # Try common HSBC date formats
    for fmt in ['%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d', '%d-%m-%Y', '%d-%m-%y']:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            continue

    return None


def parse_indian_number(num_val: Any) -> str:
    """Parse number (can be float, int, or Indian format string) to decimal string.

    Args:
        num_val: Number value (float, int, or string in Indian format)

    Returns:
        Decimal string
    """
    if num_val is None or (isinstance(num_val, str) and not num_val.strip()):
        return '0'

    try:
        # If numeric, convert directly
        if isinstance(num_val, (int, float)):
            return str(float(num_val))

        # If string, remove commas and convert
        num_str = str(num_val).strip()
        num = float(num_str.replace(',', ''))
        return str(num)
    except (ValueError, AttributeError):
        return '0'


def adapt_hsbc_xlsx_pandas(input_xlsx: str, output_csv: str) -> Dict[str, Any]:
    """Transform skill_hsbc .xlsx to canonical schema using pandas.

    Args:
        input_xlsx: Path to skill_hsbc output .xlsx
        output_csv: Path to write canonical CSV

    Returns:
        Result dict with success status, row counts, and issues
    """
    issues = []

    try:
        # Read the first sheet (or 'Transactions' if it exists)
        xls = pd.ExcelFile(input_xlsx)
        sheet_name = 'Transactions' if 'Transactions' in xls.sheet_names else xls.sheet_names[0]

        df = pd.read_excel(input_xlsx, sheet_name=sheet_name)
    except Exception as e:
        return {
            'success': False,
            'error': f"Failed to read Excel file: {e}",
        }

    if df.empty:
        return {
            'success': False,
            'error': 'No data in Excel sheet',
        }

    # Map HSBC columns to canonical
    # Expected HSBC columns: Date, Particulars, Debit, Credit, Balance, etc.
    # Map: Date → Date, Debit → Withdrawal, Credit → Deposit, Balance → Balance, etc.
    canonical_rows = []

    for i, (idx, row) in enumerate(df.iterrows(), 1):
        try:
            # Date parsing
            date_col = next((col for col in ['Date', 'Transaction Date', 'date'] if col in df.columns), None)
            if date_col:
                date_str = parse_date_hsbc(row.get(date_col, ''))
            else:
                date_str = None

            if not date_str:
                issues.append(f"Row {i}: failed to parse date")
                continue

            # Description
            desc_col = next((col for col in ['Particulars', 'Description', 'details'] if col in df.columns), None)
            description = str(row.get(desc_col, '')).strip() if desc_col else ''

            # Transaction ID (cheque/ref number)
            txn_id_col = next((col for col in ['Cheque', 'Ref', 'Reference', 'cheque'] if col in df.columns), None)
            txn_id = str(row.get(txn_id_col, '')).strip() if txn_id_col else ''

            # Amounts — look for Debit/Credit or Withdrawal/Deposit columns
            withdrawal = '0'
            deposit = '0'

            # Try Debit/Credit first
            debit_col = next((col for col in ['Debit', 'Withdrawal', 'debit'] if col in df.columns), None)
            credit_col = next((col for col in ['Credit', 'Deposit', 'credit'] if col in df.columns), None)

            if debit_col:
                withdrawal = parse_indian_number(row.get(debit_col, '0'))
            if credit_col:
                deposit = parse_indian_number(row.get(credit_col, '0'))

            # Balance
            balance_col = next((col for col in ['Balance', 'balance'] if col in df.columns), None)
            balance = parse_indian_number(row.get(balance_col, '0')) if balance_col else '0'

            canonical_rows.append({
                'Date': date_str,
                'Transaction ID': txn_id,
                'Description': description,
                'Account': '',  # Empty — Phase 3 mapper fills this
                'Deposit': deposit,
                'Withdrawal': withdrawal,
                'Balance': balance,
                'Currency': 'INR',
            })
        except Exception as e:
            issues.append(f"Row {i}: {e}")
            continue

    if not canonical_rows:
        return {
            'success': False,
            'error': 'No valid rows after transformation',
        }

    # ── Balance verification ──────────────────────────────────────────────
    running = verify_running_balance(canonical_rows)
    if not running["ok"]:
        for d in running["details"]:
            issues.append(f"Balance: {d}")

    # Write output CSV
    try:
        output_path = Path(output_csv)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'Date', 'Transaction ID', 'Description', 'Account',
                'Deposit', 'Withdrawal', 'Balance', 'Currency'
            ])
            writer.writeheader()
            writer.writerows(canonical_rows)
    except Exception as e:
        return {
            'success': False,
            'error': f"Failed to write output CSV: {e}",
        }

    return {
        'success': len(issues) == 0,
        'output_path': output_csv,
        'rows_input': len(df),
        'rows_output': len(canonical_rows),
        'issues': issues,
        'balance_summary': format_balance_summary(running),
        'opening_balance': running['opening_balance'],
        'closing_balance': running['closing_balance'],
    }


def adapt_hsbc_xlsx(input_xlsx: str, output_csv: str) -> Dict[str, Any]:
    """Transform skill_hsbc .xlsx to canonical schema.

    Uses pandas if available, otherwise openpyxl.

    Args:
        input_xlsx: Path to skill_hsbc output .xlsx
        output_csv: Path to write canonical CSV

    Returns:
        Result dict with success status, row counts, and issues
    """
    if HAS_PANDAS:
        return adapt_hsbc_xlsx_pandas(input_xlsx, output_csv)
    else:
        return {
            'success': False,
            'error': 'pandas is required for HSBC adapter (install: pip install pandas openpyxl)',
        }


class HSBCAdapterAgent:
    """HSBC Excel → Canonical Schema Adapter."""

    def invoke(self, input_file: str, output_dir: str = 'outputs', **kwargs) -> Dict[str, Any]:
        """Run the HSBC adapter.

        Args:
            input_file: Path to skill_hsbc output .xlsx
            output_dir: Output directory

        Returns:
            Result dict with success status and output path
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        input_path = Path(input_file)
        output_file = output_dir / f"{input_path.stem}_canonical.csv"

        result = adapt_hsbc_xlsx(input_file, str(output_file))

        return result


if __name__ == '__main__':
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python agent.py <input_xlsx> [output_dir]")
        sys.exit(1)

    input_xlsx = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else 'outputs'

    agent = HSBCAdapterAgent()
    result = agent.invoke(input_xlsx, output_dir)

    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# PA Skills UI entry point
# ---------------------------------------------------------------------------

def run(
    input_file: str,
    output_path: str,
    config_path: str = None,
    model_override: str = None,
) -> str:
    """
    Run the HSBC adapter from the PA Skills UI.

    Args:
        input_file:     Path to skill_hsbc enriched .xlsx workbook.
        output_path:    Path for the canonical output CSV.
        config_path:    Unused.
        model_override: Unused.

    Returns:
        Human-readable result string for the UI.
    """
    from pathlib import Path
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    # Try pandas path first; fall back to openpyxl-only path
    try:
        import pandas  # noqa: F401
        result = adapt_hsbc_xlsx_pandas(input_file, output_path)
    except ImportError:
        result = adapt_hsbc_xlsx(input_file, output_path)

    if result.get('success'):
        rows_in  = result.get('rows_input', '?')
        rows_out = result.get('rows_output', '?')
        bal = result.get('balance_summary', '')
        return (
            f"Converted **{rows_in} HSBC rows** → **{rows_out} canonical rows**.\n\n"
            f"Saved to `{Path(output_path).name}`.\n\n"
            f"{bal}\n\n"
            "Next: upload this CSV to the **GnuCash — Map Accounts** skill."
        )
    else:
        issues = "\n".join(f"- {i}" for i in result.get('issues', []))
        bal = result.get('balance_summary', '')
        return (
            f"ERROR: {result.get('error', 'Unknown error')}\n\n"
            + (f"Issues:\n{issues}\n\n" if issues else "")
            + (f"{bal}" if bal else "")
        )
