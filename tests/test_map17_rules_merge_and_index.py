"""
MAP-17 -- persistent-rules merge must not add near-duplicates, and the apply
pass must be indexed without changing which rows map.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
from agents.skill_gnucash_account_mapper import persistent_rules as pr  # noqa: E402


def _rule(pats, acct="Expenses:Food:Dining", conf="medium"):
    return {"patterns": list(pats), "account": acct, "confidence": conf,
            "frequency": 2, "reason": "2 occurrences"}


def _pin(monkeypatch, tmp_path):
    rp = tmp_path / "book_mapping_rules.yaml"
    monkeypatch.setattr(pr, "rules_path", lambda *a, **k: rp)
    return rp


def _n(rules):
    return sum(len(v) for v in rules.values())


def test_same_statement_twice_adds_no_rules(tmp_path, monkeypatch):
    _pin(monkeypatch, tmp_path)
    gen = lambda: {"HDFC": [_rule(["UPI/ACMECAFE/.*"]), _rule([".*445566.*"], "Expenses:Groceries"),
                            _rule(["NEFT.*ZORBA.*"], "Expenses:Fuel")]}
    first = pr.merge_auto_rules("book.gnucash", gen())
    second = pr.merge_auto_rules("book.gnucash", gen())
    assert _n(first) == 3 and _n(second) == 3


def test_near_duplicate_variants_do_not_add_rules(tmp_path, monkeypatch):
    _pin(monkeypatch, tmp_path)
    pr.merge_auto_rules("book.gnucash", {"HDFC": [_rule(["NEFT.*ZORBA.*"])]})
    merged = pr.merge_auto_rules("book.gnucash", {"HDFC": [
        _rule(["neft.*zorba"]),            # case + redundant .*
        _rule([".*NEFT.*ZORBA.*"]),        # leading .*
    ]})
    assert _n(merged) == 1


def test_reordered_patterns_same_account_are_one_rule(tmp_path, monkeypatch):
    _pin(monkeypatch, tmp_path)
    pr.merge_auto_rules("book.gnucash", {"HDFC": [_rule(["UPI/CAFE/.*", ".*445566.*"])]})
    merged = pr.merge_auto_rules("book.gnucash", {"HDFC": [_rule([".*445566.*", "UPI/CAFE/.*"])]})
    assert _n(merged) == 1


def test_genuinely_different_rules_are_still_added(tmp_path, monkeypatch):
    """NEGATIVE: the dedupe must not swallow distinct merchants."""
    _pin(monkeypatch, tmp_path)
    pr.merge_auto_rules("book.gnucash", {"HDFC": [_rule(["UPI/CAFE/.*"])]})
    merged = pr.merge_auto_rules("book.gnucash", {"HDFC": [
        _rule(["UPI/CAFEX/.*"]), _rule([".*445566.*"]), _rule([".*445567.*"])]})
    assert _n(merged) == 4


def test_existing_rules_on_file_are_not_deleted_or_rewritten(tmp_path, monkeypatch):
    """NEGATIVE: near-duplicates already in the user's file stay; the merge
    of an unrelated rule leaves both untouched."""
    rp = _pin(monkeypatch, tmp_path)
    existing = {"HDFC": [_rule(["NEFT.*ZORBA.*"]), _rule(["neft.*zorba.*"]),
                         dict(_rule(["UPI/MINE/.*"], "Income:Interest"), source="user")]}
    rp.write_text(yaml.safe_dump(existing), encoding="utf-8")
    merged = pr.merge_auto_rules("book.gnucash", {"HDFC": [_rule([".*889900.*"])]})
    assert _n(merged) == 4
    pats = [r["patterns"][0] for r in merged["HDFC"]]
    assert "NEFT.*ZORBA.*" in pats and "neft.*zorba.*" in pats and "UPI/MINE/.*" in pats


def _synthetic(n_rules, seed=7):
    rnd = random.Random(seed)
    rules = []
    for i in range(n_rules):
        k = i % 4
        if k == 0:
            pats = [f"UPI/SHOP{i}/.*"]
        elif k == 1:
            pats = [f".*{100000 + i}.*"]
        elif k == 2:
            pats = [f"NEFT.*PAYEE{i}.*"]
        else:
            pats = [f"UPI/VEND{i}/.*", f".*{200000 + i}.*"]
        rules.append({"patterns": pats, "account": f"Expenses:E{i % 40}",
                      "confidence": rnd.choice(["high", "medium", "low"]),
                      "frequency": rnd.randint(1, 9), "reason": f"r{i}"})
    return rules


def _descs(n, n_rules, seed=11):
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        i = rnd.randrange(n_rules * 2)
        out.append(rnd.choice([
            f"UPI/SHOP{i}/x@bank/ref", f"IMPS {100000 + i} PAYMENT", f"neft-payee{i}-ltd",
            f"UPI/VEND{i}/y@bank", "SOMETHING UNRELATED", "CASH DEPOSIT"]))
    return out


def test_compiled_pass_maps_exactly_the_same_rows():
    rules = _synthetic(600)
    rules += [{"patterns": ["("], "account": "Expenses:Broken", "confidence": "low"},   # invalid regex
              {"patterns": [r"A\.B.*"], "account": "Expenses:Esc", "confidence": "low"},
              {"patterns": [".*"], "account": "Expenses:Catchall", "confidence": "low"}]
    rules.sort(key=lambda r: ({"high": 0, "medium": 1, "low": 2}[r["confidence"]], -r.get("frequency", 0)))
    compiled = m.CompiledRules(rules)
    for d in _descs(400, 600) + ["A.B", "AxB", "x(y", ""]:
        assert m.match_rule(d, compiled) == m.match_rule(d, rules), d


def test_rules_pass_output_identical_before_and_after_dedupe(tmp_path, monkeypatch):
    """Rows mapped from a rules file with near-duplicate variants equal rows
    mapped from the same file after the merge kept a single copy."""
    dupey = _synthetic(80)
    variants = [dict(r, patterns=[p.lower() for p in r["patterns"]]) for r in dupey]
    rules_all = dupey + variants
    cd = m.CompiledRules(sorted(rules_all, key=lambda r: ({"high": 0, "medium": 1, "low": 2}[r["confidence"]], -r["frequency"])))
    cu = m.CompiledRules(sorted(dupey, key=lambda r: ({"high": 0, "medium": 1, "low": 2}[r["confidence"]], -r["frequency"])))
    for d in _descs(300, 80):
        assert m.match_rule(d, cd)[0] == m.match_rule(d, cu)[0]
