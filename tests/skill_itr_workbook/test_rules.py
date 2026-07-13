"""
tests/skill_itr_workbook/test_rules.py -- Batch 6 tests for scripts/rules.py:
loading tax_rules_<year>.yaml by canonical income-year key, regime/age-class
resolution, and user_rules.yaml loading. Fully offline; reads only the
committed Data/itr/rules/*.yaml config (public, non-PII).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
RULES_DIR = ROOT / "Data" / "itr" / "rules"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rules as rules_engine  # noqa: E402


def test_load_rules_regression_year():
    rules = rules_engine.load_rules(RULES_DIR, "2024-25")
    assert rules.year_key == "2024-25"
    assert rules.act == "1961"
    assert "AY 2025-26" in rules.year_label


def test_load_rules_live_year():
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    assert "AY 2026-27" in rules.year_label


def test_load_rules_unknown_year_raises():
    with pytest.raises(rules_engine.RulesError):
        rules_engine.load_rules(RULES_DIR, "1999-00")


def test_regime_new_and_old_blocks_present():
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    assert rules.regime("new")["slabs"][0]["rate"] == 0.0
    assert rules.regime("old")["slabs_by_age"]["general"][0]["upto"] == 250000


def test_regime_unknown_raises():
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    with pytest.raises(rules_engine.RulesError):
        rules.regime("flat")


@pytest.mark.parametrize("dob,fy_end,expected", [
    (None, date(2025, 3, 31), "general"),
    ("2000-01-01", date(2025, 3, 31), "general"),
    ("1960-01-01", date(2025, 3, 31), "senior"),      # exactly 65 at FY end
    ("1945-01-01", date(2025, 3, 31), "super_senior"),  # 80
])
def test_age_class(dob, fy_end, expected):
    assert rules_engine.age_class(dob, fy_end) == expected


def test_age_class_boundary_day_after_fy_end_not_yet_60():
    # DOB 1965-04-01: turns 60 on 2025-04-01, one day AFTER FY end 2025-03-31.
    assert rules_engine.age_class("1965-04-01", date(2025, 3, 31)) == "general"


def test_resolve_slabs_new_regime_ignores_age():
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    slabs = rules_engine.resolve_slabs(rules, "new", "Individual", "1945-01-01", date(2025, 3, 31))
    assert slabs == rules.regime("new")["slabs"]


def test_resolve_slabs_old_regime_huf_uses_huf_slabs():
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    slabs = rules_engine.resolve_slabs(rules, "old", "HUF", None, date(2025, 3, 31))
    assert slabs == rules.regime("old")["huf_slabs"]


def test_resolve_slabs_old_regime_senior_individual():
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    slabs = rules_engine.resolve_slabs(rules, "old", "Individual", "1960-01-01", date(2025, 3, 31))
    assert slabs == rules.regime("old")["slabs_by_age"]["senior"]


@pytest.mark.parametrize("status,dob,expected", [
    ("Individual", "1960-01-01", "senior"),      # 65 at FY end -- resolves normally
    ("Individual", None, "general"),              # no DOB on file -- never guess
    ("HUF", "1960-01-01", "general"),             # CF6: status != Individual -- doi lives here, never dob-like age math
    ("HUF", None, "general"),
])
def test_resolve_age_class_guard(status, dob, expected):
    assert rules_engine.resolve_age_class(status, dob, date(2025, 3, 31)) == expected


def test_load_user_rules_rule1_present():
    user_rules = rules_engine.load_user_rules(RULES_DIR / "user_rules.yaml")
    ids = [r.id for r in user_rules]
    assert "RULE-1" in ids
    rule1 = next(r for r in user_rules if r.id == "RULE-1")
    assert "refund" in rule1.statement.lower()
    assert rule1.status == "active"
