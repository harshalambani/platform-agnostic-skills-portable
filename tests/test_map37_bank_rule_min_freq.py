"""
MAP-37 -- a bank-specific rule learned from book history needs the
description booked at least TWICE. A one-off payee is not a pattern.

Synthetic fixtures only (no real names, amounts or account numbers).
Every behaviour carries NEGATIVE tests: the old min_freq=1 must NOT come back.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "src" / "agents", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
from agents.skill_gnucash_mapping_generator import agent as gen  # noqa: E402

TWICE_ACCT = "Expenses:Twice Supplier"
ONCE_ACCT = "Expenses:Once Supplier"
OVR_ACCT = "Expenses:Override Target"
DESC_TWICE = "UPI/ZZTWICE/zztwice@ybl/Payment"
DESC_ONCE = "UPI/ZZONCE/zzonce@ybl/Payment"
SUSPENSE = m._SUSPENSE_DEFAULT
RULE_LEVELS = ("high", "medium", "low", "override")


def _extractor(rows):
    return {"mappings": {"HDFC": [
        {"description": d, "account": a, "frequency": f, "last_date": "2026-01-10"}
        for d, a, f in rows]}}


def _patterns(rules_by_bank):
    return [p for rs in rules_by_bank.values() for r in rs for p in r["patterns"]]


# ---------------------------------------------------------------- the constant

def test_constant_is_two_and_generic_path_is_unchanged():
    assert m.BANK_RULE_MIN_FREQ == 2
    # NEGATIVE: not back to 1
    assert m.BANK_RULE_MIN_FREQ != 1


# ---------------------------------------------------- generate_rules behaviour

def test_description_booked_twice_produces_a_bank_rule():
    out = gen.generate_rules(_extractor([(DESC_TWICE, TWICE_ACCT, 2)]),
                             min_freq=m.BANK_RULE_MIN_FREQ)
    assert "UPI/ZZTWICE/.*" in _patterns(out)


def test_description_booked_once_does_not_produce_a_rule():
    """NEGATIVE: a single booking is not enough."""
    out = gen.generate_rules(_extractor([(DESC_ONCE, ONCE_ACCT, 1)]),
                             min_freq=m.BANK_RULE_MIN_FREQ)
    assert _patterns(out) == []


def test_once_and_twice_together_only_twice_gets_a_rule():
    out = gen.generate_rules(_extractor([(DESC_TWICE, TWICE_ACCT, 2),
                                         (DESC_ONCE, ONCE_ACCT, 1)]),
                             min_freq=m.BANK_RULE_MIN_FREQ)
    pats = _patterns(out)
    assert "UPI/ZZTWICE/.*" in pats
    assert not any("ZZONCE" in p for p in pats)


# ------------------------------------------------------ full pipeline (run())

def _run_pipeline(tmp_path, monkeypatch, history, rows, existing_yaml=None,
                  overrides=None, bank_name="HDFC"):
    # run() imports these under their top-level (src/agents) names.
    import skill_gnucash_xml_extractor.agent as xml_mod
    import skill_gnucash_account_mapper.persistent_rules as pr
    import skill_gnucash_mapping_generator.agent as gen_top

    rules_file = tmp_path / "book_rules.yaml"
    if existing_yaml is not None:
        rules_file.write_text(yaml.safe_dump(existing_yaml), encoding="utf-8")
    monkeypatch.setattr(pr, "rules_path", lambda gf, cp=None: rules_file)
    monkeypatch.setattr(xml_mod, "parse_gnucash_file",
                        lambda path, gnucash_bank_account=None: _extractor(history))
    monkeypatch.setattr(m, "_emit_mapper_progress", lambda msg: None)
    if overrides:
        pr.save_overrides_batch("book.gnucash", overrides, None)

    seen = {}
    real = gen_top.generate_rules

    def spy(extractor_output, min_freq=3, **kw):
        seen["min_freq"] = min_freq
        return real(extractor_output, min_freq=min_freq, **kw)

    monkeypatch.setattr(gen_top, "generate_rules", spy)

    csv_in = tmp_path / "in.csv"
    with open(csv_in, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["Date", "Description", "Withdrawal", "Deposit"])
        w.writeheader()
        for d in rows:
            w.writerow({"Date": "05-01-2026", "Description": d, "Withdrawal": "10.00", "Deposit": ""})
    out = tmp_path / "mapped.csv"
    m.run(gnucash_file=str(tmp_path / "book.gnucash"), canonical_csv=str(csv_in),
          output_path=str(out), config_path=None, model_override=None,
          bank_name=bank_name, gnucash_bank_account=None)
    with open(out, newline="", encoding="utf-8") as f:
        mapped = list(csv.DictReader(f))
    saved = yaml.safe_load(rules_file.read_text(encoding="utf-8")) or {}
    return seen, mapped, saved


def _acct(row):
    return row.get("Transfer Account") or row.get("Account") or ""


def test_run_passes_two_for_a_bank_and_three_for_the_generic_path(tmp_path, monkeypatch):
    seen, _, _ = _run_pipeline(tmp_path, monkeypatch,
                               [(DESC_TWICE, TWICE_ACCT, 2)], [DESC_TWICE])
    assert seen["min_freq"] == 2
    # NEGATIVE: not 1 for a bank
    assert seen["min_freq"] != 1
    (tmp_path / "g").mkdir()
    seen2, _, _ = _run_pipeline(tmp_path / "g", monkeypatch, [], [DESC_TWICE], bank_name=None)
    assert seen2["min_freq"] == 3


def test_run_twice_seen_gets_a_rule_once_seen_never_does(tmp_path, monkeypatch):
    seen, mapped, saved = _run_pipeline(
        tmp_path, monkeypatch,
        [(DESC_TWICE, TWICE_ACCT, 2), (DESC_ONCE, ONCE_ACCT, 1)],
        [DESC_TWICE, DESC_ONCE])
    saved_pats = [p for k, rs in saved.items() if k != "_overrides" for r in rs for p in r["patterns"]]
    assert "UPI/ZZTWICE/.*" in saved_pats
    assert not any("ZZONCE" in p for p in saved_pats)          # NEGATIVE
    by = {r["Description"]: r for r in mapped}
    assert _acct(by[DESC_TWICE]) == TWICE_ACCT
    assert by[DESC_TWICE]["Confidence"] in RULE_LEVELS
    # NEGATIVE: the once-seen row never lands via a rule. It may only fall to
    # the history / keyword fallback (flagged weak for review), AI or Suspense.
    assert by[DESC_ONCE]["Confidence"] not in RULE_LEVELS
    assert by[DESC_ONCE]["Confidence"] in ("weak", "history", "none", "suspense")


def test_existing_yaml_rule_for_a_once_seen_description_is_not_removed(tmp_path, monkeypatch):
    existing = {"HDFC": [{"patterns": ["UPI/ZZONCE/.*"], "account": ONCE_ACCT,
                          "confidence": "high", "source": "auto", "frequency": 1,
                          "last_date": "2026-01-10"}]}
    _, mapped, saved = _run_pipeline(
        tmp_path, monkeypatch, [(DESC_ONCE, ONCE_ACCT, 1)], [DESC_ONCE],
        existing_yaml=existing)
    pats = [p for r in saved["HDFC"] for p in r["patterns"]]
    assert "UPI/ZZONCE/.*" in pats                              # NEGATIVE: not pruned
    assert _acct(mapped[0]) == ONCE_ACCT                        # and it still applies


def test_override_still_wins_over_a_rule(tmp_path, monkeypatch):
    _, mapped, _ = _run_pipeline(
        tmp_path, monkeypatch, [(DESC_TWICE, TWICE_ACCT, 2)], [DESC_TWICE],
        overrides=[{"patterns": ["zztwice"], "account": OVR_ACCT}])
    assert _acct(mapped[0]) == OVR_ACCT
    assert _acct(mapped[0]) != TWICE_ACCT                       # NEGATIVE: rule did not win
