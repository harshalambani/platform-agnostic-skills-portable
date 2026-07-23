"""
tests/test_tds_journal_review.py -- regression guards for the Journal Review
tab's save logic (ui/tabs/tds_journal_review.py).

Covers the module-level functions directly (Gradio-free), per the module's
own design goal: fy_prefix parsing off the journal CSV's Transaction IDs
(never re-derived), the primary Account-match path for locating a credit
split, the negative-Amount fallback when the primary match finds nothing,
the ambiguous case being skipped rather than guessed, a full round-trip
save that rewrites both CSVs, and balance re-verification.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from ui.tabs import tds_journal_review as tjr  # noqa: E402


# ---------------------------------------------------------------------------
# fy_prefix parsing
# ---------------------------------------------------------------------------

def test_fy_prefix_from_parses_prefix_before_last_dash():
    journal_rows = [
        {"Transaction ID": "2526-TDSJ01"},
        {"Transaction ID": "2526-TDSJ01"},
    ]
    assert tjr._fy_prefix_from(journal_rows) == "2526"


def test_fy_prefix_from_handles_tcs_kind():
    journal_rows = [{"Transaction ID": "2425-TCSJ03"}]
    assert tjr._fy_prefix_from(journal_rows) == "2425"


def test_fy_prefix_from_empty_when_no_rows():
    assert tjr._fy_prefix_from([]) == ""


def test_txn_id_for_tds_vs_tcs():
    assert tjr._txn_id_for("2526", "1", "A") == "2526-TDSJ01"
    assert tjr._txn_id_for("2526", "3", "T") == "2526-TCSJ03"


def test_txn_id_for_category_g_uses_15gj_series():
    # Category G (Part II / 15G-15H) must never fall through to TDSJ -- that
    # is the exact defect this module's single-source-of-truth import fixes.
    assert tjr._txn_id_for("2526", "1", "G") == "2526-15GJ01"


def test_txn_id_for_unknown_category_raises_not_guesses():
    # Regression: an unrecognised category must never silently resolve to
    # "TDSJ" (the resurrected defect) -- it must fail loud instead.
    import pytest
    with pytest.raises(ValueError):
        tjr._txn_id_for("2526", "1", "Z")


# ---------------------------------------------------------------------------
# _find_credit_split -- primary match, negative-amount fallback, ambiguous
# ---------------------------------------------------------------------------

def _interest_txn(txn_id="2526-TDSJ01"):
    """An Interest journal: Dr TDS = a, Dr Interest Income = c-a, Cr matched = c."""
    return [
        {"Transaction ID": txn_id, "Account": "Expense:TDS on Interest", "Amount": "10.00"},
        {"Transaction ID": txn_id, "Account": "Income:Interest Income:Interest on FD", "Amount": "90.00"},
        {"Transaction ID": txn_id, "Account": "Assets:Bank:HDFC", "Amount": "-100.00"},
    ]


def test_find_credit_split_primary_match_by_account():
    rows = _interest_txn()
    idx, err = tjr._find_credit_split(rows, "2526-TDSJ01", "Assets:Bank:HDFC")
    assert err is None
    assert rows[idx]["Account"] == "Assets:Bank:HDFC"


def test_find_credit_split_negative_amount_fallback():
    # old_account no longer matches anything in the transaction (e.g. it was
    # already renamed out-of-band) -- falls back to the unique negative split.
    rows = _interest_txn()
    idx, err = tjr._find_credit_split(rows, "2526-TDSJ01", "Liabilities:Suspense")
    assert err is None
    assert rows[idx]["Amount"] == "-100.00"


def test_find_credit_split_ambiguous_negative_is_skipped_not_guessed():
    # Pathological data: a > c produces a SECOND negative split. Neither the
    # primary match nor the fallback resolves to exactly one row, so the
    # change must be skipped and reported, never guessed.
    rows = [
        {"Transaction ID": "2526-TDSJ02", "Account": "Expense:TDS on Interest", "Amount": "-10.00"},
        {"Transaction ID": "2526-TDSJ02", "Account": "Assets:Bank:HDFC", "Amount": "-90.00"},
        {"Transaction ID": "2526-TDSJ02", "Account": "Income:Interest Income:Interest on FD", "Amount": "100.00"},
    ]
    idx, err = tjr._find_credit_split(rows, "2526-TDSJ02", "Liabilities:Suspense")
    assert idx is None
    assert err is not None
    assert "ambiguous" in err


def test_find_credit_split_no_matching_transaction():
    rows = _interest_txn()
    idx, err = tjr._find_credit_split(rows, "2526-TDSJ99", "Assets:Bank:HDFC")
    assert idx is None
    assert "no splits found" in err


# ---------------------------------------------------------------------------
# _verify_balanced
# ---------------------------------------------------------------------------

def test_verify_balanced_passes_for_balanced_journal():
    rows = _interest_txn()
    assert tjr._verify_balanced(rows) == []


def test_verify_balanced_flags_unbalanced_transaction():
    rows = [
        {"Transaction ID": "2526-TDSJ01", "Amount": "10.00"},
        {"Transaction ID": "2526-TDSJ01", "Amount": "-5.00"},
    ]
    problems = tjr._verify_balanced(rows)
    assert len(problems) == 1
    assert "2526-TDSJ01" in problems[0]


def test_verify_balanced_tolerates_rounding_within_tolerance():
    rows = [
        {"Transaction ID": "2526-TDSJ01", "Amount": "10.004"},
        {"Transaction ID": "2526-TDSJ01", "Amount": "-10.00"},
    ]
    assert tjr._verify_balanced(rows) == []


# ---------------------------------------------------------------------------
# _row_presentation -- row accent/tag computation
# ---------------------------------------------------------------------------

def test_row_presentation_unbalanced_gets_its_own_accent_not_plain():
    # Regression: a row that IS unbalanced but is neither needs_review nor
    # suspense nor missing_account must not fall through to an empty
    # rowclass -- it's the loudest failure state and must never render plain.
    row = {
        "Needs Review": "", "Credit Account": "Income:Interest:ACME FD",
        "Account Exists": "yes", "Balanced": "NO",
    }
    tjr._row_presentation(row)
    assert row["_rowclass"] != ""
    assert "unbalanced" in row["_tags"]


def test_row_presentation_unbalanced_ranks_above_missing_account():
    row = {
        "Needs Review": "", "Credit Account": "Income:Interest:ACME FD",
        "Account Exists": "NO", "Balanced": "NO",
    }
    tjr._row_presentation(row)
    # Both tags are present, but the rowclass reflects the more severe one.
    assert "unbalanced" in row["_tags"] and "missing_account" in row["_tags"]
    assert row["_rowclass"] == "accent-red"


def test_row_presentation_clean_matched_row_is_green():
    row = {
        "Needs Review": "", "Credit Account": "Income:Interest:ACME FD",
        "Account Exists": "yes", "Balanced": "yes",
    }
    tjr._row_presentation(row)
    assert row["_rowclass"] == "accent-green"
    assert row["_tags"] == ["matched"]


# ---------------------------------------------------------------------------
# _apply_changes -- in-memory row mutation
# ---------------------------------------------------------------------------

def _review_row(sr="1", category="A", credit_account="Liabilities:Suspense",
                needs_review="yes"):
    return {
        "Sr": sr, "Deductor": "ACME BANK", "Section": "194A", "Category": category,
        "Credit Account": credit_account, "Confidence": "Suspense",
        "Account Exists": "yes", "Balanced": "yes", "Debit": "100.00",
        "Credit": "100.00", "Needs Review": needs_review,
        "Basis": "no confident match",
    }


def test_apply_changes_reassigns_both_row_sets():
    review_rows = [_review_row()]
    journal_rows = [
        {"Transaction ID": "2526-TDSJ01", "Account": "Expense:TDS on Interest", "Amount": "10.00"},
        {"Transaction ID": "2526-TDSJ01", "Account": "Income:Interest Income:Interest on FD", "Amount": "90.00"},
        {"Transaction ID": "2526-TDSJ01", "Account": "Liabilities:Suspense", "Amount": "-100.00"},
    ]
    changes = [{
        "Sr": "1", "Category": "A", "Credit Account": "Income:Interest:ACME FD",
        "_orig": "Liabilities:Suspense",
    }]

    review_rows, journal_rows, problems, applied = tjr._apply_changes(review_rows, journal_rows, changes)

    assert problems == []
    assert applied == 1
    assert review_rows[0]["Credit Account"] == "Income:Interest:ACME FD"
    assert review_rows[0]["Confidence"] == "override"
    assert journal_rows[2]["Account"] == "Income:Interest:ACME FD"
    # The other two splits are untouched.
    assert journal_rows[0]["Account"] == "Expense:TDS on Interest"
    assert journal_rows[1]["Account"] == "Income:Interest Income:Interest on FD"


def test_apply_changes_reports_ambiguous_without_mutating():
    review_rows = [_review_row(sr="2")]
    journal_rows = [
        {"Transaction ID": "2526-TDSJ02", "Account": "Expense:TDS on Interest", "Amount": "-10.00"},
        {"Transaction ID": "2526-TDSJ02", "Account": "Liabilities:Suspense", "Amount": "-90.00"},
        {"Transaction ID": "2526-TDSJ02", "Account": "Income:Interest Income:Interest on FD", "Amount": "100.00"},
    ]
    changes = [{
        "Sr": "2", "Category": "A", "Credit Account": "Income:Interest:ACME FD",
        "_orig": "Nothing:Matches:This",
    }]

    review_rows, journal_rows, problems, applied = tjr._apply_changes(review_rows, journal_rows, changes)

    assert applied == 0
    assert len(problems) == 1
    assert "ambiguous" in problems[0]
    # Nothing was mutated -- neither the review row nor either negative split.
    assert review_rows[0]["Credit Account"] == "Liabilities:Suspense"
    assert journal_rows[0]["Account"] == "Expense:TDS on Interest"
    assert journal_rows[1]["Account"] == "Liabilities:Suspense"


def test_apply_changes_tcs_uses_separate_id_series():
    # Part VI collectors restart Sr numbering, so the same Sr with Category
    # "T" must resolve against the TCSJ id series, not TDSJ.
    review_rows = [_review_row(sr="1", category="T")]
    journal_rows = [
        {"Transaction ID": "2526-TDSJ01", "Account": "Assets:Bank:HDFC", "Amount": "-100.00"},
        {"Transaction ID": "2526-TCSJ01", "Account": "Expense:TCS on Foreign Trip", "Amount": "50.00"},
        {"Transaction ID": "2526-TCSJ01", "Account": "Liabilities:Suspense", "Amount": "-50.00"},
    ]
    changes = [{
        "Sr": "1", "Category": "T", "Credit Account": "Expense:Drawings",
        "_orig": "Liabilities:Suspense",
    }]

    review_rows, journal_rows, problems, applied = tjr._apply_changes(review_rows, journal_rows, changes)

    assert problems == []
    assert applied == 1
    # The TDSJ01 transaction (a different party) is untouched.
    assert journal_rows[0]["Account"] == "Assets:Bank:HDFC"
    assert journal_rows[2]["Account"] == "Expense:Drawings"


def test_apply_changes_category_g_uses_15gj_id_series():
    """Regression: this is the exact defect the coordinator flagged -- Category
    G rows get 15GJ ids in the journal CSV, but if _txn_id_for doesn't know
    about that series it falls through to TDSJ, builds an id that doesn't
    exist in journal_rows, _find_credit_split reports "no splits found", and
    the row is silently skipped on save. Assert it actually round-trips."""
    review_rows = [_review_row(sr="1", category="G")]
    journal_rows = [
        {"Transaction ID": "2526-TDSJ01", "Account": "Assets:Bank:HDFC", "Amount": "-100.00"},
        {"Transaction ID": "2526-15GJ01", "Account": "Income:Interest Income:Interest on FD",
         "Amount": "50000.00"},
        {"Transaction ID": "2526-15GJ01", "Account": "Liabilities:Suspense",
         "Amount": "-50000.00"},
    ]
    changes = [{
        "Sr": "1", "Category": "G", "Credit Account": "Income:Interest Income:Interest on BOB - FD",
        "_orig": "Liabilities:Suspense",
    }]

    review_rows, journal_rows, problems, applied = tjr._apply_changes(review_rows, journal_rows, changes)

    assert problems == [], "Category G row must not be skipped as 'no splits found'"
    assert applied == 1
    assert review_rows[0]["Credit Account"] == "Income:Interest Income:Interest on BOB - FD"
    # The other party's TDSJ01 transaction is untouched.
    assert journal_rows[0]["Account"] == "Assets:Bank:HDFC"
    assert journal_rows[2]["Account"] == "Income:Interest Income:Interest on BOB - FD"


def test_apply_changes_account_exists_reflects_known_accounts_not_journal_rows():
    # Regression: Account Exists must be checked against the REAL account
    # tree (known_accounts), not against the journal CSV's own accounts --
    # checking the latter is tautological, since the new account was just
    # written into that same journal_rows list.
    review_rows = [_review_row(sr="1")]
    journal_rows = [
        {"Transaction ID": "2526-TDSJ01", "Account": "Expense:TDS on Interest", "Amount": "10.00"},
        {"Transaction ID": "2526-TDSJ01", "Account": "Income:Interest Income:Interest on FD", "Amount": "90.00"},
        {"Transaction ID": "2526-TDSJ01", "Account": "Liabilities:Suspense", "Amount": "-100.00"},
    ]
    known_accounts = {"Income:Interest:ACME FD", "Assets:Bank:HDFC"}

    # A known account -> "yes".
    r1, j1, problems1, applied1 = tjr._apply_changes(
        [dict(r) for r in review_rows], [dict(r) for r in journal_rows],
        [{"Sr": "1", "Category": "A", "Credit Account": "Income:Interest:ACME FD",
          "_orig": "Liabilities:Suspense"}],
        known_accounts=known_accounts,
    )
    assert problems1 == [] and applied1 == 1
    assert r1[0]["Account Exists"] == "yes"

    # An account NOT in the book -> "NO", not a tautological "yes".
    r2, j2, problems2, applied2 = tjr._apply_changes(
        [dict(r) for r in review_rows], [dict(r) for r in journal_rows],
        [{"Sr": "1", "Category": "A", "Credit Account": "Income:Interest:Nonexistent",
          "_orig": "Liabilities:Suspense"}],
        known_accounts=known_accounts,
    )
    assert problems2 == [] and applied2 == 1
    assert r2[0]["Account Exists"] == "NO"

    # No book loaded (known_accounts=None) -> column left untouched.
    original_value = review_rows[0]["Account Exists"]
    r3, j3, problems3, applied3 = tjr._apply_changes(
        [dict(r) for r in review_rows], [dict(r) for r in journal_rows],
        [{"Sr": "1", "Category": "A", "Credit Account": "Income:Interest:ACME FD",
          "_orig": "Liabilities:Suspense"}],
        known_accounts=None,
    )
    assert problems3 == [] and applied3 == 1
    assert r3[0]["Account Exists"] == original_value


def test_apply_changes_blank_credit_account_reports_zero_applied():
    review_rows = [_review_row(sr="1")]
    journal_rows = [
        {"Transaction ID": "2526-TDSJ01", "Account": "Expense:TDS on Interest", "Amount": "10.00"},
        {"Transaction ID": "2526-TDSJ01", "Account": "Income:Interest Income:Interest on FD", "Amount": "90.00"},
        {"Transaction ID": "2526-TDSJ01", "Account": "Liabilities:Suspense", "Amount": "-100.00"},
    ]
    changes = [{"Sr": "1", "Category": "A", "Credit Account": "", "_orig": "Liabilities:Suspense"}]

    review_rows, journal_rows, problems, applied = tjr._apply_changes(
        review_rows, journal_rows, changes,
    )

    assert applied == 0
    assert len(problems) == 1
    assert "blank" in problems[0].lower()
    # Nothing was mutated.
    assert review_rows[0]["Credit Account"] == "Liabilities:Suspense"


# ---------------------------------------------------------------------------
# Full save round-trip through real temp CSVs
# ---------------------------------------------------------------------------

def _write_csv(path: Path, headers: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)


def test_save_changes_rewrites_both_csvs_and_verifies_balance(tmp_path):
    review_p = tmp_path / "2026-FY2526-tds-journals-review.csv"
    journal_p = tmp_path / "2026-FY2526-tds-journals.csv"

    _write_csv(review_p, tjr._REVIEW_HEADERS, [_review_row()])
    _write_csv(journal_p, tjr._JOURNAL_HEADERS, [
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "TDS FY 2025-26 - ACME BANK (Sec 194A)",
         "Account": "Expense:TDS on Interest", "Amount": "10.00", "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "TDS FY 2025-26 - ACME BANK (Sec 194A)",
         "Account": "Income:Interest Income:Interest on FD", "Amount": "90.00", "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "TDS FY 2025-26 - ACME BANK (Sec 194A)",
         "Account": "Liabilities:Suspense", "Amount": "-100.00", "Currency": "INR"},
    ])

    import json
    payload = json.dumps({
        "context": {"review_path": str(review_p)},
        "changes": [{
            "Sr": "1", "Category": "A", "Credit Account": "Income:Interest:ACME FD",
            "_orig": "Liabilities:Suspense",
        }],
        "all_rows": [],
    })

    status, download_update, part_i_download_update = tjr._save_changes(payload)

    assert "Applied 1 of 1 change" in status
    assert "all transactions sum to 0.00" in status
    # No 15GJ rows in this fixture -- the Part I split has nothing to do.
    assert "nothing to exclude" in status

    saved_review = list(csv.DictReader(open(review_p, encoding="utf-8")))
    saved_journal = list(csv.DictReader(open(journal_p, encoding="utf-8")))

    assert saved_review[0]["Credit Account"] == "Income:Interest:ACME FD"
    assert saved_review[0]["Confidence"] == "override"
    accounts = [r["Account"] for r in saved_journal]
    assert "Income:Interest:ACME FD" in accounts
    assert "Liabilities:Suspense" not in accounts

    # Balance still holds after rewrite.
    assert tjr._verify_balanced(saved_journal) == []


def test_save_changes_no_changes_is_a_noop():
    status, download_update, part_i_download_update = tjr._save_changes("")
    assert "No changes to save" in status


# ---------------------------------------------------------------------------
# Part I split regeneration on Save -- the core of this feature: a
# hand-filtered copy must be structurally impossible because Save always
# rewrites it from the journal rows just written, not from whatever was on
# disk before.
# ---------------------------------------------------------------------------

def test_save_changes_regenerates_part_i_split_reflecting_the_edit(tmp_path):
    review_p = tmp_path / "2026-FY2526-tds-journals-review.csv"
    journal_p = tmp_path / "2026-FY2526-tds-journals.csv"
    part_i_p = tmp_path / "2026-FY2526-tds-journals-partI.csv"

    _write_csv(review_p, tjr._REVIEW_HEADERS, [
        _review_row(sr="1", category="A", credit_account="Liabilities:Suspense"),
        _review_row(sr="1", category="G", credit_account="Liabilities:Suspense"),
    ])
    _write_csv(journal_p, tjr._JOURNAL_HEADERS, [
        # Part I -- Category A, the one we'll reassign.
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "TDS FY 2025-26 - ACME BANK (Sec 194A)",
         "Account": "Expense:TDS on Interest", "Amount": "10.00", "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "TDS FY 2025-26 - ACME BANK (Sec 194A)",
         "Account": "Income:Interest Income:Interest on FD", "Amount": "90.00", "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "TDS FY 2025-26 - ACME BANK (Sec 194A)",
         "Account": "Liabilities:Suspense", "Amount": "-100.00", "Currency": "INR"},
        # Part II -- Category G (15G/15H), must be excluded from the split.
        {"Date": "2026-03-31", "Transaction ID": "2526-15GJ01", "Number": "2526-15GJ01",
         "Description": "15G/15H TDS FY 2025-26 - BAJAJ FINANCE (Sec 194A)",
         "Account": "Income:Interest Income:Interest on FD", "Amount": "50000.00", "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-15GJ01", "Number": "2526-15GJ01",
         "Description": "15G/15H TDS FY 2025-26 - BAJAJ FINANCE (Sec 194A)",
         "Account": "Liabilities:Suspense", "Amount": "-50000.00", "Currency": "INR"},
    ])

    import json
    payload = json.dumps({
        "context": {"review_path": str(review_p)},
        "changes": [{
            "Sr": "1", "Category": "A", "Credit Account": "Income:Interest:ACME FD",
            "_orig": "Liabilities:Suspense",
        }],
        "all_rows": [],
    })

    status, download_update, part_i_download_update = tjr._save_changes(payload)

    assert "1 transaction(s) excluded" in status
    assert part_i_p.name in status
    assert part_i_p.exists(), "Save must write the Part I split when a 15GJ row exists"

    part_i_rows = list(csv.DictReader(open(part_i_p, encoding="utf-8")))
    txn_ids = {r["Transaction ID"] for r in part_i_rows}
    assert txn_ids == {"2526-TDSJ01"}, "the 15GJ transaction must not appear in the split"

    # The edit made on this screen (reassigning the credit account) must be
    # reflected in the regenerated split -- proving it isn't a stale copy.
    accounts_in_split = {r["Account"] for r in part_i_rows}
    assert "Income:Interest:ACME FD" in accounts_in_split
    assert "Liabilities:Suspense" not in accounts_in_split

    # The split itself still balances.
    assert tjr._verify_balanced(part_i_rows) == []

    # The download button for the Part I file must be interactive now that a
    # Part II row existed in this run.
    assert part_i_download_update["interactive"] is True
    assert part_i_download_update["value"] is not None


def test_save_changes_no_part_ii_rows_leaves_part_i_button_disabled(tmp_path):
    review_p = tmp_path / "2026-FY2526-tds-journals-review.csv"
    journal_p = tmp_path / "2026-FY2526-tds-journals.csv"

    _write_csv(review_p, tjr._REVIEW_HEADERS, [_review_row()])
    _write_csv(journal_p, tjr._JOURNAL_HEADERS, [
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "x", "Account": "Expense:TDS on Interest", "Amount": "10.00",
         "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "x", "Account": "Income:Interest Income:Interest on FD",
         "Amount": "90.00", "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "x", "Account": "Liabilities:Suspense", "Amount": "-100.00",
         "Currency": "INR"},
    ])

    import json
    payload = json.dumps({
        "context": {"review_path": str(review_p)},
        "changes": [{
            "Sr": "1", "Category": "A", "Credit Account": "Income:Interest:ACME FD",
            "_orig": "Liabilities:Suspense",
        }],
        "all_rows": [],
    })

    status, download_update, part_i_download_update = tjr._save_changes(payload)

    assert "nothing to exclude" in status
    assert part_i_download_update["interactive"] is False
    assert part_i_download_update["value"] is None


def test_save_changes_deletes_stale_part_i_file_when_no_longer_needed(tmp_path):
    """If a previous run's Part I split is still on disk but this journal now
    carries no 15GJ rows (e.g. the 26AS was re-run without Part II data), Save
    must delete it -- a leftover Part I file the current data doesn't support
    is exactly the stale-derivative bug this feature removes."""
    review_p = tmp_path / "2026-FY2526-tds-journals-review.csv"
    journal_p = tmp_path / "2026-FY2526-tds-journals.csv"
    part_i_p = tmp_path / "2026-FY2526-tds-journals-partI.csv"
    part_i_p.write_text("stale from a previous run with 15G/15H data\n", encoding="utf-8")

    _write_csv(review_p, tjr._REVIEW_HEADERS, [_review_row()])
    _write_csv(journal_p, tjr._JOURNAL_HEADERS, [
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "x", "Account": "Expense:TDS on Interest", "Amount": "10.00",
         "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "x", "Account": "Income:Interest Income:Interest on FD",
         "Amount": "90.00", "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "x", "Account": "Liabilities:Suspense", "Amount": "-100.00",
         "Currency": "INR"},
    ])

    import json
    payload = json.dumps({
        "context": {"review_path": str(review_p)},
        "changes": [{
            "Sr": "1", "Category": "A", "Credit Account": "Income:Interest:ACME FD",
            "_orig": "Liabilities:Suspense",
        }],
        "all_rows": [],
    })

    tjr._save_changes(payload)

    assert not part_i_p.exists()


def test_save_changes_import_error_does_not_claim_full_journal_is_safe(tmp_path, monkeypatch):
    """If the Part I splitter fails to load, _save_changes must not fall into
    the "no Part II transactions" reassurance -- that branch previously
    triggered whenever part_i_path was None, which is also what an ImportError
    produces, so it silently told the user the full journal was the only file
    needed even though we have no idea whether Part II rows exist. It must
    instead warn loudly that the split did not run, and must delete (not
    leave behind) any stale -partI.csv from a previous save."""
    review_p = tmp_path / "2026-FY2526-tds-journals-review.csv"
    journal_p = tmp_path / "2026-FY2526-tds-journals.csv"
    part_i_p = tmp_path / "2026-FY2526-tds-journals-partI.csv"
    part_i_p.write_text("stale from a previous run with 15G/15H data\n", encoding="utf-8")

    _write_csv(review_p, tjr._REVIEW_HEADERS, [_review_row()])
    _write_csv(journal_p, tjr._JOURNAL_HEADERS, [
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "x", "Account": "Expense:TDS on Interest", "Amount": "10.00",
         "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "x", "Account": "Income:Interest Income:Interest on FD",
         "Amount": "90.00", "Currency": "INR"},
        {"Date": "2026-03-31", "Transaction ID": "2526-TDSJ01", "Number": "2526-TDSJ01",
         "Description": "x", "Account": "Liabilities:Suspense", "Amount": "-100.00",
         "Currency": "INR"},
    ])

    def _raise():
        raise ImportError("simulated: build_tds_journals not importable")

    monkeypatch.setattr(tjr, "_load_split_part_ii", _raise)

    import json
    payload = json.dumps({
        "context": {"review_path": str(review_p)},
        "changes": [{
            "Sr": "1", "Category": "A", "Credit Account": "Income:Interest:ACME FD",
            "_orig": "Liabilities:Suspense",
        }],
        "all_rows": [],
    })

    status, download_update, part_i_download_update = tjr._save_changes(payload)

    assert "nothing to exclude" not in status
    assert "could not be regenerated" in status
    assert "do NOT assume" in status or "do not assume" in status.lower()
    assert part_i_download_update["interactive"] is False
    assert part_i_download_update["value"] is None
    assert not part_i_p.exists()


def test_journal_path_for_derives_sibling_journal_csv():
    p = tjr._journal_path_for("/out/2026-tds-journals-review.csv")
    assert p.name == "2026-tds-journals.csv"


# ---------------------------------------------------------------------------
# Render path -- mandatory: must actually build the HTML, not just call
# helper functions in isolation (see test_gnucash_review.py /
# test_itr_mapping_review.py for the established pattern this follows).
# ---------------------------------------------------------------------------

def test_load_review_data_render_path_has_no_residual_tokens(tmp_path):
    review_p = tmp_path / "2026-FY2526-tds-journals-review.csv"
    _write_csv(review_p, tjr._REVIEW_HEADERS, [
        _review_row(sr="1", category="A", credit_account="Liabilities:Suspense"),
    ])

    html = tjr._load_review_data(str(review_p), "")

    assert re.findall(r"%%[A-Z_]*%%", html) == []
    assert "<table" in html
    assert "tdsjr-payload-box" in html
    assert 'id="tdsjr-app"' in html
    # Proves rows made it through prepare_rows into the rendered HTML, not
    # just an empty shell.
    assert "ACME BANK" in html
    assert "194A" in html


def test_load_review_data_empty_rows_render_is_safe(tmp_path):
    review_p = tmp_path / "2026-FY2526-tds-journals-review.csv"
    _write_csv(review_p, tjr._REVIEW_HEADERS, [])

    html = tjr._load_review_data(str(review_p), "")

    assert re.findall(r"%%[A-Z_]*%%", html) == []
    assert "nothing to review" in html.lower()


def test_load_review_data_hostile_content_not_live_markup(tmp_path):
    hostile = "</script><img src=x onerror=alert(1)>\"\\%%APP%%"
    review_p = tmp_path / "2026-FY2526-tds-journals-review.csv"
    _write_csv(review_p, tjr._REVIEW_HEADERS, [
        _review_row(sr="1", category="A", credit_account="Liabilities:Suspense"),
    ])
    rows = list(csv.DictReader(open(review_p, encoding="utf-8")))
    rows[0]["Deductor"] = hostile
    _write_csv(review_p, tjr._REVIEW_HEADERS, rows)

    html = tjr._load_review_data(str(review_p), "")

    assert "</script><img src=x onerror=alert(1)>" not in html
    assert "%%APP%%" not in html
