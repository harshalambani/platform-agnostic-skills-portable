#!/usr/bin/env python3
"""GnuCash XML Extractor — Extract description→account mappings from .gnucash files."""

import gzip
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

NS = {
    'gnc': '{http://www.gnucash.org/XML/gnc}',
    'act': '{http://www.gnucash.org/XML/act}',
    'trn': '{http://www.gnucash.org/XML/trn}',
}

BANK_PATTERNS = {
    'ICICI': ['ICICI', 'icici'],
    'HDFC': ['HDFC', 'hdfc'],
    'HSBC': ['HSBC', 'hsbc'],
    'BoB': ['Bank of Baroda', 'BoB', 'bob'],
}

def _match_bank(account_name: str) -> Optional[str]:
    """Detect bank type from account name."""
    for bank, patterns in BANK_PATTERNS.items():
        for pattern in patterns:
            if pattern.lower() in account_name.lower():
                return bank
    return None

def _get_account_hierarchy(acc_id: str, account_ids: Dict, account_parents: Dict,
                          visited: Optional[Set] = None) -> str:
    """Build full account path."""
    if visited is None:
        visited = set()
    if acc_id in visited or acc_id not in account_ids:
        return account_ids.get(acc_id, acc_id)

    visited.add(acc_id)
    acc_name = account_ids.get(acc_id, acc_id)
    parent_id = account_parents.get(acc_id)

    if parent_id and parent_id in account_ids:
        parent_path = _get_account_hierarchy(parent_id, account_ids, account_parents, visited)
        return f"{parent_path}:{acc_name}"
    return acc_name

def parse_gnucash_file(gnucash_file: str) -> Dict[str, Any]:
    """Parse .gnucash XML file and extract mappings."""
    logger.info(f"Parsing {gnucash_file}")
    gnucash_path = Path(gnucash_file)
    if not gnucash_path.exists():
        raise FileNotFoundError(f"File not found: {gnucash_file}")

    with gzip.open(gnucash_path, 'rt', encoding='utf-8') as f:
        tree = ET.parse(f)
    root = tree.getroot()

    # Extract accounts
    accounts = root.findall(f'.//{NS["gnc"]}account')
    account_ids = {}
    account_parents = {}

    for acc in accounts:
        acc_id_elem = acc.find(f'{NS["act"]}id')
        acc_name_elem = acc.find(f'{NS["act"]}name')
        acc_parent_elem = acc.find(f'{NS["act"]}parent')

        if acc_id_elem is not None and acc_name_elem is not None:
            acc_id = acc_id_elem.text
            account_ids[acc_id] = acc_name_elem.text
            if acc_parent_elem is not None and acc_parent_elem.text:
                account_parents[acc_id] = acc_parent_elem.text

    logger.info(f"Extracted {len(account_ids)} accounts")

    # Extract transactions
    transactions = root.findall(f'.//{NS["gnc"]}transaction')
    logger.info(f"Found {len(transactions)} transactions")

    mappings_by_bank = {'ICICI': [], 'HDFC': [], 'HSBC': [], 'BoB': [], '_other': []}
    skipped_txns = 0

    for txn in transactions:
        # Extract date
        date_posted_elem = txn.find(f'{NS["trn"]}date-posted')
        if date_posted_elem is None:
            skipped_txns += 1
            continue

        date_elem = date_posted_elem.find('{http://www.gnucash.org/XML/ts}date')
        if date_elem is None or not date_elem.text:
            skipped_txns += 1
            continue

        try:
            date_str = date_elem.text.strip().split()[0]
            datetime.strptime(date_str, '%Y-%m-%d')
        except (ValueError, IndexError):
            skipped_txns += 1
            continue

        # Extract description
        description_elem = txn.find(f'{NS["trn"]}description')
        description = description_elem.text if description_elem is not None else ""

        # Extract splits
        splits_container = txn.find(f'{NS["trn"]}splits')
        if splits_container is None or len(list(splits_container)) < 2:
            skipped_txns += 1
            continue

        # Find source bank and target account
        source_bank = None
        source_account = None
        target_split = None
        target_amount = 0

        splits = splits_container.findall(f'{NS["trn"]}split')
        for split in splits:
            acc_id_elem = split.find(f'{NS["trn"]}account')
            value_elem = split.find(f'{NS["trn"]}value')

            if acc_id_elem is None or acc_id_elem.text not in account_ids:
                continue

            acc_path = _get_account_hierarchy(acc_id_elem.text, account_ids, account_parents)
            bank = _match_bank(acc_path)

            if bank:
                if source_bank is None:
                    source_bank = bank
                    source_account = acc_path
            else:
                try:
                    # Value is in fraction format: "9500000/100" = 95000
                    value_text = (value_elem.text or "0").strip()
                    if '/' in value_text:
                        numerator, denominator = value_text.split('/')
                        amount = abs(float(numerator) / float(denominator))
                    else:
                        amount = abs(float(value_text.replace(',', '')))

                    if amount > target_amount:
                        target_amount = amount
                        target_split = acc_path
                except (ValueError, AttributeError, ZeroDivisionError):
                    pass

        if source_bank is None or target_split is None:
            skipped_txns += 1
            continue

        # Add mapping
        mapping = {
            'description': description,
            'account': target_split,
            'date': date_str,
        }
        mappings_by_bank[source_bank].append(mapping)

    logger.info(f"Extracted mappings; skipped {skipped_txns}")

    # Aggregate mappings
    def aggregate(mappings):
        desc_to_accs = {}
        for m in mappings:
            key = (m['description'], m['account'])
            if key not in desc_to_accs:
                desc_to_accs[key] = {'description': m['description'], 'account': m['account'], 'frequency': 0, 'last_date': m['date']}
            desc_to_accs[key]['frequency'] += 1
            desc_to_accs[key]['last_date'] = max(desc_to_accs[key]['last_date'], m['date'])

        result = []
        for (desc, acc), data in desc_to_accs.items():
            result.append({'description': desc, 'account': acc, 'frequency': data['frequency'], 'last_date': data['last_date']})
        return sorted(result, key=lambda x: x['frequency'], reverse=True)

    aggregated = {bank: aggregate(mappings) for bank, mappings in mappings_by_bank.items()}

    return {
        'account_tree': account_ids,
        'mappings': aggregated,
        'metadata': {
            'gnucash_file': str(gnucash_path),
            'extraction_date': datetime.now().isoformat(),
            'total_accounts': len(account_ids),
            'total_transactions_parsed': len(transactions),
            'total_transactions_skipped': skipped_txns,
            'partition_summary': {bank: len(mappings) for bank, mappings in aggregated.items() if mappings}
        }
    }

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 3:
        print("Usage: python agent.py <gnucash_file> <output_json>")
        sys.exit(1)
    result = parse_gnucash_file(sys.argv[1])
    with open(sys.argv[2], 'w') as f:
        json.dump(result, f, indent=2)
    print("✓ Done")
