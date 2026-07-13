"""
tests/skill_itr_workbook/test_parse_eguile.py -- tests for the eguile
Balance Sheet HTML parser (plan section 8). Fully offline; synthetic
fixtures only (see fixture_gen.py). No real family data is read here except
in the local_samples smoke test, which is skipped when Data/GNUCashReports/
is absent so CI never touches real data.

Run with:
    cd src && python -m pytest ../tests/skill_itr_workbook -v
"""
from __future__ import annotations

import glob
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_itr_workbook" / "scripts" / "parse_eguile.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_SAMPLES_DIR = ROOT / "Data" / "GNUCashReports"


def _load_module():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("parse_eguile", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pe = _load_module()

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixture_gen  # noqa: E402


def test_script_exists():
    assert SCRIPT.exists(), f"Script not found: {SCRIPT}"


# ---------------------------------------------------------------------------
# Golden parse of the two synthetic entities.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("build_fn, golden_name", [
    (fixture_gen.build_syn_ind_html, "syn_ind_expected.json"),
    (fixture_gen.build_syn_huf_html, "syn_huf_expected.json"),
])
def test_golden_parse(build_fn, golden_name):
    html_text = build_fn()
    tree = pe.parse_html(html_text)
    expected = json.loads((FIXTURES / golden_name).read_text(encoding="utf-8"))
    assert tree.to_dict() == expected


# ---------------------------------------------------------------------------
# Identity checks (plan section 1.1).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("build_fn", [
    fixture_gen.build_syn_ind_html,
    fixture_gen.build_syn_huf_html,
])
def test_imbalance_is_zero(build_fn):
    tree = pe.parse_html(build_fn())
    assert tree.imbalance == 0.0


@pytest.mark.parametrize("build_fn", [
    fixture_gen.build_syn_ind_html,
    fixture_gen.build_syn_huf_html,
])
def test_assets_equals_equity_trading_liabilities_plus_retained_earnings(build_fn):
    tree = pe.parse_html(build_fn())
    total_assets = tree.section_totals["Assets Accounts"]
    etl = tree.section_totals["Equity, Trading, and Liabilities"]
    total_re = tree.section_totals["Retained Earnings"]
    assert abs(total_assets - (etl + total_re)) <= 0.01


@pytest.mark.parametrize("build_fn", [
    fixture_gen.build_syn_ind_html,
    fixture_gen.build_syn_huf_html,
])
def test_every_total_equals_sum_of_children(build_fn):
    tree = pe.parse_html(build_fn())
    for node in tree.all_nodes():
        if not node.children:
            continue
        assert abs(node.total - node.child_sum()) <= 0.01, node.path


@pytest.mark.parametrize("build_fn", [
    fixture_gen.build_syn_ind_html,
    fixture_gen.build_syn_huf_html,
])
def test_verify_returns_no_failures(build_fn):
    tree = pe.parse_html(build_fn())
    assert pe.verify(tree) == []


# ---------------------------------------------------------------------------
# Security-cell (qty/symbol/value triple) parsing.
# ---------------------------------------------------------------------------

def test_security_cell_parsed_into_qty_symbol_value():
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    security_nodes = [n for n in tree.all_nodes() if n.symbol is not None]
    assert len(security_nodes) == 1
    node = security_nodes[0]
    assert node.symbol == "SYNCORP.NS"
    assert node.qty == 1000.0
    assert node.total == 150000.00


# ---------------------------------------------------------------------------
# Adversarial fixtures.
# ---------------------------------------------------------------------------

def test_truncated_html_hard_fails_with_exact_message():
    with pytest.raises(ValueError) as excinfo:
        pe.parse_html(fixture_gen.build_syn_ind_html(truncated=True))
    assert str(excinfo.value) == pe.TRUNCATED_MESSAGE == "file truncated — re-export"


def test_nonzero_imbalance_fails_validation():
    tree = pe.parse_html(fixture_gen.build_syn_ind_html(nonzero_imbalance=True))
    assert tree.imbalance != 0.0
    failures = pe.verify(tree)
    assert any("Imbalance" in f for f in failures)
    with pytest.raises(ValueError):
        pe.assert_valid(tree)


def test_missing_accounts_table_hard_fails():
    with pytest.raises(ValueError) as excinfo:
        pe.parse_html("<html><body>not a report</body></html>")
    assert str(excinfo.value) == pe.TRUNCATED_MESSAGE


# ---------------------------------------------------------------------------
# Amount parsing helper -- lakh grouping, negatives, blanks.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("₹\xa0\xa01,58,156.35", 158156.35),
    ("₹\xa0\xa0-4,99,919.22", -499919.22),
    ("\xa0", None),
    (None, None),
    ("₹\xa0\xa00.00", 0.0),
])
def test_parse_amount(raw, expected):
    assert pe.parse_amount(raw) == expected


# ---------------------------------------------------------------------------
# Real-file smoke test -- never runs in CI (skipped when the folder is absent).
# ---------------------------------------------------------------------------

@pytest.mark.local_samples
def test_real_samples_parse_and_verify_clean():
    if not REAL_SAMPLES_DIR.is_dir():
        pytest.skip("Data/GNUCashReports/ not present -- real-file smoke test skipped")
    files = sorted(glob.glob(str(REAL_SAMPLES_DIR / "*.html")))
    assert files, "expected real sample HTML files"
    for f in files:
        tree = pe.parse_file(f)
        failures = pe.verify(tree)
        assert failures == [], f"{f}: {failures}"
