"""
tests/test_bank_contract.py — Unit tests for agents/bank_contract.py (v1.2 step 1).

Covers:
  - BalanceCheck: immutability, defaults, and from_running() mapping
  - BankResult: construction, defaults, row_count, immutability
  - BankSkill: @runtime_checkable structural conformance

Run with:
    cd src && python -m pytest ../tests/test_bank_contract.py -v
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — make src/ importable.
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.bank_contract import BalanceCheck, BankResult, BankSkill


# ---------------------------------------------------------------------------
# BalanceCheck
# ---------------------------------------------------------------------------

def test_balance_check_is_frozen():
    bc = BalanceCheck(ok=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        bc.ok = False  # type: ignore[misc]


def test_balance_check_defaults():
    bc = BalanceCheck(ok=True)
    assert bc.mismatches == 0
    assert bc.first_mismatch is None
    assert bc.details == ()
    assert bc.opening_balance == 0.0
    assert bc.closing_balance == 0.0


def test_balance_check_from_running_ok():
    running = {
        "ok": True,
        "mismatches": 0,
        "details": [],
        "opening_balance": 100.0,
        "closing_balance": 250.5,
    }
    bc = BalanceCheck.from_running(running)
    assert bc.ok is True
    assert bc.mismatches == 0
    assert bc.first_mismatch is None
    assert bc.details == ()
    assert bc.opening_balance == 100.0
    assert bc.closing_balance == 250.5


def test_balance_check_from_running_with_mismatches():
    running = {
        "ok": False,
        "mismatches": 2,
        "details": ["Row 3 (..): expected 10.00, got 9.00", "Row 7 (..): ..."],
        "opening_balance": 0.0,
        "closing_balance": 5.0,
    }
    bc = BalanceCheck.from_running(running)
    assert bc.ok is False
    assert bc.mismatches == 2
    assert bc.first_mismatch == "Row 3 (..): expected 10.00, got 9.00"
    assert len(bc.details) == 2
    assert isinstance(bc.details, tuple)


# ---------------------------------------------------------------------------
# BankResult
# ---------------------------------------------------------------------------

def test_bank_result_minimal_construction_and_defaults():
    rows = [{"Date": "2025-04-01"}, {"Date": "2025-04-02"}]
    br = BankResult(rows=rows, bank_key="bob")
    assert br.bank_key == "bob"
    assert br.account_label == ""
    assert br.currency == "INR"
    assert br.opening_balance == 0.0
    assert br.closing_balance == 0.0
    assert isinstance(br.balance_check, BalanceCheck)
    assert br.balance_check.ok is True
    assert br.sidecar_path is None
    assert br.warnings == []
    assert br.row_count == 2


def test_bank_result_is_frozen():
    br = BankResult(rows=[], bank_key="hsbc")
    with pytest.raises(dataclasses.FrozenInstanceError):
        br.bank_key = "icici"  # type: ignore[misc]


def test_bank_result_warnings_are_independent_per_instance():
    a = BankResult(rows=[], bank_key="a")
    b = BankResult(rows=[], bank_key="b")
    a.warnings.append("oops")
    assert a.warnings == ["oops"]
    assert b.warnings == []  # default_factory, not a shared mutable default


def test_bank_result_full_construction():
    sidecar = Path("out.csv_summary.json")
    bc = BalanceCheck(ok=True, opening_balance=1.0, closing_balance=2.0)
    br = BankResult(
        rows=[{"Date": "2025-04-01"}],
        bank_key="hsbc",
        account_label="HSBC Savings",
        currency="INR",
        opening_balance=1.0,
        closing_balance=2.0,
        balance_check=bc,
        sidecar_path=sidecar,
        warnings=["minor"],
    )
    assert br.account_label == "HSBC Savings"
    assert br.sidecar_path == sidecar
    assert br.balance_check is bc
    assert br.warnings == ["minor"]


# ---------------------------------------------------------------------------
# BankSkill protocol (runtime_checkable)
# ---------------------------------------------------------------------------

class _ConformingBank:
    def detect(self, path):
        return 1.0

    def parse(self, path, password=None):
        return BankResult(rows=[], bank_key="x")

    def formats(self):
        return (".pdf",)


class _MissingParse:
    def detect(self, path):
        return 0.0

    def formats(self):
        return (".pdf",)


class _MissingDetect:
    def parse(self, path, password=None):
        return BankResult(rows=[], bank_key="x")

    def formats(self):
        return (".pdf",)


class _MissingFormats:
    def detect(self, path):
        return 0.0

    def parse(self, path, password=None):
        return BankResult(rows=[], bank_key="x")


def test_conforming_bank_is_instance():
    assert isinstance(_ConformingBank(), BankSkill)


def test_missing_method_is_not_instance():
    assert not isinstance(_MissingParse(), BankSkill)
    assert not isinstance(_MissingDetect(), BankSkill)
    assert not isinstance(_MissingFormats(), BankSkill)


def test_conforming_bank_round_trips():
    bank: BankSkill = _ConformingBank()
    assert bank.detect("anything") == 1.0
    assert isinstance(bank.parse("anything"), BankResult)
    assert bank.formats() == (".pdf",)


# ---------------------------------------------------------------------------
# BankStatementMeta / RowProvenance / BankResult.meta
# ---------------------------------------------------------------------------

def test_bank_result_meta_defaults_to_none():
    br = BankResult(rows=[], bank_key="hdfc")
    assert br.meta is None
    assert br.provenance == ()


def test_bank_statement_meta_defaults():
    from agents.bank_contract import BankStatementMeta
    meta = BankStatementMeta(bank_key="hdfc")
    assert meta.account_number is None
    assert meta.period_from is None
    assert meta.period_to is None
    assert meta.source_format == ""
    assert meta.fidelity == "exact"
    assert meta.password_used is False


def test_bank_statement_meta_is_frozen():
    from agents.bank_contract import BankStatementMeta
    meta = BankStatementMeta(bank_key="hdfc")
    with pytest.raises(dataclasses.FrozenInstanceError):
        meta.bank_key = "bob"  # type: ignore[misc]


def test_bank_result_carries_meta_and_provenance():
    from agents.bank_contract import BankStatementMeta, RowProvenance
    meta = BankStatementMeta(
        bank_key="hdfc", account_number="1234", source_format="pdf",
        fidelity="ocr-approx", password_used=True,
    )
    prov = (RowProvenance(row_index=0, page=1, source_line="01/04/25 ..."),)
    br = BankResult(rows=[{"Date": "2025-04-01"}], bank_key="hdfc", meta=meta, provenance=prov)
    assert br.meta is meta
    assert br.meta.password_used is True
    assert br.provenance == prov
