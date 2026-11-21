"""
tests/test_bnk01_bank_lists.py -- BNK-01 commit 2: the per-bank lists (rules
sections, extractor hints, key map, History labels) come from the bank
manifests, and the four banks that already had saved rules keep their saved
section names exactly.

All data is synthetic. The OLD behaviour is pinned here as literals (a copy of
the retired hard-coded tables) so a drift is caught against a fixed reference,
never against the code under test.
"""
from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for p in (str(SRC), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents import banks  # noqa: E402
from agents.skill_gnucash_xml_extractor import agent as ex  # noqa: E402

# ---- the retired hard-coded tables, kept verbatim as the reference ----------

OLD_PATTERNS = {
    "ICICI": ["ICICI", "icici"],
    "HDFC": ["HDFC", "hdfc"],
    "HSBC": ["HSBC", "hsbc"],
    "BoB": ["Bank of Baroda", "BoB", "bob"],
}
OLD_SECTIONS = ["ICICI", "HDFC", "HSBC", "BoB"]


def _old_match_bank(name: str):
    for bank, patterns in OLD_PATTERNS.items():
        for pattern in patterns:
            if pattern.lower() in name.lower():
                return bank
    return None


# ---- (1) the section list ---------------------------------------------------

def test_rules_keys_are_the_four_saved_names_then_sbm_and_kotak():
    assert banks.rules_keys() == OLD_SECTIONS + ["SBM", "Kotak"]


def test_every_discovered_bank_has_a_distinct_rules_key():
    keys = banks.rules_keys()
    assert len(keys) == len(set(keys)) == len(banks.discover())


def test_saved_names_are_never_the_lowercase_bank_key():
    """Saved rules are filed under 'BoB', not 'bob': a key taken from
    bank_key would orphan them."""
    for k in OLD_SECTIONS:
        assert k in banks.rules_keys()
    assert "bob" not in banks.rules_keys() and "icici" not in banks.rules_keys()


# ---- (2) extractor hints: old-vs-new battery --------------------------------

BATTERY = [
    "Assets:Current Assets:ICICI Bank - Savings",
    "Assets:HDFC Salary",
    "Assets:hsbc joint",
    "Assets:Bank of Baroda",
    "Assets:BoB Savings",
    "Assets:Bob's wallet",           # legacy substring: 'bob' inside the name
    "Assets:Fixed Deposits:ICICI Bank - FD",
    "Assets:Cash in hand",
    "Assets:Barclays",
    "Assets:HSBC and ICICI transfer",  # two banks: precedence must be unchanged
    "Assets:HDFC ICICI",
    "Liabilities:HSBC Credit Card",
    "Assets:Kobobank",
]


@pytest.mark.parametrize("name", BATTERY)
def test_the_four_legacy_banks_match_exactly_as_before(name):
    old = _old_match_bank(name)
    new = ex._match_bank(name)
    if old is not None:
        assert new == old, f"{name!r}: was {old}, now {new}"


@pytest.mark.parametrize("name", [n for n in BATTERY if _old_match_bank(n) is None])
def test_a_name_no_legacy_bank_matched_only_ever_gains_sbm_or_kotak(name):
    assert ex._match_bank(name) in (None, "SBM", "Kotak")


@pytest.mark.parametrize("name", ["Assets:SBM Savings", "Assets:Bank:sbm", "SBM-Current"])
def test_sbm_account_names_file_under_sbm(name):
    assert ex._match_bank(name) == "SBM"


@pytest.mark.parametrize("name", ["Assets:Kotak Savings", "Assets:kotak", "Kotak-Current"])
def test_kotak_account_names_file_under_kotak(name):
    assert ex._match_bank(name) == "Kotak"


@pytest.mark.parametrize("name", [
    "Assets:Subsbmerged",          # 'sbm' inside a word
    "Assets:Absbmx Holdings",
    "Assets:Resbm2",
    "Assets:Kotakish Mart",        # 'kotak' as the start of a longer word
    "Assets:Mykotak",
])
def test_sbm_and_kotak_do_not_false_match_inside_a_longer_word(name):
    assert ex._match_bank(name) is None


def test_legacy_banks_keep_the_substring_match_so_saved_history_does_not_move():
    """Negative: ICICI inside a longer word still matches exactly as it did
    (the old behaviour), because switching it to whole-word would move saved
    history for the books that already rely on it."""
    assert _old_match_bank("Assets:XICICIX") == "ICICI"
    assert ex._match_bank("Assets:XICICIX") == "ICICI"


def test_precedence_follows_the_old_order_when_two_banks_are_named():
    assert ex._match_bank("Assets:SBM to ICICI") == "ICICI"
    assert ex._match_bank("Assets:Kotak to HSBC") == "HSBC"
    assert ex._match_bank("Assets:SBM to Kotak") == "SBM"


def test_pattern_sections_are_the_rules_keys():
    assert list(ex.BANK_PATTERNS) == banks.rules_keys()


# ---- (3) generator sections --------------------------------------------------

def test_generate_rules_has_a_section_for_every_bank_and_global():
    from agents.skill_gnucash_mapping_generator.agent import generate_rules
    rules = generate_rules({"mappings": {}})
    assert set(rules) == {"_global"} | set(banks.rules_keys())
    assert all(rules[k] == [] for k in rules)


def test_generate_rules_keeps_the_four_old_sections_exactly():
    from agents.skill_gnucash_mapping_generator.agent import generate_rules
    rules = generate_rules({"mappings": {}})
    for k in OLD_SECTIONS:
        assert k in rules
    assert not any(k.lower() == "bob" and k != "BoB" for k in rules)


def _rule(bank, pat):
    return {"bank": bank, "patterns": [pat], "account": "Expenses:Misc",
            "confidence": "high", "reason": "r", "frequency": 5}


def test_generate_yaml_writes_sbm_and_kotak_sections_and_keeps_old_ones():
    from agents.skill_gnucash_mapping_generator.agent import generate_yaml
    text = generate_yaml([_rule(k, f"P{k}") for k in banks.rules_keys()])
    loaded = yaml.safe_load(text)
    assert set(banks.rules_keys()) <= set(loaded)
    # file order: the four saved sections first, as before
    heads = [ln[:-1] for ln in text.splitlines() if ln and not ln.startswith((" ", "#", "-")) and ln.endswith(":")]
    assert heads == banks.rules_keys()


def test_generate_yaml_drops_a_rule_for_an_unknown_section_as_before():
    from agents.skill_gnucash_mapping_generator.agent import generate_yaml
    text = generate_yaml([_rule("NotABank", "PX"), _rule("HDFC", "PH")])
    loaded = yaml.safe_load(text)
    assert "NotABank" not in loaded and "HDFC" in loaded


# ---- (4) mapper: key map and the per-bank rule filter ------------------------

def test_key_map_is_display_name_to_rules_key():
    from agents.skill_gnucash_account_mapper.agent import _BANK_KEY_MAP
    assert _BANK_KEY_MAP == {b.display_name: b.rules_key for b in banks.discover()}
    assert _BANK_KEY_MAP["Bank of Baroda"] == "BoB"


def _map(tmp_path, rules, bank_key, descs):
    from agents.skill_gnucash_account_mapper import agent
    canon = tmp_path / "c.csv"
    with open(canon, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Date", "Description", "Withdrawal", "Deposit"])
        w.writeheader()
        for i, d in enumerate(descs, 1):
            w.writerow({"Date": f"0{i}-04-2025", "Description": d, "Withdrawal": "10.00", "Deposit": ""})
    ry = tmp_path / "r.yaml"
    ry.write_text(yaml.dump(rules), encoding="utf-8")
    out, rep = tmp_path / "m.csv", tmp_path / "m.txt"
    agent.map_accounts(str(canon), str(ry), str(out), str(rep), bank_key=bank_key,
                       reference_date="2025-04-30")
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


RULES = {
    "SBM": [{"patterns": ["ALPHA"], "account": "Expenses:Sbm", "confidence": "high",
             "reason": "x", "frequency": 9}],
    "Kotak": [{"patterns": ["ALPHA"], "account": "Expenses:Kotak", "confidence": "high",
               "reason": "x", "frequency": 9}],
    "HDFC": [{"patterns": ["ALPHA"], "account": "Expenses:Hdfc", "confidence": "high",
              "reason": "x", "frequency": 9}],
}


@pytest.mark.parametrize("key,acct", [("SBM", "Expenses:Sbm"), ("Kotak", "Expenses:Kotak"),
                                      ("HDFC", "Expenses:Hdfc")])
def test_a_bank_key_applies_only_that_banks_rules(tmp_path, key, acct):
    rows = _map(tmp_path, RULES, key, ["ALPHA PAYMENT"])
    assert rows[0]["Account"].endswith(acct)


@pytest.mark.parametrize("key", ["SBM", "Kotak", "HDFC"])
def test_another_banks_rules_never_fire_for_this_bank(tmp_path, key):
    only_other = {k: v for k, v in RULES.items() if k != key}
    rows = _map(tmp_path, only_other, key, ["ALPHA PAYMENT"])
    assert not rows[0]["Account"] or rows[0]["Confidence"] == "none"


# ---- (5) History labels ------------------------------------------------------

def _labels_for(names):
    from ui.tabs.history import scan_outputs
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        out = Path(d)
        for n in names:
            (out / n).write_text("x")
        with patch("ui.tabs.history._config.output_dir", return_value=out):
            return {e.filename: e.skill_label for e in scan_outputs()}


def test_old_history_files_still_resolve_by_their_unchanged_suffix():
    got = _labels_for(["2026-01-02-030405-stmt-BoB.csv", "2026-01-02-030406-stmt-HSBC.xlsx",
                       "2026-01-02-030407-x-summary.md", "2026-01-02-030408-CC-Sort"])
    assert got["2026-01-02-030405-stmt-BoB.csv"] == "Bank of Baroda"
    assert got["2026-01-02-030406-stmt-HSBC.xlsx"] == "HSBC"          # was "HSBC Cleanup"
    assert got["2026-01-02-030407-x-summary.md"] == "Summarizer"
    assert got["2026-01-02-030408-CC-Sort"] == "CC Sort"


def test_no_bank_history_label_is_the_old_cleanup_wording():
    got = _labels_for([f"2026-01-02-03040{i}-s-{b.output_suffix}.csv"
                       for i, b in enumerate(banks.discover(), 1) if b.output_suffix])
    assert "HSBC Cleanup" not in got.values()
    assert set(got.values()) <= {b.display_name for b in banks.discover()}


def test_sbm_and_kotak_outputs_now_carry_their_bank_label_not_unknown():
    got = _labels_for(["2026-01-02-030405-s-SBM_canonical.csv", "2026-01-02-030406-s-Kotak.csv"])
    assert sorted(got.values()) == ["Kotak", "SBM"]


def test_an_unrelated_suffix_is_still_unknown():
    got = _labels_for(["2026-01-02-030405-s-NOTABANK.csv"])
    assert list(got.values()) == ["Unknown"]
