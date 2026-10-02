"""
MAP-27 -- self-describing amount headers on the GnuCash IMPORT-READY file.

The mapper's import-ready CSV (and the review re-save of it) names its two amount
columns "Amount Negated (Deposit)" and "Amount (Withdrawal)", in the same
positions as the old "Deposit" / "Withdrawal". Every reader accepts the new pair,
the 1092991 pair ("Deposit - Amount Negated" / "Withdrawal - Amount") and the
plain names. Canonical bank CSVs keep the plain names.

A Deposit/Withdrawal swap anywhere is a red flag, so every value test uses
DISTINCT numbers per side and checks the value under the header, row by row.

Fixtures are synthetic.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "skill_hdfc"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "agents"))

import hdfc_fixture_gen as fixture_gen  # noqa: E402
import test_gnucash_dedup_account_scope as dedup_fixture  # noqa: E402
from agents import canonical_io as cio  # noqa: E402
from agents import amount_headers as ah  # noqa: E402
from agents.skill_gnucash_pipeline import agent as pipeline  # noqa: E402
from ui.tabs import gnucash_review as rv  # noqa: E402

NEW_DEP, NEW_WDL = "Amount Negated (Deposit)", "Amount (Withdrawal)"
OLD_DEP, OLD_WDL = "Deposit - Amount Negated", "Withdrawal - Amount"
PLAIN_DEP, PLAIN_WDL = "Deposit", "Withdrawal"
FORMS = [(NEW_DEP, NEW_WDL), (OLD_DEP, OLD_WDL), (PLAIN_DEP, PLAIN_WDL)]
FORM_IDS = ["new", "legacy-1092991", "plain"]

# distinct per side, so a swap can never pass
DEP_A, DEP_B = "50000.00", "500.00"
WDL_A, WDL_B, WDL_C = "2000.00", "5000.00", "3500.00"


def _row(dep_key, wdl_key, desc, dep="", wdl="", **kw):
    r = {"Date": "2025-06-01", "Description": desc, "Account": "Expenses:Misc",
         dep_key: dep, wdl_key: wdl, "Balance": "9000.00",
         "Confidence": "high", "MatchReason": "x"}
    r.update(kw)
    return r


# ---- 1. the schema constants ------------------------------------------------

def test_header_row_has_the_two_new_names_in_the_old_positions():
    fields, heads = list(cio.IMPORT_READY_FIELDS), list(cio.IMPORT_READY_HEADERS)
    assert len(heads) == len(fields)
    assert heads[fields.index("Deposit")] == NEW_DEP
    assert heads[fields.index("Withdrawal")] == NEW_WDL
    # everything else untouched and in place
    for f, h in zip(fields, heads):
        if f not in ("Deposit", "Withdrawal"):
            assert h == f
    for kept in ("Balance", "Currency", "Confidence", "MatchReason"):
        assert kept in heads


def test_canonical_schema_keeps_plain_names():
    """NEGATIVE: only the import-ready file changes; canonical stays Deposit/Withdrawal."""
    assert "Deposit" in cio.CANONICAL_FIELDS and "Withdrawal" in cio.CANONICAL_FIELDS
    assert NEW_DEP not in cio.CANONICAL_FIELDS and NEW_WDL not in cio.CANONICAL_FIELDS


def test_canonical_csv_writer_still_writes_plain_headers(tmp_path):
    """NEGATIVE: a bank skill's canonical CSV must NOT pick up the new names."""
    p = tmp_path / "canon.csv"
    cio.write_canonical_csv([{"Date": "2025-06-01", "Transaction ID": "1",
                              "Description": "d", "Account": "a", "Deposit": DEP_A,
                              "Withdrawal": "", "Balance": "1", "Currency": "INR"}], p)
    head = p.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert "Deposit" in head and "Withdrawal" in head
    assert NEW_DEP not in head and NEW_WDL not in head


def test_aliases_live_in_one_helper():
    """The alias spellings are defined once, in amount_headers, and canonical_io
    re-exports that same object (no second copy to drift)."""
    assert cio.DEPOSIT_HEADER_ALIASES is ah.DEPOSIT_HEADER_ALIASES
    assert cio.WITHDRAWAL_HEADER_ALIASES is ah.WITHDRAWAL_HEADER_ALIASES
    assert set(ah.DEPOSIT_HEADER_ALIASES) == {NEW_DEP, OLD_DEP, PLAIN_DEP}
    assert set(ah.WITHDRAWAL_HEADER_ALIASES) == {NEW_WDL, OLD_WDL, PLAIN_WDL}


# ---- 2. every reader accepts all three forms ---------------------------------

@pytest.mark.parametrize("dep_key,wdl_key", FORMS, ids=FORM_IDS)
def test_lookup_helpers_find_each_form(dep_key, wdl_key):
    row = _row(dep_key, wdl_key, "x", dep=DEP_A, wdl="")
    assert ah.find_deposit_key(row.keys()) == dep_key
    assert ah.find_withdrawal_key(row.keys()) == wdl_key
    assert ah.row_deposit(row) == DEP_A
    assert ah.row_withdrawal(row) == ""


@pytest.mark.parametrize("dep_key,wdl_key", FORMS, ids=FORM_IDS)
def test_final_closing_balance_reader_sums_each_form(dep_key, wdl_key):
    """pipeline.final_closing_balance_verdict reads the import-ready file."""
    rows = [_row(dep_key, wdl_key, "in", dep=DEP_A), _row(dep_key, wdl_key, "out", wdl=WDL_A)]
    net = float(DEP_A) - float(WDL_A)
    recon = {"account_found": True, "gnucash_balance": 1000.0}
    ok = pipeline.final_closing_balance_verdict(recon, rows, 1000.0 + net, None)
    assert "VERIFIED" in ok
    # NEGATIVE: with the sides swapped the verdict must NOT verify
    bad = pipeline.final_closing_balance_verdict(recon, rows, 1000.0 - net, None)
    assert "MISMATCH" in bad and "VERIFIED" not in bad


@pytest.mark.parametrize("dep_key,wdl_key", FORMS, ids=FORM_IDS)
def test_balance_carrier_reader_handles_each_form(dep_key, wdl_key):
    carrier = _row(dep_key, wdl_key, "BALANCE B/F")                 # no money moves
    real = _row(dep_key, wdl_key, "RENT", wdl=WDL_B)
    assert cio.is_balance_carrier([carrier, real], 0) is True
    # NEGATIVE: a first row that moves money, on EITHER side, is never a carrier
    assert cio.is_balance_carrier([real, carrier], 0) is False
    assert cio.is_balance_carrier([_row(dep_key, wdl_key, "SAL", dep=DEP_B)], 0) is False
    kept, dropped = cio.split_balance_carriers([carrier, real])
    assert dropped == [0] and kept == [real]


@pytest.mark.parametrize("dep_key,wdl_key", FORMS, ids=FORM_IDS)
def test_review_load_uses_the_files_own_amount_headers(tmp_path, dep_key, wdl_key):
    p = tmp_path / "x-GnuCash_import_ready.csv"
    rows = [_row(dep_key, wdl_key, "SALARY", dep=DEP_A), _row(dep_key, wdl_key, "RENT", wdl=WDL_A)]
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    book = dedup_fixture._build_book_with_unrelated_same_date_amount_posting(tmp_path)
    html = rv._load_review_data(str(p), book)
    assert DEP_A in html and WDL_A in html
    # the grid binds its amount columns to the file's own keys
    assert json.dumps(dep_key) in html or dep_key in html


# ---- 3. the review round trip --------------------------------------------------

_HEADER = "Date,Description,Account\n"


def _review_save(tmp_path, rows):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER, encoding="utf-8")
    payload = {"context": {"csv_path": str(p), "gnucash_file": str(p)},
               "changes": [{"_idx": 0, "_orig": "Expenses:Misc", "Description": "x",
                            "Account": "Expenses:Misc"}],
               "all_rows": rows}
    rv._save_changes(json.dumps(payload))
    with open(p, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        head = next(r)
        body = [dict(zip(head, line)) for line in r]
    return head, body


@pytest.mark.parametrize("dep_key,wdl_key", FORMS, ids=FORM_IDS)
def test_review_round_trip_writes_new_headers_and_keeps_every_amount(tmp_path, dep_key, wdl_key):
    rows = [_row(dep_key, wdl_key, "SAL-A", dep=DEP_A), _row(dep_key, wdl_key, "SAL-B", dep=DEP_B),
            _row(dep_key, wdl_key, "OUT-A", wdl=WDL_A), _row(dep_key, wdl_key, "OUT-B", wdl=WDL_B),
            _row(dep_key, wdl_key, "OUT-C", wdl=WDL_C)]
    head, body = _review_save(tmp_path, rows)
    # the saved header is the import-ready schema order with the new amount names
    present = [h for h in cio.IMPORT_READY_HEADERS if h in head]
    assert head[:len(present)] == present
    assert head.index(NEW_DEP) < head.index(NEW_WDL)
    # NEGATIVE: never reverts to (or keeps) the plain / legacy names
    for bad in (PLAIN_DEP, PLAIN_WDL, OLD_DEP, OLD_WDL):
        assert bad not in head
    want = {"SAL-A": (DEP_A, ""), "SAL-B": (DEP_B, ""),
            "OUT-A": ("", WDL_A), "OUT-B": ("", WDL_B), "OUT-C": ("", WDL_C)}
    got = {b["Description"]: (b[NEW_DEP], b[NEW_WDL]) for b in body}
    # NEGATIVE: no value dropped or swapped
    assert got == want


def test_review_round_trip_is_idempotent(tmp_path):
    """NEGATIVE: load-then-save-then-save keeps the new headers (no oscillation)."""
    rows = [_row(NEW_DEP, NEW_WDL, "SAL", dep=DEP_A), _row(NEW_DEP, NEW_WDL, "OUT", wdl=WDL_A)]
    h1, b1 = _review_save(tmp_path, rows)
    h2, b2 = _review_save(tmp_path, [dict(r) for r in b1])
    assert h1 == h2
    assert NEW_DEP in h1 and NEW_WDL in h1 and PLAIN_DEP not in h1
    assert b1 == b2


def test_import_ready_row_renames_in_place_and_never_moves_a_value():
    old = _row(PLAIN_DEP, PLAIN_WDL, "x", dep=DEP_A, wdl="")
    new = ah.import_ready_row(old)
    assert list(new) == [NEW_DEP if k == PLAIN_DEP else NEW_WDL if k == PLAIN_WDL else k for k in old]
    assert new[NEW_DEP] == DEP_A and new[NEW_WDL] == ""
    assert PLAIN_DEP not in new and PLAIN_WDL not in new
    assert old[PLAIN_DEP] == DEP_A                      # input not mutated
    again = ah.import_ready_row(new)
    assert again == new                                 # idempotent


# ---- 4. the mapper's real output, end to end ----------------------------------

def _run_pipeline(tmp_path):
    csv_path = tmp_path / "syn_hdfc.csv"
    csv_path.write_text(fixture_gen.build_csv_text(), encoding="utf-8")
    book = dedup_fixture._build_book_with_unrelated_same_date_amount_posting(tmp_path)
    out = tmp_path / "out-GnuCash_import_ready.csv"
    result = pipeline.run(bank="HDFC", statement_files=str(csv_path),
                          gnucash_file=book, output_path=str(out), config_path=None)
    assert out.is_file(), result
    with open(out, newline="", encoding="utf-8") as f:
        r = csv.reader(f)
        head = next(r)
        rows = [dict(zip(head, line)) for line in r]
    return head, rows, result


def test_mapper_output_has_new_headers_and_each_amount_under_its_own_side(tmp_path):
    head, rows, result = _run_pipeline(tmp_path)
    assert head == [h for h in cio.IMPORT_READY_HEADERS if h in head]   # schema order
    assert NEW_DEP in head and NEW_WDL in head
    assert head.index(NEW_DEP) + 1 == head.index(NEW_WDL)
    # NEGATIVE: no plain or legacy names in the import-ready output
    for bad in (PLAIN_DEP, PLAIN_WDL, OLD_DEP, OLD_WDL):
        assert bad not in head
    by_desc = {r["Description"]: r for r in rows}
    deposits = {r[NEW_DEP] for r in rows if r[NEW_DEP]}
    withdrawals = {r[NEW_WDL] for r in rows if r[NEW_WDL]}
    # NEGATIVE: the statement's credits are only ever under the Deposit header
    assert deposits == {DEP_A, DEP_B}
    assert withdrawals == {WDL_A, WDL_B, WDL_C}
    assert deposits.isdisjoint(withdrawals)
    for r in rows:                                       # one side per row
        assert not (r[NEW_DEP] and r[NEW_WDL])
    sal = next(r for d, r in by_desc.items() if "SALARY" in d)
    assert sal[NEW_DEP] == DEP_A and sal[NEW_WDL] == ""
    gro = next(r for d, r in by_desc.items() if "GROCERY" in d)
    assert gro[NEW_WDL] == WDL_A and gro[NEW_DEP] == ""
    # the final closing-balance check still reads the new headers (no false mismatch)
    assert "CLOSING BALANCE MISMATCH" not in result


# ---- 5. the docs note ------------------------------------------------------------

def test_user_guide_has_the_importing_into_gnucash_note():
    text = (ROOT / "docs" / "user-guide" / "skill_gnucash_pipeline.md").read_text(encoding="utf-8")
    assert "Importing into GnuCash" in text
    assert NEW_DEP in text and NEW_WDL in text
    assert "Amount (Negated)" in text and "preset" in text
    # the note tells the user NOT to use the Transfer Amount column types
    assert 'not "Transfer Amount"' in text
