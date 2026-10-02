"""IMP-11: own transfers already booked from the OTHER bank's statement.

All data is synthetic (masked account numbers, round amounts, A/B names).
Nothing is dropped automatically: matched rows are set aside UNTICKED.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src", ROOT / "src" / "agents", Path(__file__).parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gnc_book_fixture import (  # noqa: E402
    HDFC1, HSBC1, P_HDFC1, P_HSBC1, account_xml, standard_accounts, txn_xml, write_book,
)
from agents.skill_gnucash_reconciler.agent import (  # noqa: E402
    match_booked_own_transfers, parse_gnucash_for_reconcile, reconcile,
)
from agents.skill_gnucash_pipeline.agent import (  # noqa: E402
    _set_aside_booked_transfers, booked_sidecar_path, explain_opening_gap,
    final_closing_balance_verdict,
)

TARGET = "Root Account:" + P_HSBC1
AMT = 5000000   # 50,000.00 in paise


def _book(tmp_path, txns, extra=()):
    path = tmp_path / "b.gnucash"
    write_book(path, standard_accounts(extra), txns)
    return parse_gnucash_for_reconcile(str(path), account_filter=TARGET)


def out_to_hdfc(date, paise=AMT):
    """HSBC pays HDFC (book outflow from the target)."""
    return txn_xml("TRF OUT", date, [(HSBC1, -paise), (HDFC1, paise)])


def in_from_hdfc(date, paise=AMT):
    return txn_xml("TRF IN", date, [(HSBC1, paise), (HDFC1, -paise)])


def row(date, deposit=0, withdrawal=0):
    return {"date": date, "deposit": float(deposit), "withdrawal": float(withdrawal)}


# --- positive ---------------------------------------------------------------

def test_debit_one_day_off_matches_and_reason_names_the_book_entry(tmp_path):
    data = _book(tmp_path, [out_to_hdfc("2025-06-10")])
    m = match_booked_own_transfers([row("11/06/2025", withdrawal=50000)], data, TARGET)
    assert len(m) == 1
    assert m[0]["row_idx"] == 0 and m[0]["days_off"] == 1 and not m[0]["tie"]
    assert "Already booked" in m[0]["reason"]
    assert "2025-06-10" in m[0]["reason"]
    assert "094XXXX1234" in m[0]["reason"]          # the other own account


def test_credit_two_days_off_matches(tmp_path):
    data = _book(tmp_path, [in_from_hdfc("2025-06-10")])
    m = match_booked_own_transfers([row("12/06/2025", deposit=50000)], data, TARGET)
    assert [x["row_idx"] for x in m] == [0] and m[0]["days_off"] == 2


def test_book_split_before_statement_start_matches_across_fy_boundary(tmp_path):
    """The 1 Apr statement debit whose book split is dated 31 Mar."""
    data = _book(tmp_path, [out_to_hdfc("2025-03-31")])
    rows = [row("01/04/2025", withdrawal=50000), row("05/04/2025", deposit=10)]
    m = match_booked_own_transfers(rows, data, TARGET)
    assert len(m) == 1 and m[0]["row_idx"] == 0
    assert m[0]["pre_period"] is True


def test_nearest_date_pairs_first_and_is_one_to_one(tmp_path):
    data = _book(tmp_path, [out_to_hdfc("2025-06-09"), out_to_hdfc("2025-06-12")])
    m = match_booked_own_transfers(
        [row("10/06/2025", withdrawal=50000), row("11/06/2025", withdrawal=50000)],
        data, TARGET)
    by_row = {x["row_idx"]: x for x in m}
    assert by_row[0]["book_date"] == "2025-06-09"
    assert by_row[1]["book_date"] == "2025-06-12"
    assert not by_row[0]["tie"] and not by_row[1]["tie"]


def test_tie_pairs_in_statement_order_and_says_so(tmp_path):
    data = _book(tmp_path, [out_to_hdfc("2025-06-10")])
    rows = [row("09/06/2025", withdrawal=50000), row("11/06/2025", withdrawal=50000)]
    m = match_booked_own_transfers(rows, data, TARGET)
    assert len(m) == 1 and m[0]["row_idx"] == 0 and m[0]["tie"] is True
    assert "statement order" in m[0]["reason"]


# --- negative tests (a)-(f) --------------------------------------------------

def test_a_expense_or_income_other_leg_is_not_matched(tmp_path):
    """(a) a third-party payment of the same amount on an adjacent day."""
    for other in ("dining", "int"):
        data = _book(tmp_path, [txn_xml("SHOP", "2025-06-10", [(HSBC1, -AMT), (other, AMT)])])
        m = match_booked_own_transfers([row("11/06/2025", withdrawal=50000)], data, TARGET)
        assert m == [], other


def test_a_cash_other_leg_is_not_matched(tmp_path):
    cash = account_xml("cashacc", "Cash in Hand", "CASH", "cab")
    data = _book(tmp_path, [txn_xml("ATM", "2025-06-10", [(HSBC1, -AMT), ("cashacc", AMT)])],
                 extra=[cash])
    assert match_booked_own_transfers([row("11/06/2025", withdrawal=50000)], data, TARGET) == []


def test_a_split_leg_to_a_non_bank_account_is_not_matched(tmp_path):
    """One leg bank, another leg expense (e.g. transfer plus fee)."""
    t = txn_xml("MIXED", "2025-06-10", [(HSBC1, -AMT), (HDFC1, AMT - 100), ("dining", 100)])
    data = _book(tmp_path, [t])
    assert match_booked_own_transfers([row("11/06/2025", withdrawal=50000)], data, TARGET) == []


def test_b_two_rows_one_book_leg_gives_exactly_one_match(tmp_path):
    """(b) two genuine own transfers, consecutive days, one book leg."""
    data = _book(tmp_path, [out_to_hdfc("2025-06-10")])
    rows = [row("10/06/2025", withdrawal=50000), row("11/06/2025", withdrawal=50000)]
    # the exact-date row owns the split (existing dedup); the other must stay
    assert match_booked_own_transfers(rows, data, TARGET) == []
    rows = [row("11/06/2025", withdrawal=50000), row("12/06/2025", withdrawal=50000)]
    m = match_booked_own_transfers(rows, data, TARGET)
    assert [x["row_idx"] for x in m] == [0]          # the other row stays ticked


def test_c_three_days_away_is_not_matched(tmp_path):
    data = _book(tmp_path, [out_to_hdfc("2025-06-10")])
    assert match_booked_own_transfers([row("13/06/2025", withdrawal=50000)], data, TARGET) == []
    assert match_booked_own_transfers([row("07/06/2025", withdrawal=50000)], data, TARGET) == []


def test_direction_must_agree(tmp_path):
    data = _book(tmp_path, [out_to_hdfc("2025-06-10")])
    assert match_booked_own_transfers([row("11/06/2025", deposit=50000)], data, TARGET) == []


def test_amount_must_be_equal(tmp_path):
    data = _book(tmp_path, [out_to_hdfc("2025-06-10")])
    assert match_booked_own_transfers([row("11/06/2025", withdrawal=50001)], data, TARGET) == []


def test_d_exact_duplicate_still_removed_by_existing_dedup_and_never_claims_a_neighbour(tmp_path):
    data = _book(tmp_path, [out_to_hdfc("2025-06-10")])
    rows = [row("2025-06-10", withdrawal=50000), row("2025-06-11", withdrawal=50000)]
    rows = [dict(r, row_num=i, description="x") for i, r in enumerate(rows, 1)]
    report, _summary = reconcile(rows, data)
    assert report[0]["status"] != "New"              # exact (date, amount): removed as before
    assert report[1]["status"] == "New"
    # ... and the matcher does not hand that same book leg to the neighbouring day
    assert match_booked_own_transfers(rows, data, TARGET) == []


def test_f_one_row_statement_with_no_book_legs_has_nothing_unticked(tmp_path):
    data = _book(tmp_path, [])
    assert match_booked_own_transfers([row("11/06/2025", withdrawal=50000)], data, TARGET) == []


def test_matcher_ignores_splits_in_other_accounts(tmp_path):
    """A same-amount movement in a DIFFERENT bank account is not a target-account leg."""
    t = txn_xml("OTHER", "2025-06-10", [("hsbc2", -AMT), (HDFC1, AMT)])
    data = _book(tmp_path, [t])
    assert match_booked_own_transfers([row("11/06/2025", withdrawal=50000)], data, TARGET) == []


def test_zero_amount_row_never_matches(tmp_path):
    data = _book(tmp_path, [out_to_hdfc("2025-06-10", paise=0 + 1)])
    assert match_booked_own_transfers([row("11/06/2025")], data, TARGET) == []


# --- set-aside (e) -----------------------------------------------------------

FIELDS = ["Date", "Description", "Account", "Deposit", "Withdrawal", "Confidence", "MatchReason"]


def _mapped_csv(tmp_path, rows):
    p = tmp_path / "out.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return str(p)


def _r(date, desc, dep="", wd=""):
    return {"Date": date, "Description": desc, "Account": "X", "Deposit": dep,
            "Withdrawal": wd, "Confidence": "high", "MatchReason": "m"}


def test_e_matched_row_is_not_in_the_importable_csv_but_is_parked(tmp_path):
    out = _mapped_csv(tmp_path, [_r("01/04/2025", "A", wd="50000"), _r("02/04/2025", "B", dep="10")])
    flags: dict = {}
    n = _set_aside_booked_transfers(out, flags, [{"row_idx": 0, "reason": "Already booked ...",
                                                  "book_date": "2025-03-31", "pre_period": True}])
    assert n == 1
    kept = list(csv.DictReader(open(out, encoding="utf-8")))
    assert [r["Description"] for r in kept] == ["B"]          # never importable unless re-ticked
    parked = json.load(open(booked_sidecar_path(out), encoding="utf-8"))
    assert [e["row"]["Description"] for e in parked] == ["A"]
    assert parked[0]["reason"].startswith("Already booked")


def test_set_aside_shifts_contra_flags_and_carries_the_parked_ones(tmp_path):
    out = _mapped_csv(tmp_path, [_r("1", "A"), _r("2", "B"), _r("3", "C")])
    flags = {0: {"reason": "c0"}, 2: {"reason": "c2"}}
    _set_aside_booked_transfers(out, flags, [{"row_idx": 0, "reason": "r"}])
    assert flags == {1: {"reason": "c2"}}                      # C moved from index 2 to 1
    parked = json.load(open(booked_sidecar_path(out), encoding="utf-8"))
    assert parked[0]["contra"] == {"reason": "c0"}


def test_f_no_matches_leaves_csv_untouched_and_sidecar_empty(tmp_path):
    out = _mapped_csv(tmp_path, [_r("1", "A")])
    before = Path(out).read_bytes()
    assert _set_aside_booked_transfers(out, {}, []) == 0
    assert Path(out).read_bytes() == before
    assert json.load(open(booked_sidecar_path(out), encoding="utf-8")) == []


def test_stale_sidecar_is_overwritten_on_rerun(tmp_path):
    out = _mapped_csv(tmp_path, [_r("1", "A")])
    booked_sidecar_path(out).write_text(json.dumps([{"row": {"Description": "old"}}]))
    _set_aside_booked_transfers(out, {}, [])
    assert json.load(open(booked_sidecar_path(out), encoding="utf-8")) == []


# --- opening gap + closing verdict -------------------------------------------

def test_opening_gap_named_when_pre_period_rows_explain_it():
    m = [{"pre_period": True, "amount": -50000.0, "book_date": "2025-03-31",
          "other_account": "HDFC Bank"}]
    assert explain_opening_gap(50000.0, m) == m


def test_opening_gap_not_explained_when_amounts_differ_or_not_pre_period():
    pre = {"pre_period": True, "amount": -50000.0}
    assert explain_opening_gap(49000.0, [pre]) == []
    assert explain_opening_gap(50000.0, [dict(pre, pre_period=False)]) == []
    assert explain_opening_gap(None, [pre]) == []
    assert explain_opening_gap(0.01, [pre]) == []


def test_verdict_names_the_rows_and_computes_closing_without_unticked_rows():
    recon = {"account_found": True, "gnucash_balance": 100000.0}
    explained = [{"pre_period": True, "amount": -50000.0, "book_date": "2025-03-31",
                  "other_account": "HDFC Bank - 094XXXX1234"}]
    final_rows = [{"Deposit": "10", "Withdrawal": ""}]       # the unticked row is NOT here
    v = final_closing_balance_verdict(recon, final_rows, 100010.0, 50000.0, explained)
    assert "VERIFIED" in v and "caused by 1 statement row" in v and "2025-03-31" in v


def test_verdict_stays_red_when_gap_not_explained():
    recon = {"account_found": True, "gnucash_balance": 100000.0}
    v = final_closing_balance_verdict(recon, [], 100010.0, 50000.0, [])
    assert v.startswith("⚠ unreconciled")
    assert "caused by" not in v


def test_verdict_does_not_count_unticked_rows_into_closing():
    """If the unticked row WERE counted the closing would be off; it is not."""
    recon = {"account_found": True, "gnucash_balance": 100000.0}
    explained = [{"pre_period": True, "amount": -50000.0}]
    v = final_closing_balance_verdict(recon, [], 50000.0, 50000.0, explained)
    assert "MISMATCH" in v      # book 100000 vs closing 50000: still honest, not a false green


# --- engine + review tab -----------------------------------------------------

def _engine():
    from ui import _review_engine as e
    return e


def _spec(e, **kw):
    return e.ReviewSpec(app_id="t", columns=[e.Column("A", "A")], target_col="A",
                        payload_var="pv", **kw)


def test_engine_off_renders_nothing_exclude_related():
    e = _engine()
    html = e.build_html(_spec(e), [{"A": "x"}])
    assert "exclude-sel" not in html and "include-sel" not in html
    assert "row-excluded" not in html and "excluded_dirty" not in html
    assert "%%" not in html
    # NEGATIVE: the payload object literal must not get a doubled comma (JS syntax error)
    assert "}),," not in html and "},," not in html


def test_engine_on_renders_buttons_and_payload_fields():
    e = _engine()
    html = e.build_html(_spec(e, allow_exclude=True), [{"A": "x"}])
    assert "t-exclude-sel" in html and "t-include-sel" in html
    assert "excluded_dirty: exclDirty" in html
    assert ".filter(r => !r._excluded)" in html
    assert "%%" not in html


def test_parse_payload_excluded_keys_only_when_sent():
    e = _engine()
    assert "excluded" not in e.parse_payload("{}")
    out = e.parse_payload(json.dumps({"excluded": [{"a": 1}], "excluded_dirty": True}))
    assert out["excluded"] == [{"a": 1}] and out["excluded_dirty"] is True


def _tab(tmp_path, mapped, booked):
    from ui.tabs import gnucash_review as g
    book = tmp_path / "bk.gnucash"
    write_book(book, standard_accounts(), [])
    csvp = tmp_path / "hsbc_import_ready.csv"
    with open(csvp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(mapped)
    if booked is not None:
        csvp.with_suffix(".matched.json").write_text(json.dumps(booked), encoding="utf-8")
    return g, csvp, book


def test_review_load_shows_booked_rows_excluded_with_their_reason(tmp_path):
    entry = {"reason": "Already booked from HDFC statement: book entry 2025-03-31 with X",
             "row": _r("01/04/2025", "A OWNER TRF", wd="50000")}
    g, csvp, book = _tab(tmp_path, [_r("02/04/2025", "B", dep="10")], [entry])
    html = g._load_review_data(str(csvp), str(book))
    assert html.count('"_excluded": true') == 1          # only the booked row starts excluded
    assert "Already booked from HDFC statement" in html
    assert "exclude-sel" in html


def test_review_load_without_sidecar_has_no_excluded_rows(tmp_path):
    g, csvp, book = _tab(tmp_path, [_r("02/04/2025", "B", dep="10")], None)
    html = g._load_review_data(str(csvp), str(book))
    assert html.count('"_excluded": true') == 0


def test_review_save_keeps_excluded_out_of_csv_and_in_sidecar(tmp_path):
    g, csvp, book = _tab(tmp_path, [_r("02/04/2025", "B", dep="10")], [])
    booked_row = _r("01/04/2025", "A", wd="50000")
    payload = {"context": {"csv_path": str(csvp), "gnucash_file": str(book)},
               "changes": [], "all_rows": [_r("02/04/2025", "B", dep="10")],
               "excluded": [booked_row], "excluded_dirty": True}
    g._save_changes(json.dumps(payload))
    kept = list(csv.DictReader(open(csvp, encoding="utf-8")))
    assert [r["Description"] for r in kept] == ["B"]
    side = json.load(open(csvp.with_suffix(".matched.json"), encoding="utf-8"))
    assert [e["row"]["Description"] for e in side] == ["A"]


def test_review_retick_moves_row_into_csv_and_out_of_sidecar(tmp_path):
    g, csvp, book = _tab(tmp_path, [_r("02/04/2025", "B", dep="10")],
                         [{"reason": "r", "row": _r("01/04/2025", "A", wd="50000")}])
    payload = {"context": {"csv_path": str(csvp), "gnucash_file": str(book)},
               "changes": [], "excluded": [], "excluded_dirty": True,
               "all_rows": [_r("02/04/2025", "B", dep="10"), _r("01/04/2025", "A", wd="50000")]}
    g._save_changes(json.dumps(payload))
    kept = list(csv.DictReader(open(csvp, encoding="utf-8")))
    assert sorted(r["Description"] for r in kept) == ["A", "B"]
    assert json.load(open(csvp.with_suffix(".matched.json"), encoding="utf-8")) == []


def test_review_save_with_nothing_touched_changes_nothing(tmp_path):
    g, csvp, book = _tab(tmp_path, [_r("02/04/2025", "B", dep="10")], [])
    before = csvp.read_bytes()
    payload = {"context": {"csv_path": str(csvp), "gnucash_file": str(book)},
               "changes": [], "all_rows": [_r("02/04/2025", "B", dep="10")],
               "excluded": [], "excluded_dirty": False}
    msg, _ = g._save_changes(json.dumps(payload))
    assert "No changes" in msg and csvp.read_bytes() == before


def test_review_all_rows_excluded_writes_header_only_csv(tmp_path):
    g, csvp, book = _tab(tmp_path, [_r("02/04/2025", "B", dep="10")], [])
    payload = {"context": {"csv_path": str(csvp), "gnucash_file": str(book)},
               "changes": [], "all_rows": [], "excluded": [_r("02/04/2025", "B", dep="10")],
               "excluded_dirty": True}
    g._save_changes(json.dumps(payload))
    assert list(csv.DictReader(open(csvp, encoding="utf-8"))) == []
