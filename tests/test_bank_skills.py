"""
tests/test_bank_skills.py — Unit tests for the BankSkill implementations
(v1.2 steps 3-4): BoBSkill, HSBCSkill, ICICISkill.

Two layers:
  * Synthesized-input unit tests (no corpus dependency) — the bulk of coverage:
    native-CSV/workbook mapping, NaN handling, detect() confidence, protocol
    conformance.
  * Corpus-backed regression tests, guarded by skipif so they run locally
    (where the corpus folder is present) and skip cleanly in CI without the LFS
    blobs. These pin the post-refactor tie-out: BoB/ICICI byte-stable row
    counts + balances, HSBC balance reconciliation after the column-bug fix.

Run with:
    cd src && python -m pytest ../tests/test_bank_skills.py -v
"""
from __future__ import annotations

import csv
import json
import os
import re
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

# Point PASK_BANK_CORPUS at the local folder of real statements; when it is unset
# the corpus-backed tests skip (the placeholder below never exists).
CORPUS = Path(os.environ.get("PASK_BANK_CORPUS") or (ROOT / "Data" / "_corpus_not_set"))


def _find_bob_pdf(folder: Path):
    """The single PDF in `folder` whose stem is exactly 14 digits, else None."""
    if not folder.is_dir():
        return None
    hits = [p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() == ".pdf" and re.fullmatch(r"\d{14}", p.stem)]
    return hits[0] if len(hits) == 1 else None


def _find_unique(folder: Path, pattern: str):
    """The single file in `folder` matching the glob `pattern`; None when the
    folder is absent or when zero or several files match (never pick one)."""
    if not folder.is_dir():
        return None
    hits = [p for p in folder.glob(pattern) if p.is_file()]
    return hits[0] if len(hits) == 1 else None


BOB_PDF = _find_bob_pdf(CORPUS)
HSBC_XLSX = _find_unique(CORPUS, "*HSBC*Savings*.xlsx")
ICICI_XLS = CORPUS / "icici.xls"


def _expected_balances(corpus: Path, key: str):
    """(opening, closing) for `key` from <corpus>/expected_balances.json.

    The sidecar is a local file written by the owner, shape
    {"bob": {"opening": n, "closing": n}, "hsbc": {...}, "icici": {...}}.
    Returns None when the file or the key is absent. A file that exists but is
    malformed raises (fails loud) instead of being treated as absent.
    """
    side = corpus / "expected_balances.json"
    if not side.is_file():
        return None
    data = json.loads(side.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected_balances.json must hold a JSON object")
    if key not in data:
        return None
    entry = data[key]
    if not isinstance(entry, dict):
        raise ValueError(f"expected_balances.json[{key!r}] must be an object")
    vals = []
    for field in ("opening", "closing"):
        v = entry.get(field)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"expected_balances.json[{key!r}][{field!r}] must be a number")
        vals.append(v)
    return vals[0], vals[1]


def _check_balances(res, key: str, corpus: Path = None):
    """Compare opening/closing against the sidecar (same == comparison as
    before); skip only this comparison when no expectation is available."""
    exp = _expected_balances(CORPUS if corpus is None else corpus, key)
    if exp is None:
        pytest.skip(f"no expected balances for {key!r} in expected_balances.json "
                    "(row count, detect and balance_check were still checked)")
    assert res.opening_balance == exp[0]
    assert res.closing_balance == exp[1]


def _present(p) -> bool:
    # Guard against unresolved git-LFS pointer files (a few hundred bytes).
    return p is not None and p.is_file() and p.stat().st_size > 4096


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

    res = HSBCSkill().parse(xlsx)
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
    # parse() returns rows only — writing the CSV/sidecar is the caller's job.
    assert res.sidecar_path is None


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
# Corpus-backed regression (tie-out) — skipped when the corpus folder is absent
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _present(BOB_PDF), reason="BoB corpus PDF not available")
def test_bob_corpus_tieout(tmp_path):
    res = BoBSkill().parse(BOB_PDF)
    assert BoBSkill().detect(BOB_PDF) == 0.95
    assert res.row_count == 74
    assert res.balance_check.ok is True
    _check_balances(res, "bob")


@pytest.mark.skipif(not _present(HSBC_XLSX), reason="HSBC corpus workbook not available")
def test_hsbc_corpus_tieout(tmp_path):
    pytest.importorskip("pandas")
    res = HSBCSkill().parse(HSBC_XLSX)
    assert HSBCSkill().detect(HSBC_XLSX) == 0.9
    assert res.row_count == 482
    # The deliberate column-bug fix: balance now reconciles, opening is real.
    assert res.balance_check.ok is True
    populated = sum(1 for r in res.rows if r["Description"].strip())
    assert populated >= 470   # was 0 before the fix
    _check_balances(res, "hsbc")


@pytest.mark.skipif(not _present(ICICI_XLS), reason="ICICI corpus XLS not available")
def test_icici_corpus_tieout(tmp_path):
    res = ICICISkill().parse(ICICI_XLS)
    assert ICICISkill().detect(ICICI_XLS) == 0.9
    assert res.row_count == 465
    assert res.balance_check.ok is True
    _check_balances(res, "icici")


# ---------------------------------------------------------------------------
# Sidecar handling and guard (synthetic, no corpus needed)
# ---------------------------------------------------------------------------

class _Res:
    def __init__(self, opening, closing):
        self.opening_balance = opening
        self.closing_balance = closing


def test_balance_sidecar_absent_skips_only_the_comparison(tmp_path):
    assert _expected_balances(tmp_path, "bob") is None
    with pytest.raises(pytest.skip.Exception):
        _check_balances(_Res(1.0, 2.0), "bob", corpus=tmp_path)


def test_balance_sidecar_key_missing_skips(tmp_path):
    (tmp_path / "expected_balances.json").write_text(
        json.dumps({"hsbc": {"opening": 1, "closing": 2}}), encoding="utf-8")
    with pytest.raises(pytest.skip.Exception):
        _check_balances(_Res(1, 2), "bob", corpus=tmp_path)


def test_balance_sidecar_matching_figures_pass(tmp_path):
    (tmp_path / "expected_balances.json").write_text(
        json.dumps({"bob": {"opening": 100.5, "closing": 200}}), encoding="utf-8")
    _check_balances(_Res(100.5, 200.0), "bob", corpus=tmp_path)


def test_balance_sidecar_wrong_figure_fails(tmp_path):
    (tmp_path / "expected_balances.json").write_text(
        json.dumps({"bob": {"opening": 100.5, "closing": 200}}), encoding="utf-8")
    with pytest.raises(AssertionError):
        _check_balances(_Res(100.5, 201.0), "bob", corpus=tmp_path)
    with pytest.raises(AssertionError):
        _check_balances(_Res(99.5, 200.0), "bob", corpus=tmp_path)


@pytest.mark.parametrize("content", [
    "{not json",
    "[1, 2]",
    json.dumps({"bob": 5}),
    json.dumps({"bob": {"opening": 1}}),
    json.dumps({"bob": {"opening": "1", "closing": 2}}),
])
def test_balance_sidecar_malformed_fails_loud(tmp_path, content):
    (tmp_path / "expected_balances.json").write_text(content, encoding="utf-8")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        _check_balances(_Res(1, 2), "bob", corpus=tmp_path)


def test_find_unique_two_matches_means_none(tmp_path):
    (tmp_path / "a-HSBC-Savings-x.xlsx").write_bytes(b"x")
    assert _find_unique(tmp_path, "*HSBC*Savings*.xlsx") == tmp_path / "a-HSBC-Savings-x.xlsx"
    (tmp_path / "b-HSBC-Savings-y.xlsx").write_bytes(b"x")
    assert _find_unique(tmp_path, "*HSBC*Savings*.xlsx") is None
    assert _find_unique(tmp_path / "missing", "*.xlsx") is None


def test_corpus_tests_hold_no_literal_balance_figures():
    """Guard: the tie-out tests must not carry real balances as literals."""
    src = Path(__file__).read_text(encoding="utf-8")
    start = src.index("# Corpus-backed regression (tie-out)")
    end = src.index("# Sidecar handling and guard")
    block = src[start:end]
    bad = re.findall(r"(?<![\w.])\d{5,}(?:\.\d+)?|(?<![\w.])\d{3,}\.\d{2,}", block)
    assert bad == [], "literal figures in the tie-out tests: %d" % len(bad)
