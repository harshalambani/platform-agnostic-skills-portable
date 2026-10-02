#!/usr/bin/env python3
"""GnuCash XML Extractor — Extract description→account mappings from .gnucash files."""

import gzip
import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

NS = {
    'gnc': '{http://www.gnucash.org/XML/gnc}',
    'act': '{http://www.gnucash.org/XML/act}',
    'trn': '{http://www.gnucash.org/XML/trn}',
    'split': '{http://www.gnucash.org/XML/split}',
    'ts': '{http://www.gnucash.org/XML/ts}',
}

BANK_PATTERNS = {
    'ICICI': ['ICICI', 'icici'],
    'HDFC': ['HDFC', 'hdfc'],
    'HSBC': ['HSBC', 'hsbc'],
    'BoB': ['Bank of Baroda', 'BoB', 'bob'],
}

_ROOT_PREFIX = "Root Account:"


def _match_bank(account_name: str) -> Optional[str]:
    """Detect which known bank an account NAME PATTERN belongs to.

    This is a naming heuristic only -- it says nothing about whether the
    account is that bank's own current/savings account versus, say, a term
    deposit or a loan that merely happens to be named after the bank. Callers
    that need "is this the bank's own transactable account" must combine this
    with the account's GnuCash type (see _is_own_bank_account) or with an
    explicitly known account path (see _norm_account_path).
    """
    for bank, patterns in BANK_PATTERNS.items():
        for pattern in patterns:
            if pattern.lower() in account_name.lower():
                return bank
    return None


def _norm_account_path(path: str) -> str:
    """Normalise a GnuCash account path for equality comparisons.

    Some callers pass paths with a leading "Root Account:" and some don't;
    normalise both sides before comparing so an explicitly-named bank account
    matches regardless of which form the caller used.
    """
    if path and path.startswith(_ROOT_PREFIX):
        return path[len(_ROOT_PREFIX):]
    return path or ""


def _is_own_bank_account(acc_path: str, acc_type: Optional[str]) -> Optional[str]:
    """Structural rule for "this split is the bank's own current/savings account".

    A path merely CONTAINING a bank's name is not enough -- a fixed deposit
    ("...Fixed Deposits:ICICI Bank - FD"), a loan, or a credit card can all be
    named after the issuing bank without being that bank's transactable
    current/savings account, and treating any bank-named path as "the bank"
    causes real transfer targets (FD sweeps, inter-bank transfers) to be
    silently skipped -- see the module-level RED FLAG note above
    parse_gnucash_file.

    The sound structural signal GnuCash already gives us is the account's
    <act:type> element: a real current/savings account is booked with type
    BANK. A fixed deposit, loan, or credit card is booked under a different
    type (typically ASSET, LIABILITY, or CREDIT respectively). So an account
    only counts as a bank's own account when BOTH hold: its GnuCash type is
    BANK, AND its name matches a known bank pattern (the pattern says *which*
    bank; the type confirms it is actually that bank's transactable account
    and not merely something named after it).
    """
    if acc_type != 'BANK':
        return None
    return _match_bank(acc_path)


def _is_structural_bank_account(acc_type: Optional[str]) -> bool:
    """RED FLAG fix (round 2, real-book re-measure): whether an account is one
    of THIS BOOK'S OWN bank accounts, for the mapper's self-transfer/IFSC
    literal-code fallback (`own_bank_accounts`), must be purely structural —
    GnuCash type BANK — with NO name-pattern filter at all.

    The coordinator's real book has BANK-type accounts for SBM, Kotak, and
    Barclays, none of which appear in BANK_PATTERNS (that list only names the
    banks this book happens to partition per-bank HISTORY by — ICICI, HDFC,
    HSBC, BoB). `_is_own_bank_account` above intersects type BANK with a name
    match, which is exactly right for "which bank does this account's history
    belong to" but wrong for "is this one of the book's own transactable
    accounts a self-transfer could land on" — that second question has
    nothing to do with whether the account's name happens to match one of the
    four patterns we know how to partition history for. Using
    `_is_own_bank_account` here silently dropped SBM out of
    `own_bank_accounts`, so the IFSC-contradiction guard in the mapper
    couldn't see SBM as "one of the book's own accounts" and never fired on
    the HSBC-IFSC -> SBM misroute it exists to catch.

    The standing rule is implicit/structural learning with no hand-coded bank
    or keyword lists for "own account" — BANK_PATTERNS may keep existing as
    the legacy per-bank partition key for history, never as part of the
    definition of "own account".
    """
    return acc_type == 'BANK'


def _get_account_hierarchy(acc_id: str, account_ids: Dict, account_parents: Dict, visited: Optional[Set] = None) -> str:
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


def _split_amount(value_elem) -> Optional[float]:
    """Parse a GnuCash split <split:value> (plain decimal or N/D rational)."""
    try:
        value_text = (value_elem.text or "0").strip() if value_elem is not None else "0"
        if '/' in value_text:
            numerator, denominator = value_text.split('/')
            return abs(float(numerator) / float(denominator))
        return abs(float(value_text.replace(',', '')))
    except (ValueError, AttributeError, ZeroDivisionError):
        return None


def parse_gnucash_file(gnucash_file: str, gnucash_bank_account: Optional[str] = None) -> Dict[str, Any]:
    """Parse .gnucash XML file and extract description→account mappings.

    RED FLAG (fixed here): the previous version classified any split whose
    account PATH merely *contained* a bank's name (ICICI/HDFC/HSBC/BoB) as
    "the bank" and never as a possible transfer target. That silently broke
    three real cases: (a) a bank<->FD sweep where the FD account is named
    after the bank (e.g. "...Fixed Deposits:ICICI Bank - FD") had BOTH splits
    classified as "bank", so the transaction had no target and was skipped
    entirely; (b) a genuine inter-bank transfer (ICICI<->HDFC, ICICI<->HSBC)
    had a "bank" on both sides and was skipped the same way, so only
    transfers to a bank whose name matched no pattern (e.g. SBM) survived —
    and the self-transfer matcher then "learned" that self-transfers go to
    SBM; (c) a card or loan account named after a bank was never eligible as
    a target either.

    Args:
        gnucash_file: Path to the .gnucash book.
        gnucash_bank_account: Optional, the caller's exact GnuCash account
            path for the importing bank (e.g. from the pipeline's resolved
            bank account). When given, that split is EXACTLY the source for
            any transaction it appears in — regardless of whether the other
            split's name also happens to contain a bank name — and the
            target is the largest other split. This is the precise case that
            silently broke FD sweeps and inter-bank transfers before.
            When omitted, a split is only treated as a bank's own account
            using the structural rule in _is_own_bank_account (GnuCash type
            BANK + name pattern), never a bare name-substring match — see
            that function's docstring for why. A transaction with two own
            bank accounts (e.g. an ICICI<->HDFC transfer) now yields a pair
            in EACH bank's own history, not just the "winning" one.
    """
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
    account_types: Dict[str, str] = {}

    for acc in accounts:
        acc_id_elem = acc.find(f'{NS["act"]}id')
        acc_name_elem = acc.find(f'{NS["act"]}name')
        acc_parent_elem = acc.find(f'{NS["act"]}parent')
        acc_type_elem = acc.find(f'{NS["act"]}type')

        if acc_id_elem is not None and acc_name_elem is not None:
            acc_id = acc_id_elem.text
            account_ids[acc_id] = acc_name_elem.text
            if acc_parent_elem is not None and acc_parent_elem.text:
                account_parents[acc_id] = acc_parent_elem.text
            if acc_type_elem is not None and acc_type_elem.text:
                account_types[acc_id] = acc_type_elem.text

    logger.info(f"Extracted {len(account_ids)} accounts")

    # Extract transactions
    transactions = root.findall(f'.//{NS["gnc"]}transaction')
    logger.info(f"Found {len(transactions)} transactions")

    mappings_by_bank: Dict[str, List[Dict]] = defaultdict(list)
    for _known_bank in BANK_PATTERNS:
        mappings_by_bank[_known_bank] = []  # keep the four known keys present even if empty
    skipped_txns = 0
    norm_forced_account = _norm_account_path(gnucash_bank_account) if gnucash_bank_account else None
    # Every account path this book structurally recognises as a bank's own
    # current/savings account (GnuCash type BANK + name pattern) — collected
    # regardless of forced/fallback classification, so the mapper's
    # self-transfer literal bank-code fallback has the BOOK'S OWN full list
    # of bank accounts to choose among, not just the ones a given
    # transaction's tokens happened to reach (see agent_mapper's
    # _self_transfer_candidates for why that distinction matters).
    own_bank_account_paths: Set[str] = set()

    for txn in transactions:
        # Extract date
        date_posted_elem = txn.find(f'{NS["trn"]}date-posted')
        if date_posted_elem is None:
            skipped_txns += 1
            continue

        date_elem = date_posted_elem.find(f'{NS["ts"]}date')
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
        if splits_container is None:
            skipped_txns += 1
            continue

        splits = splits_container.findall(f'{NS["trn"]}split')
        if len(splits) < 2:
            skipped_txns += 1
            continue

        # Resolve every split's path/amount/classification once, up front, so
        # target selection can compare across ALL other splits — including
        # ones that also match a bank-name pattern (fix for the inter-bank
        # and FD-sweep cases described above).
        splits_info = []  # each: {'path', 'amount', 'forced_source', 'own_bank'}
        for split in splits:
            acc_id_elem = split.find(f'{NS["split"]}account')
            value_elem = split.find(f'{NS["split"]}value')

            if acc_id_elem is None or acc_id_elem.text not in account_ids:
                continue

            acc_path = _get_account_hierarchy(acc_id_elem.text, account_ids, account_parents)
            amount = _split_amount(value_elem)
            if amount is None:
                continue

            forced_source = (
                norm_forced_account is not None
                and _norm_account_path(acc_path) == norm_forced_account
            )
            # Structural check runs regardless of forced_source -- it feeds
            # the book-wide own_bank_account_paths set below, which is a
            # fact about the account itself, not about how THIS transaction
            # happens to be routed.
            #
            # own_bank_account_paths (returned to the mapper as
            # 'own_bank_accounts') uses the PURELY STRUCTURAL type-BANK test
            # (_is_structural_bank_account), with no name-pattern filter --
            # see that function's docstring. This is deliberately a WIDER set
            # than own_bank (below), which still uses the name-pattern-gated
            # _is_own_bank_account because that one drives per-bank HISTORY
            # partitioning (mappings_by_bank), a different concern.
            acc_type = account_types.get(acc_id_elem.text)
            if _is_structural_bank_account(acc_type):
                own_bank_account_paths.add(acc_path)
            structural_own_bank = _is_own_bank_account(acc_path, acc_type)
            own_bank = None if forced_source else structural_own_bank
            splits_info.append({
                'path': acc_path,
                'amount': amount,
                'forced_source': forced_source,
                'own_bank': own_bank,
            })

        if norm_forced_account is not None:
            # Fix #1: the caller told us exactly which account is the bank.
            # That split is the source no matter what the other split's name
            # contains; the target is simply the largest other split.
            forced = [s for s in splits_info if s['forced_source']]
            if not forced:
                skipped_txns += 1
                continue
            source = forced[0]
            others = [s for s in splits_info if s is not source]
            if not others:
                skipped_txns += 1
                continue
            target = max(others, key=lambda s: s['amount'])
            bank_key = _match_bank(source['path']) or 'OWN'
            mappings_by_bank[bank_key].append(
                {'description': description, 'account': target['path'], 'date': date_str}
            )
            continue

        # Fix #2/#3: no explicit account given. Every split that is
        # structurally a bank's own current/savings account (see
        # _is_own_bank_account) gets its own mapping entry, targeting the
        # largest OTHER split in the same transaction — regardless of
        # whether that other split also matches a bank-name pattern. This is
        # what makes an ICICI<->HDFC transfer yield a pair in EACH bank's
        # history instead of just the first one seen.
        own_bank_splits = [s for s in splits_info if s['own_bank']]
        if not own_bank_splits:
            skipped_txns += 1
            continue

        matched_any = False
        for source in own_bank_splits:
            others = [s for s in splits_info if s is not source]
            if not others:
                continue
            target = max(others, key=lambda s: s['amount'])
            mappings_by_bank[source['own_bank']].append(
                {'description': description, 'account': target['path'], 'date': date_str}
            )
            matched_any = True

        if not matched_any:
            skipped_txns += 1

    logger.info(f"Extracted mappings; skipped {skipped_txns}")

    # Aggregate mappings
    def aggregate(mappings):
        desc_to_accs = {}
        for m in mappings:
            key = (m['description'], m['account'])
            if key not in desc_to_accs:
                desc_to_accs[key] = {'description': m['description'], 'account': m['account'], 'frequency': 0, 'last_date': m['date'], 'dates': []}
            desc_to_accs[key]['frequency'] += 1
            desc_to_accs[key]['dates'].append(m['date'])   # MAP-31: additive; frequency/last_date unchanged
            desc_to_accs[key]['last_date'] = max(desc_to_accs[key]['last_date'], m['date'])

        result = []
        for (desc, acc), data in desc_to_accs.items():
            result.append({'description': desc, 'account': acc, 'frequency': data['frequency'], 'last_date': data['last_date'],
                           'dates': sorted(data['dates'])})
        return sorted(result, key=lambda x: x['frequency'], reverse=True)

    aggregated = {bank: aggregate(mappings) if mappings else [] for bank, mappings in mappings_by_bank.items()}

    return {
        'account_tree': account_ids,
        'mappings': aggregated,
        'own_bank_accounts': sorted(own_bank_account_paths),
        'metadata': {
            'gnucash_file': str(gnucash_path),
            'extraction_date': datetime.now().isoformat(),
            'total_accounts': len(account_ids),
            'total_transactions_parsed': len(transactions),
            'total_transactions_skipped': skipped_txns,
            'partition_summary': {bank: len(mappings) for bank, mappings in aggregated.items() if mappings}
        }
    }


def run(gnucash_files: list, output_path: str, **kwargs) -> dict:
    """Entry point for Cowork skill runner.

    Args:
        gnucash_files: List of paths to .gnucash files
        output_path: Directory to write output files
        **kwargs: Additional arguments from skill runner

    Returns:
        Dict with output file paths and status
    """
    logging.basicConfig(level=logging.INFO)
    results = []
    gnucash_bank_account = kwargs.get('gnucash_bank_account')

    for gnucash_file in gnucash_files:
        try:
            print(f"🔄 Extracting: {Path(gnucash_file).name}")
            result = parse_gnucash_file(gnucash_file, gnucash_bank_account=gnucash_bank_account)

            # Write output JSON
            output_name = Path(gnucash_file).stem + '_extract.json'
            output_file = Path(output_path) / output_name
            with open(output_file, 'w') as f:
                json.dump(result, f, indent=2)

            results.append({
                'status': 'success',
                'input': str(gnucash_file),
                'output': str(output_file),
                'accounts': result['metadata']['total_accounts'],
                'transactions_parsed': result['metadata']['total_transactions_parsed'],
                'transactions_skipped': result['metadata']['total_transactions_skipped']
            })
            print(f"✓ Extracted to {output_file}")
        except Exception as e:
            results.append({
                'status': 'error',
                'input': str(gnucash_file),
                'error': str(e)
            })
            print(f"✗ Error: {e}")

    return {
        'success': all(r['status'] == 'success' for r in results),
        'results': results
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
