"""
tests/skill_icici/test_icici_run_sidecar.py -- IMP-06: the .csv_summary.json
sidecar run() writes for the GnuCash pipeline's balance verification must be
built from the consolidated (date-ordered) output, not from whichever file
happened to be processed last in filename order.

Before the fix, run() grabbed the LAST successful per-file result dict out
of the processing loop -- which, for a batch, is the last file in
sorted(glob) filename order, not the last file by transaction date. Whenever
filenames don't happen to sort chronologically (the same class of bug fix
#91/#92 already closed for row ordering), the sidecar's opening/closing
balances silently came from the wrong statement.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import icici_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_icici.agent import run as icici_run  # noqa: E402

# Jan statement: single txn, deposit 50000 -> balance 150000.
# opening = balance - deposit + withdrawal = 150000 - 50000 + 0 = 100000
_JAN_TXNS = [
    ("15,Jan,2025", "15,Jan,2025", "-",
     "NEFT-REF0000000001-SYN SALARY CREDIT", "", "50000.00", "150000.00"),
]
_JAN_OPENING = 100000.0
_JAN_CLOSING = 150000.0

# Mar statement: single txn, withdrawal 5000 -> balance 143000 (closing).
_MAR_TXNS = [
    ("05,Mar,2025", "05,Mar,2025", "-",
     "BIL/ONL/REF0000000003/SYN UTILITY BILL", "5000.00", "", "143000.00"),
]
_MAR_CLOSING = 143000.0

# Feb statement, used for the 3-file variant: withdrawal 2000 -> 148000.
_FEB_TXNS = [
    ("10,Feb,2025", "10,Feb,2025", "-",
     "UPI/300000000002/NA/synshop//SYNabc0000ef", "2000.00", "", "148000.00"),
]


def _sidecar_json(csv_path: Path) -> dict:
    sidecar = csv_path.with_suffix(".csv_summary.json")
    assert sidecar.is_file(), f"sidecar not written: {sidecar}"
    with open(sidecar, "r", encoding="utf-8") as f:
        return json.load(f)


def test_sidecar_uses_date_order_not_filename_order_two_files(tmp_path):
    # Filename order is Mar-then-Jan (alphabetical: "a_mar" < "z_jan"),
    # opposite of date order (Jan is earlier than Mar). The old
    # last-in-filename-order code would grab the Jan file's OWN closing
    # balance (150000) as the batch closing balance -- wrong, since Mar is
    # the later statement and its closing (143000) is the true batch close.
    (tmp_path / "stmt_a_mar.xls").write_bytes(
        fixture_gen.build_xls_for(_MAR_TXNS, "01,Mar,2025", "31,Mar,2025"))
    (tmp_path / "stmt_z_jan.xls").write_bytes(
        fixture_gen.build_xls_for(_JAN_TXNS, "01,Jan,2025", "31,Jan,2025"))

    out_csv = tmp_path / "out.csv"
    icici_run(str(tmp_path), str(out_csv))

    sidecar = _sidecar_json(out_csv)

    assert sidecar["opening_balance"] == _JAN_OPENING
    assert sidecar["closing_balance"] == _MAR_CLOSING
    # The bug this guards against: closing balance must NOT be the
    # last-processed-by-filename file's own closing balance.
    assert sidecar["closing_balance"] != _JAN_CLOSING
    assert sidecar["row_count"] == 2


def test_sidecar_uses_date_order_not_filename_order_three_files(tmp_path):
    # Filename order z(jan), a(mar), m(feb) -- date order is jan, feb, mar.
    (tmp_path / "stmt_z_jan.xls").write_bytes(
        fixture_gen.build_xls_for(_JAN_TXNS, "01,Jan,2025", "31,Jan,2025"))
    (tmp_path / "stmt_a_mar.xls").write_bytes(
        fixture_gen.build_xls_for(_MAR_TXNS, "01,Mar,2025", "31,Mar,2025"))
    (tmp_path / "stmt_m_feb.xls").write_bytes(
        fixture_gen.build_xls_for(_FEB_TXNS, "01,Feb,2025", "28,Feb,2025"))

    out_csv = tmp_path / "out.csv"
    icici_run(str(tmp_path), str(out_csv))

    sidecar = _sidecar_json(out_csv)

    assert sidecar["opening_balance"] == _JAN_OPENING
    assert sidecar["closing_balance"] == _MAR_CLOSING
    assert sidecar["row_count"] == 3


def test_sidecar_single_file_case_unchanged(tmp_path):
    xls_path = tmp_path / "syn_icici.xls"
    xls_path.write_bytes(fixture_gen.build_xls())
    out_csv = tmp_path / "out.csv"

    icici_run(str(xls_path), str(out_csv))

    sidecar = _sidecar_json(out_csv)

    assert sidecar["opening_balance"] == fixture_gen.SYN_OPENING_BALANCE
    assert sidecar["closing_balance"] == fixture_gen.SYN_CLOSING_BALANCE
    assert sidecar["row_count"] == 5
    assert "warnings" not in sidecar


def test_sidecar_records_gap_warning_when_present(tmp_path):
    (tmp_path / "stmt_jan.xls").write_bytes(
        fixture_gen.build_xls_for(_JAN_TXNS, "01,Jan,2025", "31,Jan,2025"))
    (tmp_path / "stmt_mar.xls").write_bytes(
        fixture_gen.build_xls_for(_MAR_TXNS, "01,Mar,2025", "31,Mar,2025"))

    out_csv = tmp_path / "out.csv"
    icici_run(str(tmp_path), str(out_csv))

    sidecar = _sidecar_json(out_csv)

    assert "warnings" in sidecar
    assert any("POSSIBLE MISSING STATEMENT" in w for w in sidecar["warnings"])
