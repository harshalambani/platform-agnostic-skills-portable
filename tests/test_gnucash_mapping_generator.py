"""
tests/test_gnucash_mapping_generator.py — regression guards for ledger item
MAP-07 (root cause of the live low match rate).

Before this fix, generate_rules() in
src/agents/skill_gnucash_mapping_generator/agent.py only attempted UPI/NEFT/
merchant-id generalisation when confidence_score() > 0.7. confidence_score is
frequency * recency_weight, and recency_weight is 1.0 (<=365d) / 0.5 (<=730d)
/ 0.2 (older) -- so a transaction seen once, more than two years ago, scores
0.2, never clears the gate, and fell into `if not patterns:
patterns.append(description)`, persisting the ENTIRE RAW NARRATION as the
pattern. Bank narrations carry unique reference numbers, so such a pattern
can only ever match a byte-identical string -- it never fires again. Measured
on the live rules file this produced 1,408 of 1,564 ICICI rules as dead raw
narrations.

Covers:
  - generalisation is attempted regardless of confidence (the once-seen,
    3-year-old UPI transaction case that was completely broken);
  - a raw full narration is never persisted as a pattern -- if
    generalisation yields nothing, the rule is dropped;
  - every generated pattern actually compiles as a regex;
  - no generated pattern exceeds a sane length bound;
  - no generated pattern contains a reference-number-shaped long digit run.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_mapping_generator import agent as mapgen  # noqa: E402


def _mapping(description: str, account: str, frequency: int, last_date: str) -> dict:
    return {
        "description": description,
        "account": account,
        "frequency": frequency,
        "last_date": last_date,
    }


def _extractor_json(bank: str, mappings: list[dict]) -> dict:
    return {"mappings": {bank: mappings}}


# ---------------------------------------------------------------------------
# The exact broken case: once-seen, >2-year-old UPI transaction
# ---------------------------------------------------------------------------

def test_once_seen_old_upi_transaction_still_gets_generalised_pattern():
    """The case that was completely broken: frequency=1, last_date 3 years
    ago -> recency_weight 0.2, conf = 1 * 0.2 = 0.2, well under the old 0.7
    gate. It must still produce a generalised UPI pattern, not a raw-
    narration rule."""
    old_data = _mapping(
        "UPI/MERCHANT_XYZ/user123@ybl/Payment",
        "Expense:Shopping",
        frequency=1,
        last_date="2022-01-15",
    )
    extractor_json = _extractor_json("ICICI", [old_data])

    rules = mapgen.generate_rules(extractor_json, min_freq=1)
    icici_rules = rules["ICICI"]
    assert len(icici_rules) == 1
    rule = icici_rules[0]

    # Must be a generalised UPI pattern, never the raw narration.
    assert rule["patterns"] != ["UPI/MERCHANT_XYZ/user123@ybl/Payment"]
    assert any(p.startswith("UPI/MERCHANT_XYZ/") for p in rule["patterns"])
    # Low confidence is fine -- it reflects the WEIGHT, not whether a
    # pattern exists at all.
    assert rule["confidence"] == "low"


# ---------------------------------------------------------------------------
# Never persist a raw narration as a pattern
# ---------------------------------------------------------------------------

def test_narration_with_no_extractable_pattern_is_dropped_not_persisted_raw():
    """A narration with no UPI/NEFT/merchant-id shape (no generalisable
    structure at all) must be DROPPED, never fall back to persisting the
    full raw narration -- that produces a rule that can only ever match
    itself."""
    unmatchable = _mapping(
        "CHEQUE DEPOSIT REF NO 998877665544332211 BRANCH TRANSFER",
        "Asset:Bank",
        frequency=5,
        last_date="2024-01-01",
    )
    extractor_json = _extractor_json("ICICI", [unmatchable])

    rules = mapgen.generate_rules(extractor_json, min_freq=1)
    # extract_merchant_id would match a 6-9 digit run, but this narration's
    # digit run is >9 digits (a reference number), so no candidate pattern
    # survives -- the rule must be dropped entirely.
    assert rules["ICICI"] == []


def test_no_generated_rule_ever_has_the_raw_description_as_its_only_pattern():
    """Negative regression across a realistic mixed batch: scan every
    generated rule and assert none of them equals the source description
    verbatim as its sole pattern (the exact bug pattern)."""
    mappings = [
        _mapping("UPI/COFFEE SHOP/abc@okhdfcbank/txn", "Expense:Food", 1, "2021-06-01"),
        _mapping("NEFT-ACME CORP-salary credit", "Income:Salary", 2, "2020-03-10"),
        _mapping("POS 123456 GROCERY STORE PURCHASE", "Expense:Groceries", 1, "2023-05-05"),
        _mapping("RANDOM ONE OFF TRANSACTION WITH NO STRUCTURE AT ALL WHATSOEVER TODAY", "Expense:Misc", 1, "2019-01-01"),
    ]
    extractor_json = _extractor_json("HDFC", mappings)
    rules = mapgen.generate_rules(extractor_json, min_freq=1)

    for rule in rules["HDFC"]:
        for pattern in rule["patterns"]:
            # A raw narration is long, has spaces and no regex metachars in
            # the generated form -- but the simplest, decisive check is:
            # the pattern must never equal the original mapping's own
            # description in the fixture set above.
            assert pattern not in [m["description"] for m in mappings], (
                f"rule pattern {pattern!r} is a raw narration, not a "
                "generalised pattern"
            )


# ---------------------------------------------------------------------------
# Every generated pattern must compile as a regex
# ---------------------------------------------------------------------------

def test_all_generated_patterns_compile_as_regex():
    mappings = [
        _mapping("UPI/SHOP A/user@ybl/pay", "Expense:A", 1, "2021-01-01"),
        _mapping("NEFT-SOME COMPANY LTD-invoice", "Expense:B", 3, "2024-06-01"),
        _mapping("POS 654321 fuel station", "Expense:Fuel", 2, "2023-01-01"),
    ]
    extractor_json = _extractor_json("BoB", mappings)
    rules = mapgen.generate_rules(extractor_json, min_freq=1)

    all_patterns = [p for rule in rules["BoB"] for p in rule["patterns"]]
    assert all_patterns, "fixture should have produced at least one rule"
    for pattern in all_patterns:
        re.compile(pattern)  # raises re.error if not a valid pattern


# ---------------------------------------------------------------------------
# Length bound and reference-number-shaped digit run guards
# ---------------------------------------------------------------------------

def test_no_generated_pattern_exceeds_sane_length_bound():
    # End-to-end: realistic-length descriptions never produce an overlong
    # pattern in the first place.
    mappings = [
        _mapping("UPI/" + "X" * 40 + "/verylongvpaaddress@somebank/paymentref", "Expense:Long", 1, "2021-01-01"),
        _mapping("NEFT-" + "COMPANY WITH A VERY LONG REGISTERED NAME INDEED " * 2 + "-payment", "Expense:B", 2, "2024-01-01"),
    ]
    extractor_json = _extractor_json("HSBC", mappings)
    rules = mapgen.generate_rules(extractor_json, min_freq=1)

    for rule in rules["HSBC"]:
        for pattern in rule["patterns"]:
            assert len(pattern) <= mapgen.MAX_PATTERN_LEN

    # Direct guard on the filter itself: an artificially oversized candidate
    # must be rejected outright, proving the length cap is actually
    # enforced and not just untested-but-coincidentally-satisfied above.
    oversized = "UPI/" + "X" * 100 + "/.*"
    assert len(oversized) > mapgen.MAX_PATTERN_LEN
    assert mapgen._is_safe_pattern(oversized) is False


def test_no_generated_pattern_contains_reference_number_shaped_digit_run():
    """No generated pattern may embed a 10+ digit run -- that shape is a
    bank UTR/reference number, unique per transaction, and any pattern
    containing one can only ever match that single transaction again."""
    mappings = [
        # A narration with a bank reference number embedded right next to
        # otherwise-generalisable UPI structure.
        _mapping("UPI/MERCHANT/user@ybl/UTR9988776655443322", "Expense:X", 1, "2021-01-01"),
        _mapping("POS 12345678901 SUPERMARKET", "Expense:Y", 1, "2022-06-01"),
    ]
    extractor_json = _extractor_json("ICICI", mappings)
    rules = mapgen.generate_rules(extractor_json, min_freq=1)

    long_digit_run = re.compile(r"\d{10,}")
    for rule in rules["ICICI"]:
        for pattern in rule["patterns"]:
            assert not long_digit_run.search(pattern), (
                f"pattern {pattern!r} contains a reference-number-shaped digit run"
            )
