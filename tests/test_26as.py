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
# Part VI — TCS
#
# Part VI used to be written as an always-empty sheet, so TCS never reached the
# workbook and the tax credit was silently lost downstream. These tests pin that
# it is now parsed and rendered with the same geometry as Part I (which is what
# lets the journal builder read either sheet by column index).
# ---------------------------------------------------------------------------

PART_VI_TEXT = """
                                                                Sr. No.   Name of Collector   TAN of Collector
     1   THOMAS COOK INDIA                     ABCD12345E          500,000.00      25,000.00      25,000.00
         LIMITED
         1   206CQ     15-Jun-2025   F   30-Jun-2025   -    300,000.00   15,000.00   15,000.00
         2   206CQ     20-Nov-2025   F   30-Nov-2025   -    200,000.00   10,000.00   10,000.00
"""


def test_parse_part_vi_reads_collectors_and_transactions():
    collectors = m.parse_part_vi(PART_VI_TEXT)
    assert len(collectors) == 1
    c = collectors[0]
    # The name wrapped onto a second line and must be re-joined.
    assert c.name == "THOMAS COOK INDIA LIMITED"
    assert c.tan == "ABCD12345E"
    assert (c.tot_amt, c.tot_tax, c.tot_tds) == (500000.0, 25000.0, 25000.0)
    assert [t.section for t in c.txns] == ["206CQ", "206CQ"]
    assert sum(t.tax for t in c.txns) == 25000.0


def test_part_vi_column_geometry_matches_part_i():
    """The journal builder reads Part I and Part VI by the SAME column indices;
    if the two ever diverge it would read the wrong figures silently."""
    assert len(m.P6_HEADERS) == len(m.P1_HEADERS) == 15


def test_build_part_vi_renders_numeric_subtotals():
    collectors = [_deductor(1, "THOMAS COOK INDIA LIMITED", "ABCD12345E",
                            500000, 25000, 25000,
                            [(300000, 15000, 15000), (200000, 10000, 10000)])]
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Part VI")
    m.build_part_vi(ws, m.Assessee(name="X", pan="ABCDE1234F"), collectors)
    out = Path(tempfile.gettempdir()) / "test_26as_part_vi.xlsx"
    wb.save(out)

    ws2 = load_workbook(out, data_only=True)["Part VI"]
    assert ws2.cell(3, 2).value == "Name of Collector"
    assert ws2.cell(3, 14).value == "Tax Collected ++"
    subs = [(ws2.cell(r, 13).value, ws2.cell(r, 14).value, ws2.cell(r, 15).value)
            for r in range(4, ws2.max_row + 1)
            if isinstance(ws2.cell(r, 2).value, str)
            and ws2.cell(r, 2).value.startswith("Sub-total")]
    assert subs == [(500000, 25000, 25000)], subs


def test_build_part_vi_empty_still_renders_headers():
    """No TCS in the year must produce the banner, not a crash or a blank sheet."""
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("Part VI")
    m.build_part_vi(ws, m.Assessee(name="X"), [])
    assert ws.cell(3, 1).value == "Collector Sr.No."
    assert ws.cell(4, 1).value == "No Transactions Present"


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
