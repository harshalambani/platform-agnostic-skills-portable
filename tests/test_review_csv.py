"""
tests/test_review_csv.py — regression guards for ui/_review_csv.py.

Background: skill_krc_gnucash's build_krc_gnucash.py can drop unprocessed
rows into a "Review.csv" in its output directory instead of only a terse
agent-reply line. ui/_review_csv.py turns that CSV into an inline,
colour-coded HTML table that ui/tabs/_generic.py splices into the run result
markdown. These tests cover the classifier, the CSV/file discovery, and HTML
escaping of untrusted row data (CN No / Security originate from parsed PDF
bill data).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from ui import _review_csv  # noqa: E402


# ---------------------------------------------------------------------------
# classify_reason() / hint_for_reason()
# ---------------------------------------------------------------------------

def test_classify_reason_account_mapping():
    reason = "no security account match (fuzzy) for 'RELIANCE INDUSTRIES LTD'"
    assert _review_csv.classify_reason(reason) == "account_mapping"


def test_classify_reason_data_value_no_net_amount():
    assert _review_csv.classify_reason("no net amount") == "data_value"


def test_classify_reason_data_value_bad_quantity():
    reason = (
        "sale quantity could not be read from the contract note for TCS "
        "(CN 12345) — cannot book a FIFO sale; re-run Reconcile or fix the "
        "Quantity in the Bills workbook"
    )
    assert _review_csv.classify_reason(reason) == "data_value"


def test_classify_reason_judgment_insufficient_lots():
    reason = "insufficient FIFO lots for sale of INFY (need 100.0, have 40.0)"
    assert _review_csv.classify_reason(reason) == "judgment"


def test_classify_reason_unknown_fallback():
    assert _review_csv.classify_reason("some brand new reason text") == "unknown"


def test_hint_for_reason_covers_every_kind():
    for kind in ("account_mapping", "data_value", "judgment", "unknown"):
        hint = _review_csv.hint_for_reason(kind)
        assert isinstance(hint, str) and hint.strip()


# ---------------------------------------------------------------------------
# find_review_csv()
# ---------------------------------------------------------------------------

def test_find_review_csv_found(tmp_path):
    target = tmp_path / "Review.csv"
    target.write_text("CN No,Type,Security,Net,Reason\n", encoding="utf-8")
    found = _review_csv.find_review_csv(tmp_path)
    assert found == target


def test_find_review_csv_case_insensitive(tmp_path):
    # Write with a differently-cased name than the canonical "Review.csv".
    weird = tmp_path / "REVIEW.csv"
    weird.write_text("CN No,Type,Security,Net,Reason\n", encoding="utf-8")
    found = _review_csv.find_review_csv(tmp_path)
    assert found == weird


def test_find_review_csv_absent(tmp_path):
    (tmp_path / "SomeOtherFile.csv").write_text("a,b\n", encoding="utf-8")
    assert _review_csv.find_review_csv(tmp_path) is None


def test_find_review_csv_not_a_directory(tmp_path):
    f = tmp_path / "not_a_dir.txt"
    f.write_text("x", encoding="utf-8")
    assert _review_csv.find_review_csv(f) is None


def test_find_review_csv_ignores_nested(tmp_path):
    nested = tmp_path / "subfolder"
    nested.mkdir()
    (nested / "Review.csv").write_text("CN No,Type,Security,Net,Reason\n", encoding="utf-8")
    assert _review_csv.find_review_csv(tmp_path) is None


# ---------------------------------------------------------------------------
# read_review_rows()
# ---------------------------------------------------------------------------

def _write_review_csv(path: Path, rows: list[list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["CN No", "Type", "Security", "Net", "Reason"])
        for row in rows:
            w.writerow(row)


def test_read_review_rows_roundtrip(tmp_path):
    csv_path = tmp_path / "Review.csv"
    _write_review_csv(
        csv_path,
        [["1001", "SELL", "INFY", "50000.00", "insufficient FIFO lots for sale of INFY (need 100.0, have 40.0)"]],
    )
    rows = _review_csv.read_review_rows(csv_path)
    assert len(rows) == 1
    assert rows[0]["CN No"] == "1001"
    assert rows[0]["Security"] == "INFY"


# ---------------------------------------------------------------------------
# render_review_table_html() — escaping + content
# ---------------------------------------------------------------------------

def test_render_review_table_html_empty_rows():
    assert _review_csv.render_review_table_html([]) == ""


def test_render_review_table_html_escapes_untrusted_fields():
    rows = [
        {
            "CN No": "1001",
            "Type": "SELL",
            "Security": "<script>alert('x')</script>",
            "Net": "1000",
            "Reason": "no security account match (exact) for '<img src=x onerror=alert(1)>'",
        }
    ]
    out = _review_csv.render_review_table_html(rows)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "onerror=" not in out or "&lt;img" in out
    assert "<table" in out


def test_render_review_table_html_row_count_and_kind_badges():
    rows = [
        {"CN No": "1", "Type": "SELL", "Security": "A", "Net": "1", "Reason": "no net amount"},
        {
            "CN No": "2",
            "Type": "SELL",
            "Security": "B",
            "Net": "2",
            "Reason": "no security account match (fuzzy) for 'B'",
        },
    ]
    out = _review_csv.render_review_table_html(rows)
    assert out.count("<tbody>") == 1
    # One header <tr> plus one per data row.
    assert out.count("<tr>") == len(rows) + 1
    assert "Data value" in out
    assert "Account mapping" in out


# ---------------------------------------------------------------------------
# render_review_section_html() — end-to-end
# ---------------------------------------------------------------------------

def test_render_review_section_html_empty_file_returns_nothing(tmp_path):
    csv_path = tmp_path / "Review.csv"
    _write_review_csv(csv_path, [])
    assert _review_csv.render_review_section_html(csv_path) == ""


def test_render_review_section_html_has_heading_link_and_table(tmp_path):
    csv_path = tmp_path / "Review.csv"
    _write_review_csv(
        csv_path,
        [["1001", "SELL", "INFY", "50000.00", "no net amount"]],
    )
    out = _review_csv.render_review_section_html(csv_path)
    assert "Needs review (1)" in out
    assert "Open Review.csv" in out
    assert "<table" in out
    assert str(csv_path.resolve().parent.name) in out
