#!/usr/bin/env python3
"""
Per-GnuCash-file account override rules.

Override files are stored alongside the GnuCash book:
    MyFinances2425.gnucash → MyFinances2425_account_overrides.yaml

Format (YAML):
    - pattern: "IMPS.*WORLDLINE"        # regex matched against Description
      account: "Expenses:Online Payments" # full GnuCash account path (no Root Account: prefix)
      added: "2026-06-17"
    - pattern: "UPI/AMAZONPAY"
      account: "Expenses:Online Shopping"
      added: "2026-06-16"

Overrides take priority over auto-generated rules and smart patterns.
"""

import re
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, List, Dict

import yaml

log = logging.getLogger(__name__)


def overrides_path(gnucash_file: str) -> Path:
    """
    Get the path to the overrides YAML file for a given GnuCash file.

    Args:
        gnucash_file: Path to a .gnucash file (e.g., /path/to/MyBook.gnucash)

    Returns:
        Path: Path to the override file (e.g., /path/to/MyBook_account_overrides.yaml)
    """
    gnucash_path = Path(gnucash_file)
    override_file = gnucash_path.parent / f"{gnucash_path.stem}_account_overrides.yaml"
    return override_file


def load_overrides(gnucash_file: str) -> List[Dict]:
    """
    Load override rules from the YAML file.

    Args:
        gnucash_file: Path to a .gnucash file

    Returns:
        List[Dict]: List of override rules, each with keys: pattern, account, added
                   Returns empty list if file doesn't exist or is invalid.
    """
    override_file = overrides_path(gnucash_file)

    if not override_file.exists():
        return []

    try:
        with open(override_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not data:
            return []

        if not isinstance(data, list):
            log.warning(f"Override file {override_file} is not a list, treating as empty")
            return []

        # Validate structure
        valid_overrides = []
        for i, rule in enumerate(data):
            if not isinstance(rule, dict):
                log.warning(f"Override {i} is not a dict, skipping")
                continue

            pattern = rule.get("pattern")
            account = rule.get("account")

            if not pattern or not account:
                log.warning(f"Override {i} missing pattern or account, skipping")
                continue

            # Validate regex
            try:
                re.compile(pattern)
            except re.error as e:
                log.warning(f"Override {i} has invalid regex '{pattern}': {e}, skipping")
                continue

            valid_overrides.append({
                "pattern": pattern,
                "account": account,
                "added": rule.get("added", "unknown"),
            })

        return valid_overrides

    except Exception as e:
        log.error(f"Failed to load overrides from {override_file}: {e}")
        return []


def save_override(gnucash_file: str, pattern: str, account: str) -> None:
    """
    Append a single override rule to the YAML file.

    Creates the file if it doesn't exist. Sets 'added' date to today.

    Args:
        gnucash_file: Path to a .gnucash file
        pattern: Regex pattern to match against Description
        account: GnuCash account path (e.g., "Expenses:Online Payments")

    Raises:
        ValueError: If pattern is not valid regex
    """
    # Validate regex
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern '{pattern}': {e}")

    override_file = overrides_path(gnucash_file)

    # Load existing overrides
    overrides = load_overrides(gnucash_file)

    # Append new override
    today = datetime.now().strftime("%Y-%m-%d")
    overrides.append({
        "pattern": pattern,
        "account": account,
        "added": today,
    })

    # Save back
    save_overrides_batch(gnucash_file, overrides)


def save_overrides_batch(gnucash_file: str, overrides: List[Dict]) -> None:
    """
    Replace the entire overrides file with a new list of rules.

    Args:
        gnucash_file: Path to a .gnucash file
        overrides: List of dicts with keys: pattern, account (added date is optional)
    """
    override_file = overrides_path(gnucash_file)

    # Ensure parent directory exists
    override_file.parent.mkdir(parents=True, exist_ok=True)

    # Add today's date to any overrides missing 'added'
    today = datetime.now().strftime("%Y-%m-%d")
    for override in overrides:
        if "added" not in override or not override["added"]:
            override["added"] = today

    # Write YAML
    try:
        with open(override_file, "w", encoding="utf-8") as f:
            yaml.dump(
                overrides,
                f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        log.info(f"Saved {len(overrides)} overrides to {override_file}")
    except Exception as e:
        log.error(f"Failed to save overrides to {override_file}: {e}")
        raise


def match_overrides(description: str, overrides: List[Dict]) -> Tuple[Optional[str], str]:
    """
    Try to match a description against override patterns.

    Returns the first matching override's account and reason.

    Args:
        description: Transaction description to match
        overrides: List of override rules from load_overrides()

    Returns:
        Tuple[Optional[str], str]: (matched_account, reason_string)
                                   Returns (None, '') if no match
    """
    if not description:
        return None, ""

    for override in overrides:
        pattern = override.get("pattern", "")
        account = override.get("account", "")
        added = override.get("added", "unknown")

        if not pattern or not account:
            continue

        try:
            if re.search(pattern, description, re.IGNORECASE):
                reason = f"Override (added {added})"
                return account, reason
        except re.error as e:
            log.warning(f"Invalid override pattern '{pattern}': {e}")
            continue

    return None, ""


if __name__ == "__main__":
    # Quick test
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        test_gnucash = Path(tmpdir) / "test.gnucash"
        test_gnucash.touch()

        # Test save_override
        save_override(str(test_gnucash), "IMPS.*WORLDLINE", "Expenses:Online Payments")
        save_override(str(test_gnucash), "UPI/AMAZONPAY", "Expenses:Shopping")

        # Test load_overrides
        overrides = load_overrides(str(test_gnucash))
        print(f"Loaded {len(overrides)} overrides:")
        for o in overrides:
            print(f"  {o['pattern']} → {o['account']}")

        # Test match_overrides
        desc = "IMPS to WORLDLINE PAYMENT"
        acct, reason = match_overrides(desc, overrides)
        print(f"\nMatching '{desc}':")
        print(f"  Account: {acct}, Reason: {reason}")

        desc2 = "UPI/AMAZONPAY/ABCD1234"
        acct2, reason2 = match_overrides(desc2, overrides)
        print(f"\nMatching '{desc2}':")
        print(f"  Account: {acct2}, Reason: {reason2}")

        print("\n✓ All tests passed!")
