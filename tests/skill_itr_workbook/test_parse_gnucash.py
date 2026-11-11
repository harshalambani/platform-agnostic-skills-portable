"""
tests/skill_itr_workbook/test_parse_gnucash.py -- Batch 2 tests: the
.gnucash book parser, CG lot reconstruction, 234C quarterly bucketing, and
the book<->HTML cross-check (plan sections 1.2, 3.2, 6.1). Fully offline;
synthetic fixtures only (see fixture_gen.py's build_syn_ind_gnucash /
build_syn_huf_gnucash). Real-corpus tests are behind @pytest.mark.local_samples
and skip unless PASK_REAL_CORPUS_CONFIG names a local file (see real_corpus_config.py), so CI never
touches real data.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
SCRIPTS = SRC / "agents" / "skill_itr_workbook" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

for p in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import parse_eguile as pe  # noqa: E402
import parse_gnucash as pg  # noqa: E402
import lots  # noqa: E402
import quarters  # noqa: E402
import verify as vfy  # noqa: E402
import fixture_gen  # noqa: E402
import real_corpus_config as rcc  # noqa: E402

YEAR_KEY = "2024-25"


@pytest.fixture(scope="module")
def syn_ind_book() -> pg.Book:
    return pg.parse_book_text(fixture_gen.build_syn_ind_gnucash())


@pytest.fixture(scope="module")
def syn_huf_book() -> pg.Book:
    return pg.parse_book_text(fixture_gen.build_syn_huf_gnucash())


# ---------------------------------------------------------------------------
# parse_gnucash.py basics
# ---------------------------------------------------------------------------

def test_fy_window():
    assert pg.fy_window("2024-25") == (date(2024, 4, 1), date(2025, 3, 31))


def test_book_parses_accounts_and_transactions(syn_ind_book):
    assert len(syn_ind_book.accounts) > 10
    assert len(syn_ind_book.transactions) > 10
    names = {a.name for a in syn_ind_book.accounts.values()}
    assert "Business Remuneration" in names
    assert "OldTech Ltd" in names


def test_account_paths_built(syn_ind_book):
    synthcorp = [a for a in syn_ind_book.accounts.values() if a.name == "SynthCorp Shares"][0]
    assert synthcorp.path == "Assets/Investments/SynthCorp Shares"


@pytest.mark.parametrize("account_name, expected_fy_total", [
    ("Business Remuneration", 300000.00),
    ("Bank Interest", 20000.00),
    ("Business Expenses", -120000.00),
    ("TDS on Interest", -5000.00),
])
def test_account_fy_sum_matches_html_leaf(syn_ind_book, account_name, expected_fy_total):
    """Sign-normalized book FY sums must equal the HTML fixture's totals for
    the accounts the two fixtures share (plan section 3.2)."""
    acct = [a for a in syn_ind_book.accounts.values() if a.name == account_name][0]
    total = pg.account_fy_sum(syn_ind_book, acct.guid, YEAR_KEY)
    assert total == pytest.approx(expected_fy_total, abs=0.01)


# ---------------------------------------------------------------------------
# Book <-> HTML cross-check (plan section 1.2, point 4)
# ---------------------------------------------------------------------------

def test_cross_check_syn_ind_clean(syn_ind_book):
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    results = vfy.cross_check(tree, syn_ind_book, YEAR_KEY)
    # Business Remuneration, Bank Interest, Salary, IT Refund Principal,
    # IT Refund Interest, Business Expenses, TDS, TDS on Salary
    assert len(results) == 8
    assert all(r.ok for r in results)


def test_cross_check_syn_huf_clean(syn_huf_book):
    tree = pe.parse_html(fixture_gen.build_syn_huf_html())
    results = vfy.cross_check(tree, syn_huf_book, YEAR_KEY)
    assert len(results) == 3  # Interest Income, Long Term Capital Gain, TDS on Interest
    assert all(r.ok for r in results)


def test_cross_check_reports_the_right_account_on_mismatch(syn_ind_book):
    """A doctored book (Business Remuneration off by 500) must surface as a
    named mismatch, not a silent pass."""
    import copy
    from fractions import Fraction
    doctored = copy.deepcopy(syn_ind_book)
    for txn in doctored.transactions:
        for sp in txn.splits:
            acct = doctored.accounts[sp.account_guid]
            if acct.name == "Business Remuneration":
                sp.value -= Fraction(500)

    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    results = vfy.cross_check(tree, doctored, YEAR_KEY)
    mismatches = [r for r in results if not r.ok]
    assert len(mismatches) == 1
    assert mismatches[0].name == "Business Remuneration"


# ---------------------------------------------------------------------------
# CG lot reconstruction (plan section 6.1)
# ---------------------------------------------------------------------------

def test_lots_reconciliation_invariant_holds(syn_ind_book):
    """Sigma(lot proceeds - lot cost) == booked gain, for every disposal."""
    recs = lots.reconstruct_lots(syn_ind_book, YEAR_KEY)
    assert len(recs) == 5
    assert all(r.ok for r in recs)


def test_lots_fifo_consistent_two_lot_sale_one_gain_one_loss(syn_ind_book):
    recs = {r.sale_date: r for r in lots.reconstruct_lots(syn_ind_book, YEAR_KEY)}
    oldtech = recs[date(2024, 8, 15)]
    assert len(oldtech.lots) == 2
    gains = sorted(lot.gain for lot in oldtech.lots)
    assert gains[0] < 0 < gains[1]  # one loss lot, one gain lot
    for lot in oldtech.lots:
        assert lot.attribution == "matched"
        assert lot.fifo_flag is None
        assert lot.buy_date is not None


def test_lots_ambiguous_cost_match_flags_unattributed(syn_ind_book):
    recs = {r.sale_date: r for r in lots.reconstruct_lots(syn_ind_book, YEAR_KEY)}
    ambiguous = recs[date(2024, 9, 1)]
    assert len(ambiguous.lots) == 1
    assert ambiguous.lots[0].attribution == lots.UNATTRIBUTED
    assert ambiguous.lots[0].buy_date is None
    # The gain math must still hold even though attribution failed.
    assert ambiguous.ok


def test_lots_engineered_fifo_violation_is_flagged_never_corrected(syn_ind_book):
    recs = {r.sale_date: r for r in lots.reconstruct_lots(syn_ind_book, YEAR_KEY)}
    violator = recs[date(2024, 9, 15)]
    assert len(violator.lots) == 1
    lot = violator.lots[0]
    assert lot.attribution == "matched"
    assert lot.fifo_flag == lots.FIFO_VIOLATION
    # Never auto-corrected: the matched buy date/cost are still reported as-is.
    assert lot.buy_date == date(2019, 1, 1)


def test_lots_tier3_straddle_splits_into_one_row_per_lot(syn_ind_book):
    """B3 carry-forward patch: a Tier-3 match consuming two whole lots on
    opposite sides of the 12-month LT/ST boundary must come back as two
    rows (one ST, one LT), never merged."""
    recs = {r.sale_date: r for r in lots.reconstruct_lots(syn_ind_book, YEAR_KEY)}
    straddle = recs[date(2024, 9, 5)]
    assert len(straddle.lots) == 2
    assert all(lot.attribution == "matched" for lot in straddle.lots)
    assert all(lot.straddle_split for lot in straddle.lots)
    terms = {(lot.sale_date - lot.buy_date).days >= 365 for lot in straddle.lots}
    assert terms == {True, False}  # one LT row, one ST row
    assert straddle.ok


def test_lots_intra_year_stcg_single_lot(syn_ind_book):
    recs = {r.sale_date: r for r in lots.reconstruct_lots(syn_ind_book, YEAR_KEY)}
    quickflip = recs[date(2024, 11, 1)]
    assert len(quickflip.lots) == 1
    lot = quickflip.lots[0]
    assert lot.attribution == "matched"
    assert lot.fifo_flag is None
    assert (lot.sale_date - lot.buy_date).days < 365  # short-term holding period


def test_lots_huf_single_lot_matches_html_total(syn_huf_book):
    recs = lots.reconstruct_lots(syn_huf_book, YEAR_KEY)
    assert len(recs) == 1
    assert recs[0].booked_gain == pytest.approx(1000.00, abs=0.01)
    assert recs[0].ok


# ---------------------------------------------------------------------------
# 234C quarterly bucketing (plan section 1.2, point 1)
# ---------------------------------------------------------------------------

def test_quarters_buckets_sum_to_fy_total(syn_ind_book):
    div_guids = {a.guid for a in syn_ind_book.accounts.values() if a.name == "Dividend - Shares"}
    qb = quarters.bucket_receipts(syn_ind_book, div_guids, YEAR_KEY)
    assert sum(qb.buckets) == pytest.approx(qb.total, abs=0.01)
    assert qb.total == pytest.approx(8500.00, abs=0.01)


def test_quarters_spans_at_least_three_buckets(syn_ind_book):
    div_guids = {a.guid for a in syn_ind_book.accounts.values() if a.name == "Dividend - Shares"}
    qb = quarters.bucket_receipts(syn_ind_book, div_guids, YEAR_KEY)
    nonzero = [b for b in qb.buckets if b != 0.0]
    assert len(nonzero) >= 3


def test_quarters_flags_31_03_entry_without_reattributing(syn_ind_book):
    div_guids = {a.guid for a in syn_ind_book.accounts.values() if a.name == "Dividend - Shares"}
    qb = quarters.bucket_receipts(syn_ind_book, div_guids, YEAR_KEY)
    assert len(qb.gross_up_flags) == 1
    assert qb.gross_up_flags[0]["date"] == "2025-03-31"
    # The flagged amount stays in bucket index 4 (16-Mar..31-Mar), never
    # reattributed to an earlier quarter.
    assert qb.buckets[4] == pytest.approx(1000.00, abs=0.01)


# ---------------------------------------------------------------------------
# Batch 1 carry-forward check: the two adversarial tests must still exist.
# ---------------------------------------------------------------------------

def test_b1_adversarial_tests_present():
    test_file = (Path(__file__).parent / "test_parse_eguile.py").read_text(encoding="utf-8")
    assert "test_truncated_html_hard_fails_with_exact_message" in test_file
    assert "test_nonzero_imbalance_fails_validation" in test_file


# ---------------------------------------------------------------------------
# Real-corpus tests -- never run in CI (skipped unless PASK_REAL_CORPUS_CONFIG is set).
# ---------------------------------------------------------------------------

def _real_pairs_or_skip():
    pairs, why = rcc.need_pairs()
    if pairs is None:
        pytest.skip(why)
    return pairs


@pytest.mark.local_samples
def test_real_corpus_cross_check_green_for_all_listed_entities():
    """Pairs come from the local file named by PASK_REAL_CORPUS_CONFIG (see real_corpus_config.py)."""
    pairs = _real_pairs_or_skip()
    assert pairs
    for html_path, book_path in pairs:
        tree = pe.parse_file(html_path)
        book = pg.parse_book(book_path)
        results = vfy.cross_check(tree, book, YEAR_KEY)
        mismatches = [r for r in results if not r.ok]
        assert results, f"{html_path.name}: no matching GUIDs compared"
        assert mismatches == [], f"{html_path.name}: {mismatches}"


@pytest.mark.local_samples
def test_real_corpus_richest_book_lots_reproduce_capgain_sheet():
    """FY24-25 lot reconstruction for the richest real book (the `richest` line of the local config)
    must reproduce the books' own gain lines: every sale reconciles, one scrip carries two
    lots (one a loss, one a gain), and the gains summed per holding term equal the booked gains for
    the sales of that term. Nothing here names a scrip or states an amount: the control figures are
    read from the book at run time."""
    richest, why = rcc.need_richest()
    if richest is None:
        pytest.skip(why)
    book = pg.parse_book(richest)
    recs = lots.reconstruct_lots(book, YEAR_KEY)
    assert len(recs) == 3
    assert all(r.ok for r in recs)

    # The scrip with two lots is found by grouping, not by name.
    by_scrip: dict[str, list] = {}
    for lot in lots.all_lots(recs):
        by_scrip.setdefault(lot.scrip, []).append(lot)
    two_lot_scrips = [v for v in by_scrip.values() if len(v) == 2]
    assert len(two_lot_scrips) == 1
    pair = two_lot_scrips[0]
    assert any(lot.gain < 0 for lot in pair)  # one loss lot
    assert any(lot.gain > 0 for lot in pair)  # one gain lot

    # Control lines: the book's booked gain per sale, summed by holding term,
    # against the reconstructed lots' gains for the same sales.
    booked = {"LT": 0.0, "ST": 0.0}
    rebuilt = {"LT": 0.0, "ST": 0.0}
    for r in recs:
        terms = {lots._term(lot.buy_date, lot.sale_date) for lot in r.lots if lot.buy_date}
        assert len(terms) == 1  # every sale here is single-term
        term = terms.pop()
        booked[term] += r.booked_gain
        rebuilt[term] += sum(lot.gain for lot in r.lots)
    for term in ("LT", "ST"):
        assert rebuilt[term] == pytest.approx(booked[term], abs=0.01)
    assert any(booked.values())  # the control is not vacuous


@pytest.mark.local_samples
def test_real_corpus_no_unattributed_or_fifo_flags():
    pairs = _real_pairs_or_skip()
    for _, book_path in pairs:
        book = pg.parse_book(book_path)
        recs = lots.reconstruct_lots(book, YEAR_KEY)
        for lot in lots.all_lots(recs):
            assert lot.attribution == "matched", f"{book_path.name}: {lot.scrip} unattributed"
            assert lot.fifo_flag is None, f"{book_path.name}: {lot.scrip} FIFO flag"
