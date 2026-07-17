"""
tests/test_banks_registry.py — Unit tests for agents/banks.py (P1 contracts
session): bank-parser discovery via `bank: true` skill.yaml manifests.

Run with:
    cd src && python -m pytest ../tests/test_banks_registry.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import banks
from agents.bank_contract import BankResult, BankSkill


@pytest.fixture(autouse=True)
def _reset_cache():
    banks._cache = None
    yield
    banks._cache = None


def test_discover_finds_hdfc():
    found = banks.discover()
    keys = [b.bank_key for b in found]
    assert "hdfc" in keys


def test_discover_finds_bob():
    found = banks.discover()
    keys = [b.bank_key for b in found]
    assert "bob" in keys


def test_discover_finds_icici():
    found = banks.discover()
    keys = [b.bank_key for b in found]
    assert "icici" in keys


def test_discover_finds_hsbc():
    found = banks.discover()
    keys = [b.bank_key for b in found]
    assert "hsbc" in keys


def test_discover_is_sorted_by_display_name():
    found = banks.discover()
    names = [b.display_name for b in found]
    assert names == sorted(names)


def test_discover_is_cached_until_refresh():
    first = banks.discover()
    second = banks.discover()
    assert first == second
    refreshed = banks.discover(refresh=True)
    assert refreshed == first


def test_get_hdfc_case_insensitive():
    info = banks.get("HDFC")
    assert info is not None
    assert info.bank_key == "hdfc"
    assert info.package == "agents.skill_hdfc"


def test_get_bob_case_insensitive():
    info = banks.get("BOB")
    assert info is not None
    assert info.bank_key == "bob"
    assert info.package == "agents.skill_bob"


def test_get_icici_case_insensitive():
    info = banks.get("ICICI")
    assert info is not None
    assert info.bank_key == "icici"
    assert info.package == "agents.skill_icici"


def test_get_hsbc_case_insensitive():
    info = banks.get("HSBC")
    assert info is not None
    assert info.bank_key == "hsbc"
    assert info.package == "agents.skill_hsbc"


def test_get_unknown_bank_returns_none():
    assert banks.get("not_a_real_bank") is None


def test_load_bank_skill_returns_conforming_hdfc():
    info = banks.get("hdfc")
    skill = banks.load_bank_skill(info)
    assert isinstance(skill, BankSkill)
    assert ".pdf" in skill.formats()


def test_load_bank_skill_parse_round_trips_on_synthetic_csv(tmp_path):
    info = banks.get("hdfc")
    skill = banks.load_bank_skill(info)

    csv_path = tmp_path / "stmt.csv"
    csv_path.write_text(
        "Value Date,Narration,Number,Withdrawal,Deposit,Balance\n"
        "01/04/2025,NEFT CREDIT,000123,,50000.00,150000.00\n",
        encoding="utf-8",
    )
    result = skill.parse(str(csv_path))
    assert isinstance(result, BankResult)
    assert result.bank_key == "hdfc"
    assert result.row_count == 1
    assert result.meta is not None
    assert result.meta.bank_key == "hdfc"
    assert result.meta.password_used is False


def test_load_bank_skill_returns_conforming_bob():
    info = banks.get("bob")
    skill = banks.load_bank_skill(info)
    assert isinstance(skill, BankSkill)
    assert ".pdf" in skill.formats()


def test_load_bank_skill_returns_conforming_icici():
    info = banks.get("icici")
    skill = banks.load_bank_skill(info)
    assert isinstance(skill, BankSkill)
    assert ".xls" in skill.formats()


def test_load_bank_skill_returns_conforming_hsbc():
    info = banks.get("hsbc")
    skill = banks.load_bank_skill(info)
    assert isinstance(skill, BankSkill)
    assert ".pdf" in skill.formats()


def test_discover_skips_non_bank_manifests():
    """Sanity: not every discovered skill is a bank -- e.g. the GnuCash
    pipeline itself must not show up (it has no `bank: true` key)."""
    found = banks.discover()
    packages = [b.package for b in found]
    assert "agents.skill_gnucash_pipeline" not in packages
