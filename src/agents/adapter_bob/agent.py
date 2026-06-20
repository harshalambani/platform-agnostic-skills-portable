#!/usr/bin/env python3
"""
Bank of Baroda CSV → Canonical Schema Adapter.

Maps skill_bob's native CSV output to the canonical 8-column schema for Phase 3/4/6 consumption.

Input:  skill_bob CSV with columns: DATE, PARTICULARS, CHQ.NO., WITHDRAWALS, DEPOSITS, BALANCE
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


def parse_date_bob(date_str: str) -> Optional[str]:
    """Parse BoB date format DD-MM-YY to ISO YYYY-MM-DD."""
    if not date_str or not date_str.strip():
        return None
    date_str = str(date_str).strip()
    try:
        dt = datetime.strptime(date_str, '%d-%m-%y')
        return dt.strftime('%Y-%m-%d')
    except ValueError:
        try:
            dt = datetime.strptime(date_str, '%d-%m-%Y')
            return dt.strftime('%Y-%m-%d')
        except ValueError:
            return None


def parse_indian_number(num_str: str) -> str:
    """Parse Indian number format (lakh/crore: 1,23,456.78) to decimal string."""
    if not num_str or not str(num_str).strip():
        return '0'
    num_str = str(num_str).strip()
    try:
        num = float(num_str.replace(',', ''))
        return str(num)
    except ValueError:
        return '0'


def adapt_bob_csv(input_csv: str, output_csv: str) -> Dict[str, Any]:
    """Transform skill_bob CSV to canonical schema."""
    issues = []

    try:
        with open(input_csv, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            bob_rows = list(reader)
    except Exception as e:
        return {'success': False, 'error': f"Failed to read input CSV: {e}"}

    if not bob_rows:
        return {'success': False, 'error': 'No data rows in input CSV'}

    canonical_rows = []
    for i, bob_row in enumerate(bob_rows, 1):
        try:
            date_str = parse_date_bob(bob_row.get('DATE', ''))
            if not date_str:
                issues.append(f"Row {i}: failed to parse date '{bob_row.get('DATE', '')}'")
                continue

            txn_id = str(bob_row.get('CHQ.NO.', '')).strip() or ''
            description = str(bob_row.get('PARTICULARS', '')).strip()

            # Skip synthetic "Opening Balance" row (no real transaction)
            if 'opening balance' in description.lower():
                continue

            withdrawal = parse_indian_number(bob_row.get('WITHDRAWALS', '0'))
            deposit = parse_indian_number(bob_row.get('DEPOSITS', '0'))
            balance = parse_indian_number(bob_row.get('BALANCE', '0'))

            canonical_rows.append({
                'Date': date_str,
                'Transaction ID': txn_id,
                'Description': description,
                'Account': '',
                'Deposit': deposit,
                'Withdrawal': withdrawal,
                'Balance': balance,
                'Currency': 'INR',
            })
        except Exception as e:
            issues.append(f"Row {i}: {e}")
            continue

    if not canonical_rows:
        return {'success': False, 'error': 'No valid rows after transformation'}

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
        return {'success': False, 'error': f"Failed to write output CSV: {e}"}

    return {
        'success': len(issues) == 0,
        'output_path': output_csv,
        'rows_input': len(bob_rows),
        'rows_output': len(canonical_rows),
        'issues': issues,
        'balance_summary': format_balance_summary(running),
        'opening_balance': running['opening_balance'],
        'closing_balance': running['closing_balance'],
    }


class BoBAdapterAgent:
    """Bank of Baroda CSV → Canonical Schema Adapter."""

    def invoke(self, input_file: str, output_dir: str = 'outputs', **kwargs) -> Dict[str, Any]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        input_path = Path(input_file)
        output_file = output_dir / f"{input_path.stem}_canonical.csv"
        result = adapt_bob_csv(input_file, str(output_file))
        return result


if __name__ == '__main__':
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python agent.py <input_csv> [output_dir]")
        sys.exit(1)

    input_csv = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else 'outputs'
    agent = BoBAdapterAgent()
    result = agent.invoke(input_csv, output_dir)
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
    Run the BoB adapter from the PA Skills UI.

    Args:
        input_file:     Path to skill_bob output CSV.
        output_path:    Path for the canonical output CSV.
        config_path:    Unused.
        model_override: Unused.

    Returns:
        Human-readable result string for the UI.
    """
    from pathlib import Path
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    result = adapt_bob_csv(input_file, output_path)

    if result.get('success'):
        rows_in  = result.get('rows_input', '?')
        rows_out = result.get('rows_output', '?')
        bal = result.get('balance_summary', '')
        return (
            f"Converted **{rows_in} BoB rows** → **{rows_out} canonical rows**.\n\n"
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
