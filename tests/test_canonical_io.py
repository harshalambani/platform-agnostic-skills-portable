"""
tests/test_canonical_io.py — Unit tests for agents/canonical_io.py (v1.2 step 2).

Covers:
  - CANONICAL_FIELDS is the canonical 8-column schema
  - write_canonical_csv: parent-dir creation, exact bytes (CRLF + header), round-trip
  - derive_opening_closing delegates to balance_utils
  - run_balance_check returns a typed BalanceCheck
  - write_sidecar / read_sidecar round-trip and naming

Run with:
    cd src && python -m pytest ../tests/test_canonical_io.py -v
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — make src/ importable.
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import balance_utils
from agents.bank_contract import BalanceCheck
from agents.canonical_io import (
    CANONICAL_FIELDS,
    SIDECAR_SUFFIX,
    derive_opening_closing,
    read_sidecar,
    run_balance_check,
    write_canonical_csv,
    write_sidecar,
)


def _row(date, txn, desc, dep, wdl, bal):
    return {
        "Date": date,
        "Transaction ID": txn,
        "Description": desc,
        "Account": "",
        "Deposit": dep,
        "Withdrawal": wdl,
        "Balance": bal,
        "Currency": "INR",
    }


SAMPLE_ROWS = [
    _row("2025-04-03", "", "NEFT CREDIT", "50000.0", "0", "150000.0"),
    _row("2025-04-05", "", "ATM WDL", "0", "2500.5", "147499.5"),
    _row("2025-04-10", "123456", "CHQ PAYMENT", "0", "10000.0", "137499.5"),
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_canonical_fields_exact():
    assert CANONICAL_FIELDS == (
        "Date", "Transaction ID", "Description", "Account",
        "Deposit", "Withdrawal", "Balance", "Currency",
    )


# ---------------------------------------------------------------------------
# write_canonical_csv
# ---------------------------------------------------------------------------

def test_write_creates_parent_dirs_and_returns_path(tmp_path):
    out = tmp_path / "nested" / "deep" / "out.csv"
    returned = write_canonical_csv(SAMPLE_ROWS, out)
    assert returned == out
    assert out.is_file()


def test_write_exact_bytes_crlf_and_header(tmp_path):
    out = tmp_path / "out.csv"
    write_canonical_csv([_row("2025-04-03", "", "X", "1.0", "0", "1.0")], out)
    data = out.read_bytes()
    # csv module default line terminator is CRLF; file opened newline="".
    assert data == (
        b"Date,Transaction ID,Description,Account,"
        b"Deposit,Withdrawal,Balance,Currency\r\n"
        b"2025-04-03,,X,,1.0,0,1.0,INR\r\n"
    )


def test_write_round_trips(tmp_path):
    out = tmp_path / "out.csv"
    write_canonical_csv(SAMPLE_ROWS, out)
    with open(out, newline="", encoding="utf-8") as f:
        got = list(csv.DictReader(f))
    assert [dict(r) for r in got] == SAMPLE_ROWS


# ---------------------------------------------------------------------------
# Balance helpers
# ---------------------------------------------------------------------------

def test_derive_opening_closing_matches_balance_utils():
    assert derive_opening_closing(SAMPLE_ROWS) == \
        balance_utils.extract_opening_closing(SAMPLE_ROWS)


def test_derive_opening_closing_values():
    res = derive_opening_closing(SAMPLE_ROWS)
    # opening = first.balance - first.deposit + first.withdrawal
    assert res["opening_balance"] == 100000.0
    assert res["closing_balance"] == 137499.5
    assert res["row_count"] == 3


def test_run_balance_check_returns_typed_result_ok():
    bc = run_balance_check(SAMPLE_ROWS)
    assert isinstance(bc, BalanceCheck)
    assert bc.ok is True
    assert bc.mismatches == 0
    assert bc.opening_balance == 100000.0
    assert bc.closing_balance == 137499.5


def test_run_balance_check_flags_mismatch():
    bad = [
        _row("2025-04-03", "", "OPEN", "0", "0", "100.0"),
        _row("2025-04-04", "", "BAD", "10.0", "0", "999.0"),  # should be 110.0
    ]
    bc = run_balance_check(bad)
    assert bc.ok is False
    assert bc.mismatches == 1
    assert bc.first_mismatch is not None


# ---------------------------------------------------------------------------
# Sidecar
# ---------------------------------------------------------------------------

def test_sidecar_round_trip(tmp_path):
    canonical = tmp_path / "stmt.csv"
    write_canonical_csv(SAMPLE_ROWS, canonical)
    sidecar = write_sidecar(canonical, "Bank of Baroda", "derived",
                            100000.0, 137499.5, 3)
    assert sidecar == canonical.with_suffix(SIDECAR_SUFFIX)
    assert sidecar.is_file()

    data = read_sidecar(canonical)
    assert data == {
        "bank": "Bank of Baroda",
        "source": "derived",
        "opening_balance": 100000.0,
        "closing_balance": 137499.5,
        "row_count": 3,
    }
    # Sanity: matches what json.load sees directly.
    assert json.loads(sidecar.read_text(encoding="utf-8")) == data


def test_read_sidecar_missing_returns_none(tmp_path):
    assert read_sidecar(tmp_path / "no_such.csv") is None
