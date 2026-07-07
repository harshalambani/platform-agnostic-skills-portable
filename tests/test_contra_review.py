"""
tests/test_contra_review.py — regression guards for contra (cross-bank
transfer) surfacing in the Review Mappings tab.

Background: contra entries are detected by the pipeline and persisted to a
``<csv-stem>.contra.json`` sidecar next to the import-ready CSV. The Review
Mappings loader must read that sidecar, merge the reason/confidence onto the
matching row (by 0-based index), and the rendered widget must carry the hooks
that make those rows visible (row highlight, leftmost badge, count button).

A prior release detected + saved contras correctly but they were effectively
invisible in the UI, so these tests lock in both the data merge and the
template hooks.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from ui.tabs import gnucash_review as gr_review  # noqa: E402


_HEADER = "Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n"
_ROWS = [
    "2025-05-01,COFFEE,Expense:Food,,100.00,900.00,high,rule\n",
    "2025-05-29,CHQ DEP BANK OF BARODA,Assets:Investments,300000.00,,300900.00,smart,prefix\n",
    "2025-06-01,SALARY,Income:Salary,5000.00,,305900.00,high,rule\n",
]


def _make_csv_with_sidecar(tmp_path: Path) -> Path:
    csv_p = tmp_path / "2026-01-01-TEST-GnuCash_import_ready.csv"
    csv_p.write_text(_HEADER + "".join(_ROWS), encoding="utf-8")
    # Flag row index 1 (the BOB cheque deposit) as a contra.
    sidecar = csv_p.with_suffix(".contra.json")
    sidecar.write_text(json.dumps({
        "1": {
            "row_idx": 1,
            "contra_account": "Assets:Current Assets:Cash and Bank:BOB - 7600",
            "contra_amount": -300000.0,
            "contra_date": "2025-05-30",
            "confidence": "medium",
            "reason": "Likely transfer from BOB - 7600 (2025-05-30)",
        }
    }), encoding="utf-8")
    return csv_p


def test_loader_merges_contra_reason_onto_flagged_row(tmp_path):
    csv_p = _make_csv_with_sidecar(tmp_path)
    # Pass the CSV as the gnucash arg to clear the file-exists guard; the
    # account-tree parse fails gracefully and falls back to row accounts.
    html = gr_review._load_review_data(str(csv_p), str(csv_p))
    # The reason for the flagged row must be embedded exactly once.
    assert html.count("Likely transfer from BOB - 7600") == 1


def test_loader_does_not_flag_unrelated_rows(tmp_path):
    csv_p = _make_csv_with_sidecar(tmp_path)
    html = gr_review._load_review_data(str(csv_p), str(csv_p))
    # Only one row is a contra — COFFEE / SALARY must not be flagged. The
    # merged empty-string sentinel means exactly one non-empty _contra reason.
    assert html.count("Likely transfer") == 1


def test_missing_sidecar_is_safe(tmp_path):
    csv_p = tmp_path / "2026-01-01-NOCONTRA-GnuCash_import_ready.csv"
    csv_p.write_text(_HEADER + _ROWS[0], encoding="utf-8")
    html = gr_review._load_review_data(str(csv_p), str(csv_p))
    # No sidecar → no reasons embedded, and the loader must not raise.
    assert "Likely transfer" not in html


def test_review_template_carries_contra_visibility_hooks():
    """The static template must keep the hooks that make contras visible:
    the count button, the row-highlight class, and the leftmost-cell badge
    branch (so the badge can't be clipped by the Reason column)."""
    tmpl = gr_review._REVIEW_HTML
    assert 'id="rv-show-contra"' in tmpl
    assert "contra-row" in tmpl
    assert "c.key === 'Date' && r._contra" in tmpl
    assert "contraCount" in tmpl
    # Confirmed vs possible distinction must be wired into the template.
    assert "_contra_status" in tmpl
    assert "contraLabel" in tmpl
    assert "TRANSFER" in tmpl and "POSSIBLE" in tmpl


def test_loader_derives_status_when_sidecar_omits_it(tmp_path):
    """Older sidecars carry only 'confidence'; the loader must derive the
    confirmed/possible status (medium -> possible)."""
    csv_p = _make_csv_with_sidecar(tmp_path)
    html = gr_review._load_review_data(str(csv_p), str(csv_p))
    assert '"_contra_status": "possible"' in html or "'_contra_status': 'possible'" in html
