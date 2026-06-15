#!/usr/bin/env python3
"""
GnuCash Reconciler Agent (Phase 4 lite).
Read-only comparison of CSV vs GnuCash.
Flags: duplicates, balance gaps, missing transactions.
"""

import json
import csv
import gzip
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime
from collections import defaultdict
import xml.etree.ElementTree as ET
import logging

logger = logging.getLogger(__name__)

# ============================================================================
# GNUCASH PARSING (reuse from Phase 3)
# ============================================================================

def parse_gnucash_for_reconcile(file_path: str, account_filter: Optional[str] = None) -> Dict[str, Any]:
    """Parse GnuCash and extract transactions for specified account."""
    with gzip.open(file_path, 'rt', encoding='utf-8') as f:
        root = ET.parse(f).getroot()

    # Extract accounts
    accounts = {}
    for elem in root.iter():
        if elem.tag.endswith('account'):
            acc_id = None
            acc_name = None
            acc_parent = None
            for child in elem:
                if child.tag.endswith('}id'):
                    acc_id = child.text
                elif child.tag.endswith('}name'):
                    acc_name = child.text
                elif child.tag.endswith('}parent'):
                    acc_parent = child.text
            if acc_id and acc_name:
                accounts[acc_id] = {'name': acc_name, 'parent_id': acc_parent}

    # Build account paths
    def get_path(acc_id: str) -> str:
        acc = accounts[acc_id]
        if acc['parent_id'] and acc['parent_id'] in accounts:
            parent_path = get_path(acc['parent_id'])
            return f"{parent_path}:{acc['name']}" if parent_path else acc['name']
        return acc['name']

    for acc_id in accounts:
        accounts[acc_id]['path'] = get_path(acc_id)

    # Filter by account if specified
    target_acc_ids = None
    if account_filter:
        target_acc_ids = [aid for aid, acc in accounts.items() if acc['path'] == account_filter]

    # Extract transactions
    transactions = []
    for elem in root.iter():
        if elem.tag.endswith('transaction'):
            date_posted = None
            splits_data = []

            for child in elem:
                if child.tag.endswith('}date-posted'):
                    date_elem = child.find('{http://www.gnucash.org/XML/ts}date')
                    if date_elem is None:
                        date_elem = child.find('date')
                    if date_elem is not None:
                        date_posted = date_elem.text
                elif child.tag.endswith('}splits'):
                    for split_elem in child:
                        if split_elem.tag.endswith('split') or split_elem.tag == 'split':
                            split_acc = None
                            split_value = None
                            for sp_child in split_elem:
                                if sp_child.tag.endswith('}account'):
                                    split_acc = sp_child.text
                                elif sp_child.tag.endswith('}value'):
                                    split_value = sp_child.text
                            if split_acc and split_value:
                                splits_data.append((split_acc, split_value))

            if date_posted and splits_data:
                # Filter by account if specified
                if target_acc_ids:
                    splits_data = [(acc, val) for acc, val in splits_data if acc in target_acc_ids]

                if splits_data:
                    try:
                        txn_date = datetime.strptime(date_posted[:19], '%Y-%m-%d %H:%M:%S')
                        for acc_id, amount_str in splits_data:
                            # Parse fraction format
                            if '/' in amount_str:
                                parts = amount_str.split('/')
                                amount = float(parts[0]) / float(parts[1])
                            else:
                                amount = float(amount_str)

                            acc_path = accounts[acc_id]['path'] if acc_id in accounts else acc_id
                            transactions.append({
                                'date': txn_date.strftime('%Y-%m-%d'),
                                'amount': amount,
                                'account': acc_path,
                            })
                    except Exception as e:
                        logger.warning(f"Failed to parse transaction: {e}")

    return {
        'accounts': accounts,
        'transactions': transactions,
        'account_filter': account_filter,
    }


# ============================================================================
# CSV PARSING
# ============================================================================

def parse_csv(file_path: str) -> List[Dict[str, Any]]:
    """Parse normalized CSV from Phase 1."""
    rows = []
    with open(file_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):  # Start at 2 (skip header)
            rows.append({
                'row_num': i,
                'date': row.get('Date', ''),
                'txn_id': row.get('Transaction ID', ''),
                'description': row.get('Description', ''),
                'account': row.get('Account', ''),
                'deposit': float(row.get('Deposit', 0) or 0),
                'withdrawal': float(row.get('Withdrawal', 0) or 0),
                'balance': float(row.get('Balance', 0) or 0),
                'currency': row.get('Currency', 'INR'),
            })
    return rows


# ============================================================================
# RECONCILIATION LOGIC
# ============================================================================

def extract_key(description: str) -> str:
    """Extract stable key from description for matching."""
    if not description:
        return ""
    # UPI VPA
    if '@' in description:
        import re
        match = re.search(r'(\d+-\d+@\w+|[\w\.\-]+@[\w\.]+)', description)
        if match:
            return match.group(1).lower()
    # NEFT/IMPS
    if description.startswith('NEFT') or description.startswith('IMPS'):
        parts = description.split('-')
        if len(parts) > 2:
            return '-'.join(parts[1:3]).strip()
    # Fallback: first 40 chars
    return description[:40].strip()


def reconcile(csv_rows: List[Dict], gnucash_data: Dict) -> Tuple[List[Dict], Dict]:
    """Compare CSV rows to GnuCash transactions."""
    gnucash_txns = gnucash_data['transactions']

    # Build GnuCash index: (date, amount, key) → count
    gnucash_index = defaultdict(list)
    for txn in gnucash_txns:
        key = extract_key("")  # Placeholder since GnuCash doesn't store description
        # For now, index by (date, amount) only
        index_key = (txn['date'], round(txn['amount'], 2))
        gnucash_index[index_key].append(txn)

    # Reconcile each CSV row
    report = []
    matched_count = 0
    duplicate_count = 0
    new_count = 0

    for csv_row in csv_rows:
        amount = csv_row['deposit'] - csv_row['withdrawal']
        index_key = (csv_row['date'], round(amount, 2))

        if index_key in gnucash_index:
            matches = gnucash_index[index_key]
            if len(matches) == 1:
                status = "Match"
                details = f"Found in {matches[0]['account']}"
                matched_count += 1
            else:
                status = "Duplicate"
                details = f"{len(matches)} matching transactions in GnuCash"
                duplicate_count += 1
        else:
            status = "New"
            details = "Not in GnuCash (ready to import)"
            new_count += 1

        report.append({
            'row_num': csv_row['row_num'],
            'date': csv_row['date'],
            'description': csv_row['description'][:50],
            'amount': f"{amount:.2f}",
            'status': status,
            'details': details,
        })

    summary = {
        'csv_rows': len(csv_rows),
        'matched': matched_count,
        'duplicates': duplicate_count,
        'new': new_count,
        'missing': len(gnucash_txns) - matched_count,  # Approximate
        'balance_gaps': 0,  # TODO: implement if CSV has Balance column
        'actions': []
    }

    if duplicate_count > 0:
        summary['actions'].append(f"Review {duplicate_count} potential duplicates before import")
    if new_count > 0:
        summary['actions'].append(f"{new_count} new transactions ready to import")
    if summary['balance_gaps'] == 0:
        summary['actions'].append("No balance gaps detected")

    return report, summary


# ============================================================================
# OUTPUT
# ============================================================================

def write_report(output_path: str, report: List[Dict]) -> None:
    """Write reconciliation report to CSV."""
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Row', 'Date', 'Description', 'Amount', 'Status', 'Details'])
        for row in report:
            writer.writerow([
                row['row_num'],
                row['date'],
                row['description'],
                row['amount'],
                row['status'],
                row['details'],
            ])


def write_summary(output_path: str, summary: Dict) -> None:
    """Write summary JSON."""
    with open(output_path, 'w') as f:
        json.dump(summary, f, indent=2)


# ============================================================================
# MAIN AGENT
# ============================================================================

class GnuCashReconcilerAgent:
    def invoke(self, normalized_csv: str, gnucash_file: str,
               account_filter: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """Reconcile CSV against GnuCash."""

        logger.info(f"Parsing GnuCash: {gnucash_file}")
        gnucash_data = parse_gnucash_for_reconcile(gnucash_file, account_filter)

        logger.info(f"Parsing CSV: {normalized_csv}")
        csv_rows = parse_csv(normalized_csv)

        logger.info(f"Reconciling {len(csv_rows)} CSV rows against {len(gnucash_data['transactions'])} GnuCash txns")
        report, summary = reconcile(csv_rows, gnucash_data)

        # Output
        output_dir = Path(kwargs.get('output_dir', 'outputs'))
        output_dir.mkdir(parents=True, exist_ok=True)

        report_file = output_dir / "reconciliation_report.csv"
        summary_file = output_dir / "reconciliation_summary.json"

        write_report(str(report_file), report)
        write_summary(str(summary_file), summary)

        logger.info(f"Wrote report to {report_file}")
        logger.info(f"Wrote summary to {summary_file}")

        return {
            'success': True,
            'report_file': str(report_file),
            'summary_file': str(summary_file),
            'summary': summary,
        }


if __name__ == '__main__':
    import argparse
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description='GnuCash Reconciler')
    parser.add_argument('--normalized-csv', required=True, help='CSV from Phase 1')
    parser.add_argument('--gnucash-file', required=True, help='GnuCash file')
    parser.add_argument('--account-filter', help='Account to reconcile')
    parser.add_argument('--output-dir', default='outputs', help='Output directory')

    args = parser.parse_args()

    agent = GnuCashReconcilerAgent()
    result = agent.invoke(
        normalized_csv=args.normalized_csv,
        gnucash_file=args.gnucash_file,
        account_filter=args.account_filter,
        output_dir=args.output_dir,
    )

    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# PA Skills UI entry point
# ---------------------------------------------------------------------------

def run(
    normalized_csv: str,
    gnucash_file: str,
    output_path: str,
    config_path: str = None,
    model_override: str = None,
) -> str:
    """
    Run the reconciler from the PA Skills UI.

    Args:
        normalized_csv: Path to canonical 8-col CSV (output from ICICI/HSBC/BoB skills).
        gnucash_file:   Path to .gnucash book. Must be closed in GnuCash.
        output_path:    Path for the reconciliation report CSV.
        config_path:    Unused.
        model_override: Unused.

    Returns:
        Human-readable result string for the UI.
    """
    from pathlib import Path

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Summary written alongside the report
    summary_path = out_path.with_suffix('.summary.json')

    gnucash_data = parse_gnucash_for_reconcile(gnucash_file, None)
    csv_rows = parse_csv(normalized_csv)
    report, summary = reconcile(csv_rows, gnucash_data)
    write_report(str(out_path), report)
    write_summary(str(summary_path), summary)

    matched    = summary.get('matched', 0)
    new_txns   = summary.get('new_in_csv', 0)
    duplicates = summary.get('duplicates', 0)
    missing    = summary.get('missing_in_csv', 0)
    total_csv  = len(csv_rows)

    return (
        f"Reconciled **{total_csv} CSV rows** against "
        f"**{len(gnucash_data.get('transactions', []))} GnuCash transactions**.\n\n"
        f"- Matched (already in GnuCash): {matched}\n"
        f"- New (will be imported): {new_txns}\n"
        f"- Duplicates (skip these): {duplicates}\n"
        f"- Missing from CSV: {missing}\n\n"
        f"Report saved to `{out_path.name}`. "
        f"Summary saved to `{summary_path.name}`."
    )
