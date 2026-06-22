"""
tests/test_26as.py — Tests for the 26AS extraction script.

Focus: the sub-total / reconciliation behaviour.

The core tests use synthetic deductor data (no PDF, no LLM) so they always run
in CI. They guard the specific regression that was fixed: deductor sub-total
cells must hold COMPUTED NUMERIC VALUES, not bare =SUM formulas — a bare formula
reads back as blank in any data_only consumer (the app preview, pandas, etc.).

An optional end-to-end test runs only when a fixture PDF is dropped at
tests/fixtures/sample_26as.pdf (none is committed, to avoid storing a real PAN).

Run with:
    cd src && python -m pytest ../tests/test_26as.py -v
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_26as" / "scripts" / "extract_26as_to_xlsx.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample_26as.pdf"


def _load_module():
    """Load the extraction script as a module straight from its file path."""
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("extract_26as_to_xlsx", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: dataclass annotation resolution looks the module up
    # in sys.modules by name while the class body executes.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load_module()


def _deductor(sr, name, tan, tot_amt, tot_tax, tot_tds, txns):
    """txns: list of (amount, tax, tds) tuples."""
    d = m.P1Deductor(sr=sr, name=name, tan=tan,
                     tot_amt=tot_amt, tot_tax=tot_tax, tot_tds=tot_tds)
    for i, (a, t, td) in enumerate(txns, 1):
        d.txns.append(m.P1Txn(sr=i, section="194A", txn_date="01-Apr-2025",
                              status="F", date_booking="01-May-2025",
                              remarks="-", amount=a, tax=t, tds=td))
    return d


# ---------------------------------------------------------------------------
# Script presence
# ---------------------------------------------------------------------------

def test_script_exists():
    assert SCRIPT.exists(), f"Script not found: {SCRIPT}"


# ---------------------------------------------------------------------------
# Sub-totals must be real numbers (the regression that was fixed)
# ---------------------------------------------------------------------------

def test_subtotals_are_numeric_in_data_only():
    """Sub-total and grand-total cells must read back as numbers when the file
    is opened with data_only=True (i.e. without recalculating formulas)."""
    deductors = [
        _deductor(1, "ALPHA LTD", "AAAA11111A", 1000, 100, 100,
                  [(600, 60, 60), (400, 40, 40)]),
        _deductor(2, "BETA LTD", "BBBB22222B", 500, 50, 50, [(500, 50, 50)]),
    ]
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Part I")
    m.build_part_i(ws, m.Assessee(name="X", pan="ABCDE1234F"), deductors)

    out = Path(tempfile.gettempdir()) / "test_26as_subtotals.xlsx"
    wb.save(out)

    ws2 = load_workbook(out, data_only=True)["Part I"]
    sub_vals, grand_vals = [], None
    for r in range(4, ws2.max_row + 1):
        label = ws2.cell(r, 2).value
        if isinstance(label, str) and label.startswith("Sub-total"):
            sub_vals.append((ws2.cell(r, 13).value,
                             ws2.cell(r, 14).value,
                             ws2.cell(r, 15).value))
        elif isinstance(label, str) and label.startswith("GRAND TOTAL"):
            grand_vals = (ws2.cell(r, 13).value,
                          ws2.cell(r, 14).value,
                          ws2.cell(r, 15).value)

    assert sub_vals == [(1000, 100, 100), (500, 50, 50)], sub_vals
    assert grand_vals == (1500, 150, 150), grand_vals
    # None of them may be blank or a leftover formula string.
    for trip in sub_vals + [grand_vals]:
        for v in trip:
            assert isinstance(v, (int, float)), f"sub-total cell not numeric: {v!r}"


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def test_reconcile_clean():
    """When every header total equals its transaction sum, no mismatches."""
    deductors = [
        _deductor(1, "ALPHA LTD", "AAAA11111A", 1000, 100, 100,
                  [(600, 60, 60), (400, 40, 40)]),
        _deductor(2, "BETA LTD", "BBBB22222B", 500, 50, 50, [(500, 50, 50)]),
    ]
    assert m.reconcile_part_i(deductors) == []


def test_reconcile_flags_mismatch():
    """A header total that disagrees with the transaction sum must be reported,
    field by field, with the signed difference."""
    bad = _deductor(1, "ACME LTD", "ABCD12345E", 100000, 10000, 10000,
                    [(45750, 4575, 4575)])
    good = _deductor(2, "GOOD CO", "WXYZ54321A", 45750, 4575, 4575,
                     [(45750, 4575, 4575)])
    mm = m.reconcile_part_i([bad, good])

    assert len(mm) == 1
    entry = mm[0]
    assert entry["sr"] == 1 and entry["name"] == "ACME LTD"
    fields = {f["field"]: f for f in entry["fields"]}
    assert set(fields) == {"Amount Paid/Credited", "Tax Deducted", "TDS Deposited"}
    assert fields["Amount Paid/Credited"]["header_total"] == 100000
    assert fields["Amount Paid/Credited"]["computed_subtotal"] == 45750
    assert fields["Amount Paid/Credited"]["difference"] == -54250


def test_reconcile_zero_txn_deductor_is_flagged():
    """A deductor whose transactions failed to parse (0 rows) must surface as a
    mismatch rather than silently producing a blank/broken sub-total."""
    empty = _deductor(1, "DROPPED ROWS LTD", "AAAA11111A", 9999, 999, 999, [])
    mm = m.reconcile_part_i([empty])
    assert len(mm) == 1
    assert all(f["computed_subtotal"] == 0 for f in mm[0]["fields"])


# ---------------------------------------------------------------------------
# Optional end-to-end (only if a fixture PDF is provided)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not FIXTURE.exists(),
                    reason="No fixture PDF at tests/fixtures/sample_26as.pdf")
def test_extract_end_to_end():
    out = Path(tempfile.gettempdir()) / "test_26as_e2e.xlsx"
    stats = m.run(FIXTURE, out)
    assert out.exists()
    assert stats["part_i_deductors"] >= 1
    assert "reconciliation" in stats
    # Sub-totals must be numeric in a data_only read.
    ws = load_workbook(out, data_only=True)["Part I"]
    for r in range(4, ws.max_row + 1):
        label = ws.cell(r, 2).value
        if isinstance(label, str) and label.startswith(("Sub-total", "GRAND TOTAL")):
            for c in (13, 14, 15):
                v = ws.cell(r, c).value
                assert v is None or isinstance(v, (int, float))
