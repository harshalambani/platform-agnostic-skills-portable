"""
tests/skill_gnucash_account_mapper/test_confidence_report_reflects_final_state.py --
regression guard for MAP-09: the confidence report was written BEFORE the
passes that change the numbers.

Background (src/agents/skill_gnucash_account_mapper/agent.py):

  map_accounts() builds report_lines and writes the confidence report to
  disk, then returns. run() then runs THREE further passes that mutate
  result['confidence_counts'] and rewrite the mapped CSV -- the smart
  pattern pass, the LLM fallback pass, and the suspense pass -- but never
  used to rewrite the report file, so it went stale. Live symptom: the
  report claimed 458 "No match" while the mapped CSV had 459 assigned rows
  (1 low + 153 smart + 305 suspense) -- the report was frozen at the
  rules-pass snapshot.

  The fix (agent._rewrite_confidence_report_from_csv) recomputes the
  confidence report FROM the final mapped CSV -- the single source of
  truth -- rather than trusting any counter threaded through the pipeline,
  and run() now calls it after its final CSV write. It also surfaces the
  smart/llm/suspense/override categories in the report body, which existed
  in confidence_counts but never appeared in the rendered report before.

This test reproduces the same *shape* as the live symptom at a smaller,
fully synthetic scale: a rules pass that leaves several rows unmatched,
then a smart-pattern pass and a suspense pass (run()'s Step 4a / Step 5,
invoked here via the same real agent functions run() uses) that reassign
those rows -- mirroring exactly what run() does to mapped_rows between
map_accounts() and the final CSV write. All data is synthetic.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402


_CANONICAL_HEADERS = ["Date", "Description", "Withdrawal", "Deposit"]

# 10 synthetic rows, engineered so the rules pass alone leaves a shape
# analogous to the live 458-vs-459 symptom: some rows resolved by rules
# (high/low), several left "none" that the smart pass and suspense pass
# (run()'s Step 4a / Step 5) go on to reassign.
_CANONICAL_ROWS = [
    {"Date": "01-04-2025", "Description": "SALARY CREDIT APR", "Withdrawal": "", "Deposit": "50000.00"},
    {"Date": "02-04-2025", "Description": "UNCERTAIN MATCH XYZ", "Withdrawal": "100.00", "Deposit": ""},
    {"Date": "03-04-2025", "Description": "MIN BAL CHRGS MARCH", "Withdrawal": "50.00", "Deposit": ""},
    {"Date": "04-04-2025", "Description": "SMS CHARGES FEB", "Withdrawal": "5.00", "Deposit": ""},
    {"Date": "05-04-2025", "Description": "UNKNOWN TXN ONE", "Withdrawal": "10.00", "Deposit": ""},
    {"Date": "06-04-2025", "Description": "UNKNOWN TXN TWO", "Withdrawal": "20.00", "Deposit": ""},
    {"Date": "07-04-2025", "Description": "UNKNOWN TXN THREE", "Withdrawal": "30.00", "Deposit": ""},
    {"Date": "08-04-2025", "Description": "UNKNOWN TXN FOUR", "Withdrawal": "40.00", "Deposit": ""},
    {"Date": "09-04-2025", "Description": "UNKNOWN TXN FIVE", "Withdrawal": "50.00", "Deposit": ""},
    {"Date": "10-04-2025", "Description": "UNKNOWN TXN SIX", "Withdrawal": "60.00", "Deposit": ""},
]

_MAPPING_RULES = {
    "BankX": [
        {"patterns": ["SALARY"], "account": "Income:Salary", "confidence": "high",
         "reason": "Salary credit", "frequency": 5},
        {"patterns": ["UNCERTAIN MATCH"], "account": "Expenses:Misc", "confidence": "low",
         "reason": "fuzzy guess", "frequency": 1},
    ]
}

# Account tree used by the smart pattern pass -- includes a service-charge
# account so rows 3+4 ("MIN BAL CHRGS", "SMS CHARGES") resolve via
# smart_pattern_match's service-charge rule. Deliberately NO Suspense/
# Unclassified/Imbalance account, so _find_suspense_account falls back to
# its documented default.
_ACCOUNT_LIST = [
    "Income:Salary",
    "Expenses:Misc",
    "Expenses:Bank Service Charge",
]


def _write_fixtures(tmp_path: Path):
    canonical_csv = tmp_path / "canonical.csv"
    with open(canonical_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CANONICAL_HEADERS)
        writer.writeheader()
        writer.writerows(_CANONICAL_ROWS)

    mapping_yaml = tmp_path / "rules.yaml"
    mapping_yaml.write_text(yaml.dump(_MAPPING_RULES), encoding="utf-8")

    mapped_csv = tmp_path / "mapped.csv"
    report_path = tmp_path / "mapped_confidence.txt"
    return canonical_csv, mapping_yaml, mapped_csv, report_path


def _apply_smart_and_suspense_passes(mapped_csv: Path) -> None:
    """Mirror run()'s Step 4a (smart pattern pass) + Step 5 (suspense pass)
    on the CSV that map_accounts() wrote, using the SAME real agent
    functions run() calls (smart_pattern_match, _find_suspense_account,
    _strip_root) -- then rewrite the CSV, exactly as run() does."""
    with open(mapped_csv, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys())

    # Step 4a: smart pattern pass over rows still unmatched by rules.
    for row in rows:
        if row.get("Confidence") != "none" or row.get("Account"):
            continue
        desc = row.get("Description", "")
        match = agent.smart_pattern_match(desc, _ACCOUNT_LIST, row.get("Withdrawal", ""), row.get("Deposit", ""))
        if match is not None and match["account"]:
            row["Account"] = agent._strip_root(match["account"])
            row["Confidence"] = "smart"
            row["MatchReason"] = f"Smart: {match['reason']}"

    # Step 5: suspense pass -- assign whatever is still unmapped.
    suspense_acct = agent._find_suspense_account(_ACCOUNT_LIST)
    for row in rows:
        if not row.get("Account") or row.get("Confidence") == "none":
            row["Account"] = suspense_acct
            row["Confidence"] = "suspense"
            row["MatchReason"] = "Suspense — review and reassign in GnuCash"

    with open(mapped_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _tally_csv_confidences(mapped_csv: Path) -> dict:
    with open(mapped_csv, "r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    counts: dict = {}
    for row in rows:
        conf = row.get("Confidence") or "none"
        counts[conf] = counts.get(conf, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Core: report rewritten from the FINAL CSV matches the final CSV
# ---------------------------------------------------------------------------

def test_report_matches_final_csv_after_smart_and_suspense_passes(tmp_path):
    canonical_csv, mapping_yaml, mapped_csv, report_path = _write_fixtures(tmp_path)

    # map_accounts() runs the rules pass only -- this is the stale snapshot
    # the old code would have left the report frozen at.
    result = agent.map_accounts(str(canonical_csv), str(mapping_yaml), str(mapped_csv), str(report_path))
    rules_pass_report = report_path.read_text(encoding="utf-8")
    # Sanity: at the rules-pass snapshot, most rows are genuinely "none" --
    # this IS the stale state the fix must move past.
    assert result["confidence_counts"]["none"] == 8
    assert "No match" in rules_pass_report

    # run()'s further passes reassign those "none" rows.
    _apply_smart_and_suspense_passes(mapped_csv)

    # This is the fix under test: recompute + rewrite the report from the
    # FINAL csv, exactly as run() now does right after its final CSV write.
    final_counts = agent._rewrite_confidence_report_from_csv(str(mapped_csv), str(report_path))

    # Ground truth computed independently, straight from the CSV artefact.
    csv_counts = _tally_csv_confidences(mapped_csv)
    assert final_counts == csv_counts

    report_text = report_path.read_text(encoding="utf-8")
    for key, expected_count in csv_counts.items():
        # every non-zero category's count must be findable, formatted, in the report
        assert f"{expected_count:4d}" in report_text, (
            f"report does not show count {expected_count} for category {key!r}: {report_text}"
        )

    # The specific defect: "No match" must never overstate unassigned rows.
    unassigned_in_csv = sum(1 for r in csv.DictReader(open(mapped_csv, encoding="utf-8")) if not r.get("Account"))
    assert csv_counts.get("none", 0) == unassigned_in_csv == 0
    assert final_counts.get("none", 0) == 0


def test_stale_rules_pass_report_would_have_overstated_no_match(tmp_path):
    """Demonstrates the exact live symptom shape: the report map_accounts()
    wrote (rules pass only) claims a "No match" count that is HIGHER than
    the number of rows that are actually unassigned once the further
    passes (smart + suspense) have run -- proving the pre-rewrite report is
    stale relative to the CSV shipped beside it."""
    canonical_csv, mapping_yaml, mapped_csv, report_path = _write_fixtures(tmp_path)
    result = agent.map_accounts(str(canonical_csv), str(mapping_yaml), str(mapped_csv), str(report_path))
    stale_none_count = result["confidence_counts"]["none"]

    _apply_smart_and_suspense_passes(mapped_csv)
    actually_unassigned = sum(
        1 for r in csv.DictReader(open(mapped_csv, encoding="utf-8")) if not r.get("Account")
    )

    assert stale_none_count > actually_unassigned, (
        "fixture no longer reproduces the MAP-09 shape: the rules-pass "
        "'none' count must exceed the truly-unassigned count once the "
        "smart+suspense passes have run"
    )
    assert actually_unassigned == 0
    assert stale_none_count == 8


# ---------------------------------------------------------------------------
# Categories that existed in confidence_counts but were invisible in the
# report body before this fix
# ---------------------------------------------------------------------------

def test_smart_and_suspense_categories_appear_in_report_when_nonzero(tmp_path):
    canonical_csv, mapping_yaml, mapped_csv, report_path = _write_fixtures(tmp_path)
    agent.map_accounts(str(canonical_csv), str(mapping_yaml), str(mapped_csv), str(report_path))
    _apply_smart_and_suspense_passes(mapped_csv)
    agent._rewrite_confidence_report_from_csv(str(mapped_csv), str(report_path))

    report_text = report_path.read_text(encoding="utf-8")
    assert "Smart pattern match" in report_text
    assert "Suspense (unassigned)" in report_text
    # LLM fallback and override never fired in this fixture -- zero-count
    # categories should NOT clutter the report.
    assert "LLM fallback match" not in report_text
    assert "User override" not in report_text


def test_manual_review_section_lists_suspense_rows_not_stale_none_rows(tmp_path):
    canonical_csv, mapping_yaml, mapped_csv, report_path = _write_fixtures(tmp_path)
    agent.map_accounts(str(canonical_csv), str(mapping_yaml), str(mapped_csv), str(report_path))
    _apply_smart_and_suspense_passes(mapped_csv)
    agent._rewrite_confidence_report_from_csv(str(mapped_csv), str(report_path))

    report_text = report_path.read_text(encoding="utf-8")
    # 1 low-confidence row + 6 suspense rows = 7 items needing review;
    # the smart-matched rows (service charges) are resolved and must NOT
    # be listed as needing manual review.
    assert "Items requiring review: 7" in report_text
    assert "MIN BAL CHRGS" not in report_text
    assert "SMS CHARGES" not in report_text


# ---------------------------------------------------------------------------
# General invariant: report "No match" can never exceed genuinely
# unassigned rows in the CSV, even outside the exact fixture above
# ---------------------------------------------------------------------------

def test_no_match_count_never_exceeds_genuinely_unassigned_rows(tmp_path):
    """Direct check of the invariant on a hand-built CSV with a real
    residual 'none' row (no suspense pass applied) -- the reported count
    must equal, never exceed, the number of rows with an empty Account."""
    mapped_csv = tmp_path / "mapped2.csv"
    report_path = tmp_path / "mapped2_confidence.txt"
    fieldnames = ["Description", "Account", "Confidence", "MatchReason"]
    rows = [
        {"Description": "A", "Account": "Income:Salary", "Confidence": "high", "MatchReason": "r"},
        {"Description": "B", "Account": "", "Confidence": "none", "MatchReason": "No pattern match"},
        {"Description": "C", "Account": "", "Confidence": "none", "MatchReason": "No pattern match"},
    ]
    with open(mapped_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    counts = agent._rewrite_confidence_report_from_csv(str(mapped_csv), str(report_path))
    unassigned_in_csv = sum(1 for r in rows if not r["Account"])
    assert counts["none"] == unassigned_in_csv == 2


# ---------------------------------------------------------------------------
# Enabler for MAP-09, not a separately-scoped ledger item: while wiring the
# report rewrite into run(), a latent NameError was found — mapped_rows and
# account_list were only assigned inside `if unmatched_count > 0:`, but
# Step 5 (the suspense pass) and the final CSV/report writes reference both
# names unconditionally afterward. Any run where the rules pass alone
# resolves every row (unmatched_count == 0 — the BEST case, not an edge
# case) hit this and crashed with NameError before ever reaching the
# report-rewrite fix under test above. This test drives agent.run() itself
# through exactly that path with fully synthetic fixtures.
# ---------------------------------------------------------------------------

_FULL_MATCH_RULES_BY_BANK = {
    "BankX": [
        {"patterns": ["SALARY"], "account": "Income:Salary", "confidence": "high",
         "reason": "Salary credit", "frequency": 5},
        {"patterns": ["RENT"], "account": "Expenses:Rent", "confidence": "high",
         "reason": "Rent payment", "frequency": 5},
    ]
}

_FULL_MATCH_CANONICAL_ROWS = [
    {"Date": "01-04-2025", "Description": "SALARY CREDIT APR", "Withdrawal": "", "Deposit": "50000.00"},
    {"Date": "02-04-2025", "Description": "RENT PAYMENT APR", "Withdrawal": "15000.00", "Deposit": ""},
]

AGENTS_ROOT = SRC / "agents"
if str(AGENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENTS_ROOT))


def test_run_completes_and_reports_correctly_when_rules_resolve_every_row(tmp_path, monkeypatch):
    """run() end-to-end, with every external dependency faked so no real
    .gnucash file / network / LLM is ever touched, on a fixture engineered
    so the rules pass alone resolves 100% of rows (unmatched_count == 0
    after map_accounts()). Pre-fix, this raised NameError inside Step 5
    before mapped_rows/account_list existed. Post-fix, it must complete
    and leave a confidence report that matches the final CSV exactly, with
    zero "no match" / suspense rows."""
    # Bare/undotted module objects — run() resolves its sibling-agent
    # imports via `sys.path.insert(0, agents_root)` + bare `from X import Y`,
    # which caches them in sys.modules under undotted keys distinct from
    # this test file's own `agents.<pkg>.<mod>` dotted imports. Patching
    # here, under the SAME undotted keys run() itself will import, is what
    # makes the monkeypatch visible to run().
    import skill_gnucash_xml_extractor.agent as xml_agent_mod
    import skill_gnucash_mapping_generator.agent as mapgen_mod
    import skill_gnucash_account_mapper.persistent_rules as persistent_rules_mod

    def fake_parse_gnucash_file(path):
        return {"mappings": {"BankX": [
            {"account": "Income:Salary"}, {"account": "Expenses:Rent"},
        ]}}

    def fake_generate_rules(extractor_output, min_freq=1):
        return _FULL_MATCH_RULES_BY_BANK

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

    canonical_csv = tmp_path / "canonical_full_match.csv"
    with open(canonical_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CANONICAL_HEADERS)
        writer.writeheader()
        writer.writerows(_FULL_MATCH_CANONICAL_ROWS)

    output_path = tmp_path / "mapped_full_match.csv"
    report_path = tmp_path / "mapped_full_match_confidence.txt"

    # gnucash_file deliberately does not exist: run()'s special-account
    # filter (agents.gnucash_accounts.read_special_paths) is wrapped in a
    # broad try/except that only logs and continues, so a missing/synthetic
    # path is safe here and needs no separate mock.
    result_str = agent.run(
        gnucash_file=str(tmp_path / "synthetic-nonexistent.gnucash"),
        canonical_csv=str(canonical_csv),
        output_path=str(output_path),
        config_path=None,
        model_override=None,
        bank_name=None,
        gnucash_bank_account=None,
    )

    # The crash under test: pre-fix, run() never got this far.
    assert isinstance(result_str, str)
    assert "Mapped **2 rows**" in result_str

    with open(output_path, newline="", encoding="utf-8") as f:
        mapped_rows = list(csv.DictReader(f))
    assert len(mapped_rows) == 2
    assert all(row.get("Confidence") == "high" for row in mapped_rows)
    assert all(row.get("Account") for row in mapped_rows)
    # The specific wrong behaviour this guards against: no row should ever
    # fall through to suspense when the rules pass already resolved it.
    assert not any(row.get("Confidence") == "suspense" for row in mapped_rows)

    # Report on disk must be the FINAL, correct state -- not stale, and
    # internally consistent with the CSV sitting beside it (MAP-09's own
    # invariant, exercised here via the path that used to crash first).
    final_counts = agent._rewrite_confidence_report_from_csv(str(output_path), str(report_path))
    csv_counts = _tally_csv_confidences(output_path)
    assert final_counts == csv_counts
    assert final_counts.get("none", 0) == 0
    assert final_counts.get("suspense", 0) == 0
    assert final_counts.get("high", 0) == 2
