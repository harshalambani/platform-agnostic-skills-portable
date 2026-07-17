"""
tests/test_banks_registry.py — Unit tests for agents/banks.py (P1 contracts
session): bank-parser discovery via `bank: true` skill.yaml manifests.

Run with:
    cd src && python -m pytest ../tests/test_banks_registry.py -v
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
for _extra in ("skill_bob", "skill_icici", "skill_hsbc"):
    _dir = ROOT / "tests" / _extra
    if str(_dir) not in sys.path:
        sys.path.insert(0, str(_dir))

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


# ---------------------------------------------------------------------------
# P3a — contract conformance: every discovered bank's parse() is exactly
# (path, password=None), no output_path side-channel left anywhere.
# ---------------------------------------------------------------------------

def test_all_discovered_banks_parse_signature_matches_contract():
    found = banks.discover()
    assert len(found) == 4, f"expected 4 banks, discovered {len(found)}: {found}"
    for info in found:
        skill = banks.load_bank_skill(info)
        sig = inspect.signature(skill.parse)
        params = list(sig.parameters.values())
        names = [p.name for p in params]
        assert names == ["path", "password"], (
            f"{info.bank_key}.parse() has parameters {names}, "
            f"expected exactly ['path', 'password'] -- no output_path"
        )
        assert params[1].default is None


# ---------------------------------------------------------------------------
# P3a — registry round-trip: one synthetic fixture per bank, parsed through
# the registry ONLY (no direct `from skill_* import`), proving the wiring
# phase didn't change any bank's canonical output.
# ---------------------------------------------------------------------------

def test_registry_round_trip_bob(tmp_path):
    import bob_fixture_gen

    info = banks.get("bob")
    skill = banks.load_bank_skill(info)
    pdf_path = tmp_path / "syn_bob.pdf"
    pdf_path.write_bytes(bob_fixture_gen.build_pdf())

    result = skill.parse(pdf_path)
    assert isinstance(result, BankResult)
    assert result.bank_key == "bob"
    assert result.row_count == len(bob_fixture_gen.SYN_TRANSACTIONS)
    assert result.opening_balance == bob_fixture_gen.SYN_OPENING_BALANCE
    assert result.closing_balance == bob_fixture_gen.SYN_CLOSING_BALANCE
    assert result.balance_check.ok is True


def test_registry_round_trip_icici(tmp_path):
    import icici_fixture_gen

    info = banks.get("icici")
    skill = banks.load_bank_skill(info)
    xls_path = tmp_path / "syn_icici.xls"
    xls_path.write_bytes(icici_fixture_gen.build_xls())

    result = skill.parse(xls_path)
    assert isinstance(result, BankResult)
    assert result.bank_key == "icici"
    assert result.row_count == len(icici_fixture_gen.SYN_TRANSACTIONS)
    assert result.opening_balance == icici_fixture_gen.SYN_OPENING_BALANCE
    assert result.closing_balance == icici_fixture_gen.SYN_CLOSING_BALANCE
    assert result.balance_check.ok is True


def test_registry_round_trip_hsbc(tmp_path):
    import hsbc_fixture_gen

    info = banks.get("hsbc")
    skill = banks.load_bank_skill(info)
    xlsx_path = tmp_path / "syn_hsbc_enriched.xlsx"
    xlsx_path.write_bytes(hsbc_fixture_gen.build_xlsx())

    result = skill.parse(xlsx_path)
    assert isinstance(result, BankResult)
    assert result.bank_key == "hsbc"
    assert result.opening_balance == hsbc_fixture_gen.SYN_OPENING_BALANCE
    assert result.closing_balance == hsbc_fixture_gen.SYN_CLOSING_BALANCE
    assert result.balance_check.ok is True
