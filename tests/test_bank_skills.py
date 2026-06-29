"""
tests/test_bank_skills.py — Unit tests for the BankSkill implementations
(v1.2 steps 3-4): BoBSkill, HSBCSkill, ICICISkill.

Two layers:
  * Synthesized-input unit tests (no corpus dependency) — the bulk of coverage:
    native-CSV/workbook mapping, NaN handling, detect() confidence, protocol
    conformance.
  * Corpus-backed regression tests, guarded by skipif so they run locally
    (where Data/Harshal is present) and skip cleanly in CI without the LFS
    blobs. These pin the post-refactor tie-out: BoB/ICICI byte-stable row
    counts + balances, HSBC balance reconciliation after the column-bug fix.

Run with:
    cd src && python -m pytest ../tests/test_bank_skills.py -v
"""
from __future__ import annotations

import csv
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

from agents.bank_contract import BankResult, BankSkill
from agents.skill_bob.agent import BoBSkill, _native_csv_to_canonical
from agents.skill_hsbc.agent import (
    HSBCSkill,
    _clean_str,
    _parse_date_hsbc,
    _parse_number_hsbc,
)
from agents.skill_icici.agent import ICICISkill, _read_canonical_csv

# ---------------------------------------------------------------------------
# Corpus fixtures (optional — skip when absent).
# ---------------------------------------------------------------------------

CORPUS = ROOT / "Data" / "Harshal"
BOB_PDF = CORPUS / "76000100001791.pdf"
HSBC_XLSX = CORPUS / "2026-04-19-HSBC-Savings-Enriched-v4-Apr2025-Mar2026.xlsx"
ICICI_XLS = CORPUS / "icici.xls"


def _present(p: Path) -> bool:
    # Guard against unresolved git-LFS pointer files (a few hundred bytes).
    return p.is_file() and p.stat().st_size > 4096


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cls", [BoBSkill, HSBCSkill, ICICISkill])
def test_implements_bank_skill_protocol(cls):
    assert isinstance(cls(), BankSkill)
    assert cls().bank_key in {"bob", "hsbc", "icici"}


# ---------------------------------------------------------------------------
# BoB — native CSV → canonical mapping (folded from adapter_bob)
# ---------------------------------------------------------------------------

BOB_NATIVE = [
    "DATE,PARTICULARS,CHQ.NO.,WITHDRAWALS,DEPOSITS,BALANCE",
    '01-04-2025,Opening Balance B/F,,,,"1,00,000.00"',
    '03-04-2025,NEFT SALARY,,,"50,000.00","1,50,000.00"',
    '05-04-2025,ATM CASH WDL,,"2,500.50",,"1,47,499.50"',
    '10-04-2025,CHQ PAYMENT,123456,"10,000.00",,"1,37,499.50"',
]


def _write_bob_native(tmp_path) -> Path:
    p = tmp_path / "bob_raw.csv"
    p.write_text("\n".join(BOB_NATIVE) + "\n", encoding="utf-8")
    return p


def test_bob_mapping_skips_opening_and_parses(tmp_path):
    rows, warnings = _native_csv_to_canonical(str(_write_bob_native(tmp_path)))
    assert warnings == []
    # The synthetic "Opening Balance B/F" row is dropped.
    assert len(rows) == 3
    assert [r["Description"] for r in rows] == ["NEFT SALARY", "ATM CASH WDL", "CHQ PAYMENT"]
    # ISO dates, Indian-number parsing, cheque → Transaction ID.
    assert rows[0] == {
        "Date": "2025-04-03", "Transaction ID": "", "Description": "NEFT SALARY",
        "Account": "", "Deposit": "50000.0", "Withdrawal": "0",
        "Balance": "150000.0", "Currency": "INR",
    }
    assert rows[2]["Transaction ID"] == "123456"
    assert rows[1]["Withdrawal"] == "2500.5"


def test_bob_parse_returns_balanced_result(tmp_path):
    # Drive parse() through the native-CSV mapping without a PDF by exercising
    # the canonical tail: build rows, then assert via a tiny CSV round-trip.
    rows, _ = _native_csv_to_canonical(str(_write_bob_native(tmp_path)))
    from agents.canonical_io import run_balance_check
    bc = run_balance_check(rows)
    assert bc.ok is True
    assert bc.opening_balance == 100000.0
    assert bc.closing_balance == 137499.5


def test_bob_detect_rejects_non_pdf(tmp_path):
    txt = tmp_path / "not_a_pdf.txt"
    txt.write_text("hello", encoding="utf-8")
    assert BoBSkill().detect(txt) == 0.0
    # Empty directory → no PDFs → 0.0
    assert BoBSkill().detect(tmp_path) == 0.0


# ---------------------------------------------------------------------------
# HSBC — number/date/string coercion units
# ---------------------------------------------------------------------------

def test_hsbc_parse_number_handles_nan_blank_none():
    assert _parse_number_hsbc(float("nan")) == "0"
    assert _parse_number_hsbc(None) == "0"
    assert _parse_number_hsbc("") == "0"
    assert _parse_number_hsbc("nan") == "0"
    assert _parse_number_hsbc("1,234.50") == "1234.5"
    assert _parse_number_hsbc(150000.0) == "150000.0"


def test_hsbc_clean_str_blanks_nan():
    assert _clean_str(float("nan")) == ""
    assert _clean_str(None) == ""
    assert _clean_str("nan") == ""
    assert _clean_str("  REF1 ") == "REF1"


def test_hsbc_parse_date_formats():
    from datetime import datetime
    assert _parse_date_hsbc(datetime(2025, 4, 1)) == "2025-04-01"
    assert _parse_date_hsbc("01/04/2025") == "2025-04-01"
    assert _parse_date_hsbc("2025-04-01") == "2025-04-01"
    assert _parse_date_hsbc("garbage") is None


def _build_hsbc_workbook(path: Path) -> None:
    """Write a minimal enriched-HSBC-style workbook (real column names)."""
    openpyxl = pytest.importorskip("openpyxl")
    from datetime import datetime
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "HSBC Savings TestPeriod"
    ws.append([
        "Date", "Transaction Details", "Transaction Date", "Transaction Number",
        "Extra Information", "Deposit", "Withdrawals", "Balance",
    ])
    # Brought-forward row: NaN amounts + NaN txn number (the old bug source).
    ws.append([datetime(2025, 3, 31), "BALANCE BROUGHT FORWARD",
               datetime(2025, 3, 31), None, None, None, None, 1000.0])
    ws.append([datetime(2025, 4, 1), "Xfer to self", datetime(2025, 4, 1),
               "REF1", None, None, 200.0, 800.0])
    ws.append([datetime(2025, 4, 2), "Salary", datetime(2025, 4, 2),
               None, None, 500.0, None, 1300.0])
    # Row with a blank (NaN) description — must coerce to "".
    ws.append([datetime(2025, 4, 3), None, datetime(2025, 4, 3),
               None, None, None, 100.0, 1200.0])
    wb.save(path)


def test_hsbc_parse_maps_columns_and_reconciles(tmp_path):
    pytest.importorskip("pandas")
    xlsx = tmp_path / "hsbc.xlsx"
    _build_hsbc_workbook(xlsx)

    res = HSBCSkill().parse(xlsx, output_path=tmp_path / "out.csv")
    assert isinstance(res, BankResult)
    assert res.row_count == 4
    # The bug fix: descriptions + withdrawals are now populated, NaN → "0"/"".
    assert res.rows[0]["Transaction ID"] == ""          # was the literal "nan"
    assert res.rows[0]["Description"] == "BALANCE BROUGHT FORWARD"
    assert res.rows[1]["Description"] == "Xfer to self"
    assert res.rows[1]["Withdrawal"] == "200.0"          # was dropped before
    assert res.rows[1]["Transaction ID"] == "REF1"
    assert res.rows[3]["Description"] == ""              # NaN details → blank
    # opening = 1000.0 (was nan), running balance reconciles.
    assert res.balance_check.ok is True
    assert res.opening_balance == 1000.0
    assert res.closing_balance == 1200.0
    # Sidecar written next to the canonical CSV.
    assert res.sidecar_path is not None and res.sidecar_path.is_file()


def test_hsbc_detect_on_workbook(tmp_path):
    pytest.importorskip("openpyxl")
    xlsx = tmp_path / "hsbc.xlsx"
    _build_hsbc_workbook(xlsx)
    assert HSBCSkill().detect(xlsx) == 0.9
    assert HSBCSkill().detect(tmp_path / "nope.pdf") == 0.0


# ---------------------------------------------------------------------------
# ICICI — canonical CSV round-trip + detect rejection
# ---------------------------------------------------------------------------

def test_icici_read_canonical_round_trip(tmp_path):
    csv_path = tmp_path / "c.csv"
    rows = [{
        "Date": "2025-04-01", "Transaction ID": "", "Description": "X",
        "Account": "", "Deposit": "1.0", "Withdrawal": "0",
        "Balance": "1.0", "Currency": "INR",
    }]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    assert _read_canonical_csv(csv_path) == rows


def test_icici_detect_rejects_non_xls(tmp_path):
    p = tmp_path / "foo.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    assert ICICISkill().detect(p) == 0.0


# ---------------------------------------------------------------------------
# Corpus-backed regression (tie-out) — skipped when Data/Harshal is absent
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _present(BOB_PDF), reason="BoB corpus PDF not available")
def test_bob_corpus_tieout(tmp_path):
    res = BoBSkill().parse(BOB_PDF, output_path=tmp_path / "bob.csv")
    assert BoBSkill().detect(BOB_PDF) == 0.95
    assert res.row_count == 74
    assert res.balance_check.ok is True
    assert res.opening_balance == 1500196.76
    assert res.closing_balance == 528419.34


@pytest.mark.skipif(not _present(HSBC_XLSX), reason="HSBC corpus workbook not available")
def test_hsbc_corpus_tieout(tmp_path):
    pytest.importorskip("pandas")
    res = HSBCSkill().parse(HSBC_XLSX, output_path=tmp_path / "hsbc.csv")
    assert HSBCSkill().detect(HSBC_XLSX) == 0.9
    assert res.row_count == 482
    # The deliberate column-bug fix: balance now reconciles, opening is real.
    assert res.balance_check.ok is True
    assert res.opening_balance == 242776.41
    assert res.closing_balance == 179301.62
    populated = sum(1 for r in res.rows if r["Description"].strip())
    assert populated >= 470   # was 0 before the fix


@pytest.mark.skipif(not _present(ICICI_XLS), reason="ICICI corpus XLS not available")
def test_icici_corpus_tieout(tmp_path):
    res = ICICISkill().parse(ICICI_XLS, output_path=tmp_path / "icici.csv")
    assert ICICISkill().detect(ICICI_XLS) == 0.9
    assert res.row_count == 465
    assert res.balance_check.ok is True
    assert res.opening_balance == 814745.2
    assert res.closing_balance == 12261.17
