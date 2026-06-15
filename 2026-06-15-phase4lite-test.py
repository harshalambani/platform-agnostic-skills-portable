#!/usr/bin/env python3
"""
Phase 4 Lite Test — Verify duplicate detection integration.

Tests that the pipeline correctly:
1. Detects duplicate transactions (date + amount match)
2. Filters them out before account mapping
3. Handles edge cases (all duplicates, no duplicates, errors)
"""

import sys
import csv
import tempfile
from pathlib import Path

# Add src to path
SRC = Path(__file__).parent / "src"
sys.path.insert(0, str(SRC))

from agents.skill_gnucash_reconciler.agent import reconcile, parse_gnucash_for_reconcile

def test_duplicate_filtering():
    """Test that reconcile() correctly identifies new vs duplicate rows."""
    print("TEST: Duplicate filtering logic")
    print("-" * 70)

    # Mock CSV rows
    csv_rows = [
        {
            'row_num': 1,
            'date': '2026-06-01',
            'description': 'Transfer to Savings',
            'deposit': 5000.0,
            'withdrawal': 0.0,
        },
        {
            'row_num': 2,
            'date': '2026-06-02',
            'description': 'Purchase - Store ABC',
            'deposit': 0.0,
            'withdrawal': 1250.50,
        },
        {
            'row_num': 3,
            'date': '2026-06-03',
            'description': 'Salary Credit',
            'deposit': 50000.0,
            'withdrawal': 0.0,
        },
    ]

    # Mock GnuCash data with one duplicate (row 1 already in GnuCash)
    gnucash_data = {
        'transactions': [
            {
                'date': '2026-06-01',
                'amount': 5000.0,  # Matches row 1 (deposit - withdrawal)
                'account': 'Assets:Savings',
            },
        ]
    }

    # Run reconciliation
    report, summary = reconcile(csv_rows, gnucash_data)

    print(f"CSV rows: {len(csv_rows)}")
    print(f"GnuCash transactions: {len(gnucash_data['transactions'])}")
    print(f"Report entries: {len(report)}")
    print()

    # Check results
    statuses = [r['status'] for r in report]
    matched = sum(1 for s in statuses if s == 'Match')
    new = sum(1 for s in statuses if s == 'New')

    print("Reconciliation results:")
    for i, r in enumerate(report, 1):
        print(f"  Row {i}: {r['date']} | {r['amount']:>10} | {r['status']:<10} | {r['details']}")
    print()

    print(f"Summary:")
    print(f"  Matched: {matched}")
    print(f"  Duplicates: {summary['duplicates']}")
    print(f"  New: {new}")
    print()

    # Assertions
    success = True
    if matched != 1:
        print(f"❌ FAIL: Expected 1 match, got {matched}")
        success = False
    else:
        print(f"✓ PASS: Correctly identified 1 matched transaction")

    if new != 2:
        print(f"❌ FAIL: Expected 2 new, got {new}")
        success = False
    else:
        print(f"✓ PASS: Correctly identified 2 new transactions")

    return success


def test_all_duplicates_edge_case():
    """Test edge case where ALL rows are duplicates."""
    print("\nTEST: All duplicates edge case")
    print("-" * 70)

    csv_rows = [
        {'row_num': 1, 'date': '2026-06-01', 'description': 'Txn 1', 'deposit': 1000.0, 'withdrawal': 0.0},
        {'row_num': 2, 'date': '2026-06-02', 'description': 'Txn 2', 'deposit': 0.0, 'withdrawal': 500.0},
    ]

    gnucash_data = {
        'transactions': [
            {'date': '2026-06-01', 'amount': 1000.0, 'account': 'Assets:Bank'},
            {'date': '2026-06-02', 'amount': -500.0, 'account': 'Assets:Bank'},
        ]
    }

    report, summary = reconcile(csv_rows, gnucash_data)

    new_count = summary['new']
    total_dup = summary['matched'] + summary['duplicates']

    print(f"CSV rows: {len(csv_rows)}")
    print(f"Duplicates: {total_dup}, New: {new_count}")

    if new_count == 0 and total_dup == 2:
        print(f"✓ PASS: Correctly identified all rows as duplicates")
        return True
    else:
        print(f"❌ FAIL: Expected all duplicates, got {total_dup} dup, {new_count} new")
        return False


def test_no_duplicates():
    """Test case where NO rows are duplicates."""
    print("\nTEST: No duplicates case")
    print("-" * 70)

    csv_rows = [
        {'row_num': 1, 'date': '2026-06-05', 'description': 'New Txn 1', 'deposit': 2000.0, 'withdrawal': 0.0},
        {'row_num': 2, 'date': '2026-06-06', 'description': 'New Txn 2', 'deposit': 0.0, 'withdrawal': 750.0},
    ]

    gnucash_data = {
        'transactions': [
            {'date': '2026-06-01', 'amount': 1000.0, 'account': 'Assets:Bank'},
        ]
    }

    report, summary = reconcile(csv_rows, gnucash_data)

    new_count = summary['new']
    total_dup = summary['matched'] + summary['duplicates']

    print(f"CSV rows: {len(csv_rows)}")
    print(f"Duplicates: {total_dup}, New: {new_count}")

    if new_count == 2 and total_dup == 0:
        print(f"✓ PASS: Correctly identified all rows as new")
        return True
    else:
        print(f"❌ FAIL: Expected 0 dup, 2 new, got {total_dup} dup, {new_count} new")
        return False


def main():
    print("=" * 70)
    print("PHASE 4 LITE — Duplicate Detection Tests")
    print("=" * 70)
    print()

    results = []
    results.append(("Duplicate filtering", test_duplicate_filtering()))
    results.append(("All duplicates edge case", test_all_duplicates_edge_case()))
    results.append(("No duplicates case", test_no_duplicates()))

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, passed in results:
        status = "✓ PASS" if passed else "❌ FAIL"
        print(f"{status}: {name}")

    print()
    total = len(results)
    passed = sum(1 for _, p in results if p)
    print(f"Passed: {passed}/{total}")

    if passed == total:
        print("\n✓ All tests passed! Phase 4 Lite duplicate detection is working.")
        return 0
    else:
        print(f"\n❌ {total - passed} test(s) failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
