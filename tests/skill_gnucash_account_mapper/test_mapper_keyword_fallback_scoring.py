"""
tests/skill_gnucash_account_mapper/test_mapper_keyword_fallback_scoring.py --
regression guard for MAP-08: the mapper's keyword fallback (inside
_historical_prefix_match) and two smart_pattern_match rules picked the
FIRST tree-order / list-order candidate that shared a bare substring or a
single >=5-char token with the narration -- no scoring, no tiebreak, no
document-frequency notion. A family surname present in several account
leaves, or several accounts sharing a bare "TDS"/"Tax" substring, would
then decide the account by list-order luck alone, and the keyword-fallback
result was stamped confidence='smart' -- indistinguishable from the
high-precision rules, so a bad guess was never reconsidered by the LLM
pass.

Background (src/agents/skill_gnucash_account_mapper/agent.py):

  Approach 8.1 -- _historical_prefix_match's keyword fallback now scores
  EVERY deduped candidate account. A token's document frequency (how many
  distinct candidate leaves contain it) determines whether it can
  discriminate between candidates: a token in more than one leaf never
  counts toward a score. Candidates are ranked by (summed discriminating
  token length, historical frequency); a tie on that ranking after the
  frequency tiebreak returns None rather than guess. The whole thing sorts
  its own candidate list, so it is deterministic regardless of the
  caller's list order. A match found this way carries confidence='weak'.

  Approach 8.3 -- run()'s smart pass now reads match['confidence']
  (default 'smart') instead of hard-coding 'smart' for every match. 'weak'
  rows are NOT counted into smart_mapped_count / confidence_counts['smart'],
  are folded back into still_unmatched for the LLM pass to reconsider (an
  LLM answer replaces the weak guess; no usable LLM answer keeps 'weak'),
  and are excluded from the LLM's example_mappings (an unscored guess must
  never train the prompt). 'weak' also joins _MANUAL_REVIEW_CONFIDENCES and
  _CONFIDENCE_LABELS (as "Weak keyword match").

  3.3.e -- smart_pattern_match rule 5 (TDS on dividend) and rule 14 (tax
  payment) no longer return the first tree-order account containing a bare
  "TDS" / "Tax" substring. Each now prefers its most specific keyword,
  and only falls back to the bare substring when EXACTLY ONE account
  qualifies (sorted, so the choice cannot depend on account_tree order);
  otherwise the rule returns None and lets later rules / the LLM decide.

All account names, surnames and narrations below are synthetic/invented.
The LLM is monkeypatched in every test that reaches it -- no network.
"""
from __future__ import annotations

import csv
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402

# run()'s sibling-agent imports resolve via `sys.path.insert(0, agents_root)`
# + bare `from X import Y`, cached in sys.modules under undotted keys
# distinct from this file's own `agents.<pkg>.<mod>` dotted imports.
# Importing under the same undotted keys here is what makes monkeypatching
# them (in the Part 3 test) visible to run().
AGENTS_ROOT = SRC / "agents"
if str(AGENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENTS_ROOT))


# ---------------------------------------------------------------------------
# Part 1: _historical_prefix_match keyword-fallback scoring
# ---------------------------------------------------------------------------

def _hist(desc: str, account: str, frequency: int = 1) -> dict:
    return {"description": desc, "account": account, "frequency": frequency}


# A synthetic family surname shared across 5 account leaves. Each historical
# description is long/distinct enough from the narration below that the
# prefix-matcher (the first half of _historical_prefix_match) never fires,
# so every case here genuinely exercises the keyword fallback.
_SURNAME_HISTORICAL = [
    _hist("PAYMENT ZORBLAX AAA REF001", "Expenses:Family:ZORBLAX Alpha"),
    _hist("PAYMENT ZORBLAX BBB REF002", "Expenses:Family:ZORBLAX Beta"),
    _hist("PAYMENT ZORBLAX CCC REF003", "Expenses:Family:ZORBLAX Gamma"),
    _hist("PAYMENT ZORBLAX DDD REF004", "Expenses:Family:ZORBLAX Delta"),
    _hist("PAYMENT ZORBLAX EEE REF005", "Expenses:Family:ZORBLAX Epsilon"),
]
_SURNAME_NARRATION = "XYZQR ZORBLAX TRANSFER"


def test_shared_surname_across_five_leaves_yields_no_match():
    """The wrong behaviour: picking ANY of the 5 ZORBLAX accounts because
    the surname token is shared. ZORBLAX has document frequency 5 (it
    appears in all 5 leaves), so it can never count as a discriminating
    token -- the fix must return None, not Alpha/Beta/Gamma/Delta/Epsilon."""
    result = agent._historical_prefix_match(_SURNAME_NARRATION, _SURNAME_HISTORICAL)
    assert result is None


def test_shared_surname_result_is_order_independent():
    """Same inputs in two different list orders must yield the identical
    result (here: None either way) -- the fix must not depend on
    dict/list iteration order or PYTHONHASHSEED."""
    shuffled = list(_SURNAME_HISTORICAL)
    random.Random(12345).shuffle(shuffled)
    assert shuffled != _SURNAME_HISTORICAL  # sanity: the shuffle actually changed order

    result_original = agent._historical_prefix_match(_SURNAME_NARRATION, _SURNAME_HISTORICAL)
    result_shuffled = agent._historical_prefix_match(_SURNAME_NARRATION, shuffled)
    assert result_original == result_shuffled == None  # noqa: E711


# Two candidates that score identically (same summed discriminating-token
# length, same historical frequency) -- a genuine tie with no tiebreak left.
_TIE_HISTORICAL = [
    _hist("SHORT A", "Expenses:Misc:QUENYAX Corp"),
    _hist("SHORT B", "Expenses:Misc:VALARIN Ltd"),
]
_TIE_NARRATION = "PAYMENT QUENYAX VALARIN REF999"


def test_tied_candidates_yield_no_match():
    result = agent._historical_prefix_match(_TIE_NARRATION, _TIE_HISTORICAL)
    assert result is None


def test_tied_candidates_result_is_order_independent():
    reversed_hist = list(reversed(_TIE_HISTORICAL))
    result_a = agent._historical_prefix_match(_TIE_NARRATION, _TIE_HISTORICAL)
    result_b = agent._historical_prefix_match(_TIE_NARRATION, reversed_hist)
    assert result_a == result_b is None


# A single, genuinely discriminating token ("WIDGETS") that appears in only
# one candidate leaf -- alongside an unrelated second candidate with no
# overlapping token at all, so this is a real (if small) field of
# candidates, not a trivial single-candidate case.
_DISCRIMINATING_HISTORICAL = [
    _hist("SOME OLD NARRATION ONE", "Expenses:Acme:Widgets", frequency=2),
    _hist("SOME OLD NARRATION TWO", "Expenses:Acme:Gadgets", frequency=9),
]
_DISCRIMINATING_NARRATION = "PYMT WIDGETS INVOICE REF777"


def test_unique_discriminating_token_matches_with_weak_confidence():
    result = agent._historical_prefix_match(_DISCRIMINATING_NARRATION, _DISCRIMINATING_HISTORICAL)
    assert result is not None
    assert result["account"] == "Expenses:Acme:Widgets"
    # The specific wrong behaviour this guards against: a keyword-fallback
    # match must NOT be stamped 'smart' -- that would make it
    # indistinguishable from a high-precision rule match and skip LLM
    # reconsideration entirely.
    assert result["confidence"] == "weak"
    assert result["confidence"] != "smart"


def test_prefix_match_branch_still_reports_smart_confidence():
    """The prefix-matcher (the OTHER half of _historical_prefix_match, not
    the keyword fallback under test above) must be unaffected: it stays
    'smart' via the default in run()'s `match.get('confidence', 'smart')`."""
    hist = [_hist("BAJAJ FINANCE LIMITED -808693", "Liabilities:Loans:Bajaj Finance", frequency=3)]
    result = agent._historical_prefix_match("BAJAJ FINANCE LIMITED -5150102", hist)
    assert result is not None
    assert result["account"] == "Liabilities:Loans:Bajaj Finance"
    assert "confidence" not in result  # prefix-match branch never sets it


# ---------------------------------------------------------------------------
# Part 2: smart_pattern_match rule 5 (TDS on dividend) and rule 14 (tax)
# ---------------------------------------------------------------------------

def test_rule5_multiple_bare_tds_accounts_yields_no_match():
    """Wrong behaviour guarded against: picking the first tree-order
    account merely because it contains the bare substring 'TDS', with two
    equally-plausible bare-TDS accounts and no 'TDS on Dividend' account
    at all."""
    account_tree = [
        "Expenses:Tax:TDS Receivable Bank A",
        "Expenses:Tax:TDS Receivable Bank B",
    ]
    result = agent.smart_pattern_match("TDS ON DIV FROM ACME LTD", account_tree)
    assert result is None


def test_rule5_multiple_bare_tds_accounts_order_independent():
    account_tree = [
        "Expenses:Tax:TDS Receivable Bank A",
        "Expenses:Tax:TDS Receivable Bank B",
    ]
    reversed_tree = list(reversed(account_tree))
    result_a = agent.smart_pattern_match("TDS ON DIV FROM ACME LTD", account_tree)
    result_b = agent.smart_pattern_match("TDS ON DIV FROM ACME LTD", reversed_tree)
    assert result_a == result_b is None


def test_rule5_single_bare_tds_account_matches_deterministically():
    account_tree = ["Expenses:Tax:TDS Receivable Only Account"]
    result = agent.smart_pattern_match("TDS ON DIV FROM ACME LTD", account_tree)
    assert result is not None
    assert result["account"] == "Expenses:Tax:TDS Receivable Only Account"


def test_rule5_prefers_specific_tds_on_dividend_account():
    account_tree = [
        "Expenses:Tax:TDS Receivable Bank A",     # bare TDS, decoy
        "Expenses:Tax:TDS Receivable Bank B",     # bare TDS, decoy
        "Income:Dividend:TDS on Dividend",        # the specific one -- must win
    ]
    result = agent.smart_pattern_match("TDS ON DIV FROM ACME LTD", account_tree)
    assert result is not None
    assert result["account"] == "Income:Dividend:TDS on Dividend"


def test_rule14_multiple_bare_tax_accounts_yields_no_match():
    """Wrong behaviour guarded against: picking the first tree-order
    account merely because it contains the bare substring 'Tax', with two
    equally-plausible bare-Tax accounts and no 'Income Tax'/'Advance Tax'
    account."""
    account_tree = [
        "Expenses:Statutory:Tax Payment Channel A",
        "Expenses:Statutory:Tax Payment Channel B",
    ]
    result = agent.smart_pattern_match("INCOME TAX CHALLAN PAID", account_tree)
    assert result is None


def test_rule14_multiple_bare_tax_accounts_order_independent():
    account_tree = [
        "Expenses:Statutory:Tax Payment Channel A",
        "Expenses:Statutory:Tax Payment Channel B",
    ]
    reversed_tree = list(reversed(account_tree))
    result_a = agent.smart_pattern_match("INCOME TAX CHALLAN PAID", account_tree)
    result_b = agent.smart_pattern_match("INCOME TAX CHALLAN PAID", reversed_tree)
    assert result_a == result_b is None


def test_rule14_prefers_advance_tax_over_income_tax_and_bare_tax():
    account_tree = [
        "Expenses:Statutory:Tax Payment Channel A",  # bare Tax, decoy
        "Expenses:Statutory:Income Tax Paid",          # more specific, still a decoy
        "Expenses:Statutory:Advance Tax Paid",         # most specific -- must win
    ]
    result = agent.smart_pattern_match("ADVANCE TAX Q2 CHALLAN PAID", account_tree)
    assert result is not None
    assert result["account"] == "Expenses:Statutory:Advance Tax Paid"


def test_rule14_income_tax_wins_over_bare_tax_when_no_advance_tax_wording():
    account_tree = [
        "Expenses:Statutory:Tax Payment Channel A",  # bare Tax, decoy
        "Expenses:Statutory:Income Tax Paid",          # specific -- must win (no "ADVANCE TAX" in narration)
    ]
    result = agent.smart_pattern_match("INCOME TAX CHALLAN PAID", account_tree)
    assert result is not None
    assert result["account"] == "Expenses:Statutory:Income Tax Paid"


def test_rule14_single_bare_tax_account_matches_deterministically():
    account_tree = ["Expenses:Statutory:Tax Payment Only Channel"]
    result = agent.smart_pattern_match("SELF ASSESSMENT TAX CHALLAN", account_tree)
    assert result is not None
    assert result["account"] == "Expenses:Statutory:Tax Payment Only Channel"


# --- MAP-08 follow-up: every tier requires EXACTLY ONE candidate, not just
# the bare-substring fallback tier. A book with several payers/assessment
# years can have several "TDS on Dividend" or several "Advance Tax"/"Income
# Tax" leaves; picking the alphabetically first one at a specific tier is
# the same arbitrary-pick bug MAP-08 removed, just moved up a tier. The
# LLM (always available, per the user) is the right fallback when a tier
# is ambiguous -- these rules must never guess, at any tier.

def test_rule5_multiple_tds_on_dividend_accounts_yields_no_match():
    """Two 'TDS on Dividend' leaves (e.g. one per payer) -- the specific
    tier itself is ambiguous. Must NOT pick either one (not the
    alphabetically first), and must NOT drop down to a bare 'TDS' tier."""
    account_tree = [
        "Income:Dividend:TDS on Dividend Acme",
        "Income:Dividend:TDS on Dividend Zenith",
    ]
    result = agent.smart_pattern_match("TDS ON DIV FROM ACME LTD", account_tree)
    assert result is None


def test_rule5_multiple_tds_on_dividend_accounts_order_independent():
    account_tree = [
        "Income:Dividend:TDS on Dividend Acme",
        "Income:Dividend:TDS on Dividend Zenith",
    ]
    reversed_tree = list(reversed(account_tree))
    result_a = agent.smart_pattern_match("TDS ON DIV FROM ACME LTD", account_tree)
    result_b = agent.smart_pattern_match("TDS ON DIV FROM ACME LTD", reversed_tree)
    assert result_a == result_b is None


def test_rule5_ambiguous_specific_tier_does_not_drop_to_bare_tier():
    """Two 'TDS on Dividend' leaves PLUS exactly one other bare 'TDS' leaf
    (which on its own would be an unambiguous match at the fallback tier).
    The specific tier's ambiguity must still win -- the rule falls through
    entirely rather than dropping down to the now-unambiguous bare tier."""
    account_tree = [
        "Income:Dividend:TDS on Dividend Acme",
        "Income:Dividend:TDS on Dividend Zenith",
        "Expenses:Tax:TDS Receivable Only Other Account",
    ]
    result = agent.smart_pattern_match("TDS ON DIV FROM ACME LTD", account_tree)
    assert result is None


def test_rule14_multiple_advance_tax_accounts_yields_no_match():
    """Two 'Advance Tax' leaves (e.g. one per assessment year) with an
    ADVANCE TAX narration -- must NOT pick either one, and must NOT drop
    down to 'Income Tax' or a bare 'Tax' tier."""
    account_tree = [
        "Expenses:Statutory:Advance Tax AY2025-26",
        "Expenses:Statutory:Advance Tax AY2026-27",
        "Expenses:Statutory:Income Tax Paid",
    ]
    result = agent.smart_pattern_match("ADVANCE TAX Q2 CHALLAN PAID", account_tree)
    assert result is None


def test_rule14_multiple_advance_tax_accounts_order_independent():
    account_tree = [
        "Expenses:Statutory:Advance Tax AY2025-26",
        "Expenses:Statutory:Advance Tax AY2026-27",
    ]
    reversed_tree = list(reversed(account_tree))
    result_a = agent.smart_pattern_match("ADVANCE TAX Q2 CHALLAN PAID", account_tree)
    result_b = agent.smart_pattern_match("ADVANCE TAX Q2 CHALLAN PAID", reversed_tree)
    assert result_a == result_b is None


def test_rule14_multiple_income_tax_accounts_yields_no_match():
    """Two 'Income Tax' leaves, no ADVANCE TAX wording in the narration --
    the Income Tax tier itself is ambiguous. Must NOT pick either one, and
    must NOT drop down to a bare 'Tax' tier even though only one bare-Tax
    decoy account exists."""
    account_tree = [
        "Expenses:Statutory:Income Tax Paid AY2025-26",
        "Expenses:Statutory:Income Tax Paid AY2026-27",
        "Expenses:Statutory:Tax Payment Channel A",
    ]
    result = agent.smart_pattern_match("INCOME TAX CHALLAN PAID", account_tree)
    assert result is None


def test_rule14_multiple_income_tax_accounts_order_independent():
    account_tree = [
        "Expenses:Statutory:Income Tax Paid AY2025-26",
        "Expenses:Statutory:Income Tax Paid AY2026-27",
    ]
    reversed_tree = list(reversed(account_tree))
    result_a = agent.smart_pattern_match("INCOME TAX CHALLAN PAID", account_tree)
    result_b = agent.smart_pattern_match("INCOME TAX CHALLAN PAID", reversed_tree)
    assert result_a == result_b is None


def test_rule14_ambiguous_advance_tax_tier_does_not_drop_to_income_tax_tier():
    """Two 'Advance Tax' leaves PLUS exactly one 'Income Tax' leaf (which on
    its own, or under a non-ADVANCE-TAX narration, would be an unambiguous
    match). With ADVANCE TAX wording, the ambiguous Advance Tax tier must
    still win -- the rule falls through entirely rather than dropping down
    to the now-unambiguous Income Tax tier."""
    account_tree = [
        "Expenses:Statutory:Advance Tax AY2025-26",
        "Expenses:Statutory:Advance Tax AY2026-27",
        "Expenses:Statutory:Income Tax Paid",
    ]
    result = agent.smart_pattern_match("ADVANCE TAX Q2 CHALLAN PAID", account_tree)
    assert result is None


def test_rule14_ambiguous_income_tax_tier_does_not_drop_to_bare_tax_tier():
    """Two 'Income Tax' leaves PLUS exactly one bare 'Tax' leaf (which on
    its own would be an unambiguous fallback match). No ADVANCE TAX wording
    in the narration, so Income Tax is the first applicable tier -- its
    ambiguity must still win over dropping to the bare tier."""
    account_tree = [
        "Expenses:Statutory:Income Tax Paid AY2025-26",
        "Expenses:Statutory:Income Tax Paid AY2026-27",
        "Expenses:Statutory:Tax Payment Only Channel",
    ]
    result = agent.smart_pattern_match("INCOME TAX CHALLAN PAID", account_tree)
    assert result is None


# ---------------------------------------------------------------------------
# Part 3: run() end-to-end -- weak rows are reconsidered by the LLM pass,
# excluded from LLM examples, and correctly reflected in confidence
# counts / the confidence report.
# ---------------------------------------------------------------------------

_CANONICAL_HEADERS = ["Date", "Description", "Withdrawal", "Deposit"]

_WEAK_FLOW_CANONICAL_ROWS = [
    # Row 1: keyword-fallback finds a weak match; the LLM finds nothing
    # usable -- the weak guess must be KEPT (not silently dropped to
    # suspense, not silently trusted as 'smart').
    {"Date": "01-04-2025", "Description": "ROWA WEAK KEEP TXN", "Withdrawal": "10.00", "Deposit": ""},
    # Row 2: keyword-fallback finds a weak match; the LLM DOES find a
    # usable answer -- the LLM answer must REPLACE the weak guess.
    {"Date": "02-04-2025", "Description": "ROWB WEAK REPLACE TXN", "Withdrawal": "20.00", "Deposit": ""},
    # Row 3: no keyword-fallback match at all, and the LLM also finds
    # nothing -- must fall through to the suspense pass as always,
    # unaffected by the weak-confidence plumbing.
    {"Date": "03-04-2025", "Description": "ROWC NONE STAYS NONE TXN", "Withdrawal": "30.00", "Deposit": ""},
]


def _write_weak_flow_fixtures(tmp_path: Path):
    canonical_csv = tmp_path / "canonical_weak.csv"
    with open(canonical_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CANONICAL_HEADERS)
        writer.writeheader()
        writer.writerows(_WEAK_FLOW_CANONICAL_ROWS)
    return canonical_csv


def test_weak_rows_reconsidered_by_llm_replace_and_keep_with_correct_counts(tmp_path, monkeypatch):
    """run() end-to-end with every external dependency faked, engineered so
    the rules pass resolves nothing and the smart pass produces exactly two
    'weak' keyword-fallback matches (rows 1 and 2) plus one genuine 'none'
    (row 3). The LLM fallback is faked to replace row 2's weak guess, leave
    row 1's weak guess untouched, and leave row 3 unresolved (-> suspense).
    """
    import skill_gnucash_xml_extractor.agent as xml_agent_mod
    import skill_gnucash_mapping_generator.agent as mapgen_mod
    import skill_gnucash_account_mapper.persistent_rules as persistent_rules_mod

    def fake_parse_gnucash_file(path):
        return {"mappings": {"BankX": [
            {"account": "Expenses:WeakKeep"},
            {"account": "Expenses:WeakReplace"},
            {"account": "Expenses:Other"},
        ]}}

    def fake_generate_rules(extractor_output, min_freq=1):
        return {}  # rules pass matches nothing -- every row starts 'none'

    def fake_merge_auto_rules(gnucash_file, rules_by_bank, config_path=None):
        return rules_by_bank

    def fake_load_overrides(gnucash_file, config_path=None):
        return []

    def fake_migrate_legacy_overrides(gnucash_file, config_path=None):
        return 0

    def fake_rules_path(gnucash_file, config_path=None):
        return tmp_path / "fake_persistent_rules.yaml"

    monkeypatch.setattr(xml_agent_mod, "parse_gnucash_file", fake_parse_gnucash_file)
    monkeypatch.setattr(mapgen_mod, "generate_rules", fake_generate_rules)
    monkeypatch.setattr(persistent_rules_mod, "merge_auto_rules", fake_merge_auto_rules)
    monkeypatch.setattr(persistent_rules_mod, "load_overrides", fake_load_overrides)
    monkeypatch.setattr(persistent_rules_mod, "migrate_legacy_overrides", fake_migrate_legacy_overrides)
    monkeypatch.setattr(persistent_rules_mod, "rules_path", fake_rules_path)

    # Force every row through the keyword-fallback path deterministically:
    # smart_pattern_match never matches, and _historical_prefix_match
    # returns synthetic 'weak' matches for rows 1/2 and None for row 3.
    def fake_smart_pattern_match(description, account_tree, withdrawal="", deposit=""):
        return None

    def fake_historical_prefix_match(desc, historical_mappings):
        if "ROWA" in desc:
            return {"account": "Expenses:WeakKeep", "reason": "synthetic weak", "confidence": "weak"}
        if "ROWB" in desc:
            return {"account": "Expenses:WeakReplace", "reason": "synthetic weak", "confidence": "weak"}
        return None  # ROWC: no match at all

    monkeypatch.setattr(agent, "smart_pattern_match", fake_smart_pattern_match)
    monkeypatch.setattr(agent, "_historical_prefix_match", fake_historical_prefix_match)

    captured_calls = []

    def fake_llm_fallback_mapping(unmatched_rows, account_tree, example_mappings,
                                   config_path, model_override=None, historical_mappings=None):
        captured_calls.append({
            "unmatched_rows": list(unmatched_rows),
            "example_mappings": list(example_mappings),
        })
        results = {}
        for r in unmatched_rows:
            if "ROWB" in r["description"]:
                results[r["row"]] = {"account": "Expenses:LLMResolved", "reason": "synthetic llm"}
            # ROWA and ROWC: LLM finds nothing usable -- omitted from results.
        return results

    monkeypatch.setattr(agent, "llm_fallback_mapping", fake_llm_fallback_mapping)

    canonical_csv = _write_weak_flow_fixtures(tmp_path)
    output_path = tmp_path / "mapped_weak.csv"
    report_path = tmp_path / "mapped_weak_confidence.txt"

    result_str = agent.run(
        gnucash_file=str(tmp_path / "synthetic-nonexistent.gnucash"),
        canonical_csv=str(canonical_csv),
        output_path=str(output_path),
        config_path="dummy-config-truthy",  # only needs to be truthy; LLM call is faked
        model_override=None,
        bank_name=None,
        gnucash_bank_account=None,
    )
    assert isinstance(result_str, str)

    # --- The LLM was actually invoked, and reconsidered BOTH weak rows ---
    assert len(captured_calls) == 1
    seen_rows = {r["description"] for r in captured_calls[0]["unmatched_rows"]}
    assert any("ROWA" in d for d in seen_rows)
    assert any("ROWB" in d for d in seen_rows)
    assert any("ROWC" in d for d in seen_rows)

    # --- Weak rows must NEVER be used as LLM few-shot examples ---
    example_accounts = {e["account"] for e in captured_calls[0]["example_mappings"]}
    assert "Expenses:WeakKeep" not in example_accounts
    assert "Expenses:WeakReplace" not in example_accounts

    # --- Final CSV: row 1 kept weak, row 2 replaced by llm, row 3 suspense ---
    with open(output_path, newline="", encoding="utf-8") as f:
        rows = {r["Description"]: r for r in csv.DictReader(f)}

    row_a = rows["ROWA WEAK KEEP TXN"]
    assert row_a["Confidence"] == "weak"
    assert row_a["Account"] == "Expenses:WeakKeep"

    row_b = rows["ROWB WEAK REPLACE TXN"]
    assert row_b["Confidence"] == "llm"
    assert row_b["Account"] == "Expenses:LLMResolved"

    row_c = rows["ROWC NONE STAYS NONE TXN"]
    assert row_c["Confidence"] == "suspense"

    # --- Confidence counts / report rebuilt from the final CSV ---
    report_text = report_path.read_text(encoding="utf-8")
    assert "Weak keyword match" in report_text
    assert "    1  " in report_text  # weak count of 1 appears somewhere formatted
    # Row 1 (still 'weak') must be listed under manual review; row 2 (now
    # 'llm', resolved) must not be.
    assert "Items requiring review:" in report_text
    assert "ROWA WEAK KEEP TXN" in report_text
    assert "ROWB WEAK REPLACE TXN" not in report_text


def test_weak_confidence_appears_in_confidence_labels_and_manual_review_set():
    """Direct check of the two module-level registries the report and the
    manual-review section are driven from."""
    label_keys = dict(agent._CONFIDENCE_LABELS)
    assert label_keys.get("weak") == "Weak keyword match"
    assert "weak" in agent._MANUAL_REVIEW_CONFIDENCES
