"""
HSB-04 -- the HSBC 'BALANCE BROUGHT FORWARD' row carries the opening balance
(the extractors keep it on purpose) but must not reach the GnuCash CSV as a
zero-amount transaction. Detected structurally, dropped only after the balance
check has used it. Synthetic data only.
"""
from __future__ import annotations

import csv
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from agents import canonical_io as cio  # noqa: E402
from agents.balance_utils import extract_opening_closing, verify_running_balance  # noqa: E402
from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402

FIELDS = ["Date", "Transaction ID", "Description", "Account", "Deposit",
          "Withdrawal", "Balance", "Currency", "Confidence", "MatchReason"]


def _r(desc, dep="", wd="", bal="", date="2025-06-01"):
    return {"Date": date, "Transaction ID": "", "Description": desc, "Account": "",
            "Deposit": dep, "Withdrawal": wd, "Balance": bal, "Currency": "INR",
            "Confidence": "", "MatchReason": ""}


def _stmt():
    return [_r("BALANCE BROUGHT FORWARD", bal="1000.00"),
            _r("Xfer to self", wd="200.00", bal="800.00", date="2025-06-02"),
            _r("Salary credit", dep="500.00", bal="1300.00", date="2025-06-03")]


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_opening_carrier_row_is_dropped():
    kept, dropped = cio.split_balance_carriers(_stmt())
    assert dropped == [0]
    assert [r["Description"] for r in kept] == ["Xfer to self", "Salary credit"]


def test_detection_is_structural_not_by_wording():
    """The carrier is found without the word BALANCE in it."""
    rows = _stmt()
    rows[0]["Description"] = "OPENING FIGURE 2025"
    _kept, dropped = cio.split_balance_carriers(rows)
    assert dropped == [0]


def test_zero_amount_charge_reversal_mentioning_balance_is_kept():
    """NEGATIVE (a): a real zero-amount record mid-statement whose narration
    contains 'BALANCE' is not dropped."""
    rows = _stmt()
    rows.insert(2, _r("CHARGE REVERSAL LOW BALANCE FEE", dep="0.00", wd="0.00",
                      bal="800.00", date="2025-06-02"))
    kept, dropped = cio.split_balance_carriers(rows)
    assert dropped == [0]
    assert any("LOW BALANCE FEE" in r["Description"] for r in kept)


def test_first_row_with_money_movement_is_kept_even_if_text_says_balance():
    """NEGATIVE (c): a genuine money row containing 'BALANCE' is kept."""
    rows = [_r("BALANCE TRANSFER TO SAVINGS", wd="50.00", bal="950.00"),
            _r("Salary credit", dep="500.00", bal="1450.00", date="2025-06-03")]
    kept, dropped = cio.split_balance_carriers(rows)
    assert dropped == [] and len(kept) == 2
    rows2 = [_r("BALANCE BROUGHT FORWARD", dep="12.00", bal="1012.00")]
    assert cio.split_balance_carriers(rows2)[1] == []


def test_zero_amount_first_row_without_a_balance_is_kept():
    """NEGATIVE: nothing to carry, so nothing is dropped."""
    rows = [_r("NOTE ONLY", bal=""), _r("Salary credit", dep="5.00", bal="5.00")]
    assert cio.split_balance_carriers(rows)[1] == []


def test_split_does_not_mutate_input():
    rows = _stmt()
    snap = [dict(r) for r in rows]
    cio.split_balance_carriers(rows)
    assert rows == snap


def test_opening_check_still_gets_its_opening_figure():
    """NEGATIVE (b): the canonical rows keep the carrier, so the opening figure
    and the running balance check are unchanged; only the mapped output loses it."""
    rows = _stmt()
    assert extract_opening_closing(rows)["opening_balance"] == 1000.0
    assert verify_running_balance(rows)["ok"] is True
    cio.split_balance_carriers(rows)          # must not affect the canonical rows
    assert extract_opening_closing(rows)["opening_balance"] == 1000.0


def test_drop_runs_after_the_balance_and_opening_checks():
    """NEGATIVE (b), ordering: in the pipeline the drop is placed after the
    opening reconciliation, dedup and contra detection."""
    src = inspect.getsource(pipe)
    drop = src.index("n_carrier = _drop_balance_carriers(output_path")
    for earlier in ("verify_running_balance(canonical_rows)",
                    "_reconcile_opening_balance(\n            canonical_rows",
                    "detect_contra_entries(\n                    canonical_rows",
                    "mapper_run("):
        assert src.index(earlier) < drop, earlier


def test_mapped_output_loses_the_row_and_contra_flags_follow(tmp_path):
    out = tmp_path / "GnuCash_import_ready.csv"
    _write(out, _stmt())
    flags = {"1": {"status": "confirmed"}, "2": {"status": "possible"}, "0": {"status": "x"}}
    n = pipe._drop_balance_carriers(str(out), flags)
    assert n == 1
    rows = _read(out)
    assert [r["Description"] for r in rows] == ["Xfer to self", "Salary credit"]
    # flags re-pointed at the same transactions; the dropped row's flag is gone
    assert flags == {0: {"status": "confirmed"}, 1: {"status": "possible"}}
    assert rows[0]["Description"] == "Xfer to self"


def test_output_without_a_carrier_is_left_byte_identical(tmp_path):
    """NEGATIVE: nothing to drop -> file and flags untouched."""
    out = tmp_path / "x.csv"
    _write(out, _stmt()[1:])
    before = out.read_bytes()
    flags = {"0": {"status": "confirmed"}}
    assert pipe._drop_balance_carriers(str(out), flags) == 0
    assert out.read_bytes() == before and flags == {"0": {"status": "confirmed"}}


def test_extractor_still_keeps_the_carrier_row():
    """The HSBC extractor's behaviour (pinned in test_bank_skills) is not changed
    by this fix: the drop lives only in the pipeline, after the checks."""
    src = (ROOT / "tests" / "test_bank_skills.py").read_text(encoding="utf-8")
    assert 'res.rows[0]["Description"] == "BALANCE BROUGHT FORWARD"' in src
