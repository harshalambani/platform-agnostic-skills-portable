"""
tests/test_cc19_issue_noise.py -- CC-19: the issues list says what is true.
(a) the due-values line is not an unread row (see also test_cc16_sbm_older_layout);
(b) an all-zero statement with no rows is "no activity";
(c) a sub-rupee difference is judged by the tie-out tolerance, consistently.

Everything is synthetic.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_cc_transactions" / "scripts" / "create_cc_transaction_list.py"
for _p in (str(SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_module():
    spec = importlib.util.spec_from_file_location("create_cc_transaction_list", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load_module()

HEAD = ["Statement Period : 1-Sep-2025 to 30-Sep-2025",
        "Opening Balance Purchase/Debits Finance Charges Payment/Credit Total Amount Due"]


def _run(tmp_path, monkeypatch, lines, folder="SBM-Sample", name="s.pdf"):
    d = tmp_path / "in" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(b"%PDF-1.4 synthetic")
    monkeypatch.setattr(m, "read_lines", lambda p: lines)
    return m.run_extraction(tmp_path / "in", tmp_path / "out.xlsx")


def _st(lines):
    return m.parse_statement(lines, "SBM", "Sample", "s.pdf")


# --- (b) no activity --------------------------------------------------------

def test_all_zero_statement_with_no_rows_says_no_activity(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, HEAD + ["0.00 0.00 0.00 0.00 0.00"])
    assert any("no activity" in i for i in res.issues), res.issues
    assert not any("no transactions parsed" in i for i in res.issues)


def test_nonzero_printed_purchases_with_no_rows_is_still_an_issue_never_no_activity(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, HEAD + ["0.00 500.00 0.00 0.00 500.00"])
    assert any("no transactions parsed" in i for i in res.issues), res.issues
    assert not any("no activity" in i for i in res.issues)
    assert any("tie-out" in i for i in res.issues)


def test_nonzero_opening_balance_with_no_rows_is_not_no_activity(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, HEAD + ["300.00 0.00 0.00 0.00 300.00"])
    assert not any("no activity" in i for i in res.issues)
    assert any("no transactions parsed" in i for i in res.issues)


def test_missing_summary_with_no_rows_is_not_no_activity(tmp_path, monkeypatch):
    res = _run(tmp_path, monkeypatch, ["Statement Period : 1-Sep-2025 to 30-Sep-2025", "nothing"])
    assert not any("no activity" in i for i in res.issues)
    assert any("no transactions parsed" in i for i in res.issues)


# --- (c) sub-rupee differences ---------------------------------------------

def test_a_difference_of_one_paisa_is_within_tolerance_and_passes():
    st = _st(HEAD + ["0.00 0.00 0.00 0.00 0.00", "5-Sep-2025 EXAMPLE SHOP 0.01 -"])
    t = m.tie_out(st)
    assert abs(t.difference) <= m.TOL_STRICT
    assert t.result == "PASS"


def test_a_difference_above_tolerance_still_fails():
    st = _st(HEAD + ["0.00 0.00 0.00 0.00 0.00", "5-Sep-2025 EXAMPLE SHOP 0.02 -"])
    t = m.tie_out(st)
    assert t.result == "FAIL"
    assert t.difference == 0.02


def test_a_sub_rupee_difference_is_not_forgiven_on_a_strict_layout():
    st = _st(HEAD + ["0.00 0.00 0.00 0.00 0.00", "5-Sep-2025 EXAMPLE SHOP 0.50 -"])
    assert m.tie_out(st).result == "FAIL"


def test_unavailable_total_with_rows_netting_a_paisa_stays_not_available():
    st = _st(["Statement Period : 1-Sep-2025 to 30-Sep-2025", "5-Sep-2025 EXAMPLE SHOP 0.01 -"])
    t = m.tie_out(st)
    assert t.result.startswith("NOT AVAILABLE")
