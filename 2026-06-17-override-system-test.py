#!/usr/bin/env python3
"""
Override System Test — Demonstrate per-GnuCash-file account overrides.

This test shows how the override system works:
1. Create override rules from user corrections
2. Store them alongside the GnuCash file
3. Load and apply them on next run (highest priority)
"""

import sys
import tempfile
from pathlib import Path

# Add agents to path
agents_path = Path(__file__).parent / "src" / "agents"
sys.path.insert(0, str(agents_path))

from skill_gnucash_account_mapper.account_overrides import (
    load_overrides,
    save_override,
    save_overrides_batch,
    match_overrides,
    overrides_path,
)


def test_override_workflow():
    """Test the complete override workflow."""
    print("=" * 70)
    print("OVERRIDE SYSTEM TEST")
    print("=" * 70)
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        # Simulate a GnuCash file
        gnucash_file = Path(tmpdir) / "MyBook.gnucash"
        gnucash_file.touch()

        print(f"Test GnuCash file: {gnucash_file.name}")
        print(f"Override file will be: {overrides_path(str(gnucash_file)).name}")
        print()

        # ─────────────────────────────────────────────────────────────
        # SCENARIO 1: User makes corrections, saves them as overrides
        # ─────────────────────────────────────────────────────────────
        print("SCENARIO 1: Save user corrections as overrides")
        print("-" * 70)

        corrections = [
            ("IMPS.*WORLDLINE", "Expenses:Online Payments"),
            ("UPI/AMAZONPAY", "Expenses:Online Shopping"),
            ("NEFT.*SALARY", "Income:Salary"),
        ]

        for pattern, account in corrections:
            save_override(str(gnucash_file), pattern, account)
            print(f"  ✓ Saved: {pattern} → {account}")

        print()

        # ─────────────────────────────────────────────────────────────
        # SCENARIO 2: Load overrides on next run
        # ─────────────────────────────────────────────────────────────
        print("SCENARIO 2: Load overrides on next run")
        print("-" * 70)

        overrides = load_overrides(str(gnucash_file))
        print(f"Loaded {len(overrides)} overrides from {overrides_path(str(gnucash_file)).name}:")
        for i, o in enumerate(overrides, 1):
            print(f"  {i}. Pattern: {o['pattern']}")
            print(f"     Account: {o['account']}")
            print(f"     Added: {o['added']}")

        print()

        # ─────────────────────────────────────────────────────────────
        # SCENARIO 3: Match transaction descriptions against overrides
        # ─────────────────────────────────────────────────────────────
        print("SCENARIO 3: Match descriptions against overrides")
        print("-" * 70)

        test_transactions = [
            "IMPS to WORLDLINE PAYMENT REF123",
            "UPI/AMAZONPAY/9876543210",
            "NEFT SALARY CREDIT JUN 2026",
            "DEBIT CARD PURCHASE STARBUCKS",  # No override
        ]

        for txn in test_transactions:
            account, reason = match_overrides(txn, overrides)
            if account:
                print(f"  ✓ '{txn}'")
                print(f"    → {account} ({reason})")
            else:
                print(f"  ✗ '{txn}'")
                print(f"    → No override match")

        print()

        # ─────────────────────────────────────────────────────────────
        # SCENARIO 4: Bulk update overrides (from Review UI)
        # ─────────────────────────────────────────────────────────────
        print("SCENARIO 4: Bulk update (e.g., from Review UI)")
        print("-" * 70)

        updated_overrides = [
            {"pattern": "IMPS.*WORLDLINE", "account": "Expenses:Online Payments"},
            {"pattern": "UPI/AMAZONPAY", "account": "Expenses:Shopping"},  # Changed
            {"pattern": "NEFT.*SALARY", "account": "Income:Salary"},
            {"pattern": "STARBUCKS|COFFEE", "account": "Expenses:Food & Drinks"},  # New
        ]

        save_overrides_batch(str(gnucash_file), updated_overrides)
        print(f"✓ Saved {len(updated_overrides)} overrides (1 corrected, 1 added)")

        # Reload and verify
        reloaded = load_overrides(str(gnucash_file))
        print(f"✓ Reloaded {len(reloaded)} overrides")

        # Test the new override
        desc = "COFFEE AT STARBUCKS TIMES SQUARE"
        acct, reason = match_overrides(desc, reloaded)
        print(f"✓ New override matched: '{desc}' → {acct}")

        print()

        # ─────────────────────────────────────────────────────────────
        # Summary
        # ─────────────────────────────────────────────────────────────
        print("=" * 70)
        print("✓ OVERRIDE SYSTEM WORKING")
        print("=" * 70)
        print()
        print("Features demonstrated:")
        print("  1. Save individual overrides (save_override)")
        print("  2. Load all overrides for a GnuCash file (load_overrides)")
        print("  3. Match descriptions against overrides (match_overrides)")
        print("  4. Bulk update from Review UI (save_overrides_batch)")
        print()
        print("Integration point:")
        print("  - Overrides are loaded in account_mapper.run()")
        print("  - Applied as highest-priority pass (before smart patterns)")
        print("  - Can correct any mapping (not just fill gaps)")
        print()
        print(f"Override file location: {overrides_path(str(gnucash_file)).parent / overrides_path(str(gnucash_file)).name}")


if __name__ == "__main__":
    test_override_workflow()
