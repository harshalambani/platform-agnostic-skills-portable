"""
tests/skill_itr_workbook/test_foreign_dividend_os.py -- foreign-broker
dividend data (parse_foreign.py's DividendData, produced from a broker's
"consolidated tax report") wired into OtherSourcesSchedule and the 234C
quarterly-instalment buckets.

Design invariants under test (D1-D4 of the foreign-dividend design):
  D1: foreign dividends live on their OWN fields and never touch
      dividend_gross / taxable_total (the book may already tag the same
      receipts as OS_DIVIDEND -- summing them in would double-count).
  D2: quarter-window mapping is LABEL-driven (parses the period's end date
      and resolves it through quarters.bucket_index), never positional.
  D3: a None/placeholder bucket must never turn into an invented 0.0, and a
      series that is entirely None must give a None gross, not 0.0.
  D4: silence is loud -- an unrecognised series name, or a report that
      parses but yields zero quarterly buckets, must produce a warning.

All fixtures here are built in-memory (synthetic, invented amounts/labels).
No real financial data, and nothing under Data/ is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"

for p in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import parse_foreign as pf  # noqa: E402
import quarters as quarters_engine  # noqa: E402
import schedules as sch  # noqa: E402

ORDINARY = "Dividend Income"
DEEMED = "Dividend u/s 2(22)(f)"


def _empty_report(dividend: pf.DividendData, financial_year: str | None = "2025-26") -> pf.ForeignIncomeReport:
    """Build a minimal, otherwise-empty ForeignIncomeReport carrying only
    the Dividend sheet under test -- every other section defaults to its
    all-absent shape, which is all build_other_sources looks at besides
    .dividend and .warnings."""
    return pf.ForeignIncomeReport(
        source_path="<in-memory>",
        financial_year=financial_year,
        report_as_on=None,
        schedule_fa=pf.ScheduleFAData(),
        schedule_fsi=pf.ScheduleFSIData(),
        form_67=pf.Form67Data(),
        schedule_tr=pf.ScheduleTRData(),
        dividend=dividend,
        interest=pf.InterestData(),
    )


def _build_os(foreign_report=None, foreign_dividends_in_book: bool = True):
    """Default `foreign_dividends_in_book=True` here (opposite of the
    production default) so every D1-D4 test above -- written against #196's
    original "always excluded" behaviour -- keeps exercising the excluded
    path unchanged. Tests of the new flag-false inclusion behaviour pass
    `foreign_dividends_in_book=False` explicitly."""
    return sch.build_other_sources(
        {}, {}, book=None, year_key=None, rules=None, as26_data=None,
        foreign_report=foreign_report, foreign_dividends_in_book=foreign_dividends_in_book,
    )


# ---------------------------------------------------------------------------
# D1: separate fields, never conflated, never summed into the taxable total
# ---------------------------------------------------------------------------

def test_both_series_land_in_their_own_fields_never_conflated():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 100.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 200.0, series=ORDINARY),
        pf.QuarterlyBucket("1-Apr to 15-Jun", 5.0, series=DEEMED),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 7.0, series=DEEMED),
    ]
    dividend = pf.DividendData(total=312.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report)

    assert os_.foreign_dividend_source == "broker report"
    assert os_.foreign_dividend_gross == pytest.approx(300.0)
    assert os_.foreign_deemed_dividend_gross == pytest.approx(12.0)
    # Never conflated: the deemed total must not leak into the ordinary one.
    assert os_.foreign_dividend_gross != os_.foreign_deemed_dividend_gross


def test_foreign_dividends_in_book_true_does_not_change_dividend_gross_or_taxable_total():
    """flag=True (book already records the broker's activity): byte-identical
    to #196's original merged/excluded behaviour."""
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 100.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 200.0, series=ORDINARY),
    ]
    dividend = pf.DividendData(total=300.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_without = _build_os(None, foreign_dividends_in_book=True)
    os_with = _build_os(report, foreign_dividends_in_book=True)

    assert os_with.dividend_gross == os_without.dividend_gross
    assert os_with.taxable_total == os_without.taxable_total
    assert os_with.dividend_quarters == os_without.dividend_quarters
    assert os_with.foreign_dividends_in_book is True
    # Sanity: the foreign figure is real and non-zero, so this isn't a
    # trivially-true comparison of two zeros.
    assert os_with.foreign_dividend_gross == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# T2/T3: foreign_dividends_in_book=False (new default) -- inclusion
# ---------------------------------------------------------------------------

def test_foreign_dividends_in_book_false_adds_ordinary_series_into_totals():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 100.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 200.0, series=ORDINARY),
        pf.QuarterlyBucket("1-Apr to 15-Jun", 5.0, series=DEEMED),
    ]
    dividend = pf.DividendData(total=305.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_without = _build_os(None, foreign_dividends_in_book=False)
    os_with = _build_os(report, foreign_dividends_in_book=False)

    assert os_with.foreign_dividends_in_book is False
    assert os_with.dividend_gross == pytest.approx(os_without.dividend_gross + 300.0)
    assert os_with.taxable_total == pytest.approx(os_without.taxable_total + 300.0)
    assert os_with.dividend_quarters[0] == pytest.approx(os_without.dividend_quarters[0] + 100.0)
    assert os_with.dividend_quarters[1] == pytest.approx(os_without.dividend_quarters[1] + 200.0)
    # Remaining quarters (no foreign activity) are untouched.
    assert os_with.dividend_quarters[2:] == os_without.dividend_quarters[2:]
    # Deemed series never reaches the total under either flag value.
    assert os_with.dividend_gross != pytest.approx(os_without.dividend_gross + 305.0)
    # Detail lines stay populated exactly as under flag=True.
    assert os_with.foreign_dividend_gross == pytest.approx(300.0)
    assert os_with.foreign_deemed_dividend_gross == pytest.approx(5.0)
    assert os_with.foreign_dividend_quarters == [100.0, 200.0, None, None, None]


def test_deemed_series_never_reaches_total_or_buckets_under_either_flag():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 100.0, series=ORDINARY),
        pf.QuarterlyBucket("1-Apr to 15-Jun", 9999.0, series=DEEMED),
    ]
    dividend = pf.DividendData(total=10099.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    for flag in (True, False):
        os_ = _build_os(report, foreign_dividends_in_book=flag)
        assert os_.dividend_gross != pytest.approx(9999.0)
        assert 9999.0 not in os_.dividend_quarters
        # Deemed detail is still populated either way.
        assert os_.foreign_deemed_dividend_gross == pytest.approx(9999.0)


def test_none_foreign_quarter_leaves_book_bucket_unchanged_when_included():
    """T3: a None/placeholder foreign quarter must not zero-coerce or
    otherwise disturb a real book bucket when merged in."""
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 200.0, series=ORDINARY),
    ]
    dividend = pf.DividendData(total=200.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report, foreign_dividends_in_book=False)

    # Book bucket for Q1 was 0.0 (no book supplied) and the foreign Q1 is
    # None -- must stay exactly 0.0, never become NaN/None/etc.
    assert os_.dividend_quarters[0] == 0.0
    assert os_.dividend_quarters[1] == pytest.approx(200.0)


def test_foreign_dividend_gross_none_leaves_total_unchanged_and_numeric():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", None, flag="placeholder:-", series=ORDINARY),
    ]
    dividend = pf.DividendData(total=None, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_without = _build_os(None, foreign_dividends_in_book=False)
    os_ = _build_os(report, foreign_dividends_in_book=False)

    assert os_.foreign_dividend_gross is None
    assert os_.dividend_gross == os_without.dividend_gross
    assert os_.dividend_gross is not None
    assert os_.taxable_total is not None


def test_no_foreign_report_supplied_unchanged_under_both_flags():
    for flag in (True, False):
        os_default = sch.OtherSourcesSchedule()
        os_ = _build_os(None, foreign_dividends_in_book=flag)
        assert os_.dividend_gross == os_default.dividend_gross
        assert os_.taxable_total == os_default.taxable_total
        assert os_.dividend_quarters == os_default.dividend_quarters
        assert os_.foreign_dividends_in_book is flag


def test_detail_lines_identical_under_both_flag_values():
    """Inclusion changes only the totals/buckets -- the raw detail fields
    themselves must be byte-identical regardless of foreign_dividends_in_book."""
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 100.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 200.0, series=ORDINARY),
        pf.QuarterlyBucket("1-Apr to 15-Jun", 5.0, series=DEEMED),
    ]
    dividend = pf.DividendData(total=305.0, quarterly=quarterly, financial_year="2025-26")

    os_true = _build_os(_empty_report(dividend), foreign_dividends_in_book=True)
    os_false = _build_os(_empty_report(dividend), foreign_dividends_in_book=False)

    assert os_true.foreign_dividend_gross == os_false.foreign_dividend_gross
    assert os_true.foreign_dividend_quarters == os_false.foreign_dividend_quarters
    assert os_true.foreign_dividend_quarter_flags == os_false.foreign_dividend_quarter_flags
    assert os_true.foreign_deemed_dividend_gross == os_false.foreign_deemed_dividend_gross
    assert os_true.foreign_deemed_dividend_quarters == os_false.foreign_deemed_dividend_quarters
    assert os_true.foreign_dividend_source == os_false.foreign_dividend_source


# ---------------------------------------------------------------------------
# Coordinator review of #198, defect 2: dividend_quarters_source must say so
# when a fold actually happened, and stay exactly "book"/"26AS" otherwise.
# ---------------------------------------------------------------------------

def test_dividend_quarters_source_gains_broker_suffix_when_folded():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 100.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 200.0, series=ORDINARY),
    ]
    dividend = pf.DividendData(total=300.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report, foreign_dividends_in_book=False)

    assert os_.dividend_quarters_source == "book + broker report"


def test_dividend_quarters_source_stays_book_when_flag_true_excludes_fold():
    """Same report, but flag=True means nothing is actually merged into
    div_quarters -- the source label must not claim a fold that didn't
    happen."""
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 100.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 200.0, series=ORDINARY),
    ]
    dividend = pf.DividendData(total=300.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report, foreign_dividends_in_book=True)

    assert os_.dividend_quarters_source == "book"


def test_dividend_quarters_source_stays_book_when_no_foreign_report():
    os_ = _build_os(None, foreign_dividends_in_book=False)
    assert os_.dividend_quarters_source == "book"


def test_dividend_quarters_source_stays_book_when_all_foreign_quarters_none():
    """flag=False and a report IS supplied, but every foreign quarter is a
    None/placeholder -- nothing numeric actually got folded into
    div_quarters, so the source label must not claim otherwise."""
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Sep to 15-Dec", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Dec to 15-Mar", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Mar to 31-Mar", None, flag="placeholder:-", series=ORDINARY),
    ]
    dividend = pf.DividendData(total=None, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report, foreign_dividends_in_book=False)

    assert os_.dividend_quarters_source == "book"


# ---------------------------------------------------------------------------
# T1: per-AY override resolution (mirrors audit_case_by_ay's resolution
# style exactly -- see EntityProfile.foreign_dividends_in_book_by_ay).
# ---------------------------------------------------------------------------

def test_per_ay_override_beats_entity_default_both_directions():
    import configs

    entity_default_false = configs.EntityProfile(
        key="SYN-TEST", name="Synthetic", pan="AAAAA0000A", status="Individual",
        foreign_dividends_in_book=False,
        foreign_dividends_in_book_by_ay={"2026-27": True},
    )
    # AY with an override: overridden value wins.
    resolved_2026_27 = entity_default_false.foreign_dividends_in_book_by_ay.get(
        "2026-27", entity_default_false.foreign_dividends_in_book
    )
    assert resolved_2026_27 is True
    # AY without an override: entity-level default wins.
    resolved_2025_26 = entity_default_false.foreign_dividends_in_book_by_ay.get(
        "2025-26", entity_default_false.foreign_dividends_in_book
    )
    assert resolved_2025_26 is False

    entity_default_true = configs.EntityProfile(
        key="SYN-TEST", name="Synthetic", pan="AAAAA0000A", status="Individual",
        foreign_dividends_in_book=True,
        foreign_dividends_in_book_by_ay={"2025-26": False},
    )
    resolved_override = entity_default_true.foreign_dividends_in_book_by_ay.get(
        "2025-26", entity_default_true.foreign_dividends_in_book
    )
    assert resolved_override is False
    resolved_default = entity_default_true.foreign_dividends_in_book_by_ay.get(
        "2026-27", entity_default_true.foreign_dividends_in_book
    )
    assert resolved_default is True


# ---------------------------------------------------------------------------
# T6: advance-tax projection picks up included foreign dividends
# ---------------------------------------------------------------------------

def test_advance_tax_from_book_picks_up_included_foreign_dividend():
    import advance_tax

    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 100.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 200.0, series=ORDINARY),
    ]
    dividend = pf.DividendData(total=300.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    est_without = advance_tax.from_book(
        {}, {}, book=None, fy="2025-26", rules=None, entity_name="Synthetic",
        regime="new", status="Individual", dob=None,
        foreign_report=None, foreign_dividends_in_book=False,
    )
    est_with = advance_tax.from_book(
        {}, {}, book=None, fy="2025-26", rules=None, entity_name="Synthetic",
        regime="new", status="Individual", dob=None,
        foreign_report=report, foreign_dividends_in_book=False,
    )

    assert est_with.other_sources_other == pytest.approx(
        est_without.other_sources_other + 300.0
    )

    # flag=True: excluded, matches the no-report baseline.
    est_excluded = advance_tax.from_book(
        {}, {}, book=None, fy="2025-26", rules=None, entity_name="Synthetic",
        regime="new", status="Individual", dob=None,
        foreign_report=report, foreign_dividends_in_book=True,
    )
    assert est_excluded.other_sources_other == pytest.approx(est_without.other_sources_other)


# ---------------------------------------------------------------------------
# D2: label-driven window mapping, including out-of-order periods
# ---------------------------------------------------------------------------

def test_label_driven_mapping_puts_each_period_in_the_right_234c_window():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 10.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 20.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Sep to 15-Dec", 30.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Dec to 15-Mar", 40.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Mar to 31-Mar", 50.0, series=ORDINARY),
    ]
    dividend = pf.DividendData(total=150.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report)

    assert os_.foreign_dividend_quarters == [10.0, 20.0, 30.0, 40.0, 50.0]
    assert os_.foreign_dividend_quarter_flags == [None, None, None, None, None]


def test_label_driven_mapping_is_order_independent():
    # Same periods as above but supplied out of order -- the mapping must
    # be driven by parsing each label's own date, not by list position.
    quarterly = [
        pf.QuarterlyBucket("16-Mar to 31-Mar", 50.0, series=ORDINARY),
        pf.QuarterlyBucket("1-Apr to 15-Jun", 10.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Dec to 15-Mar", 40.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Sep to 15-Dec", 30.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 20.0, series=ORDINARY),
    ]
    dividend = pf.DividendData(total=150.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report)

    assert os_.foreign_dividend_quarters == [10.0, 20.0, 30.0, 40.0, 50.0]
    # Cross-check against the single canonical window resolver directly,
    # so this test would fail if quarters.bucket_index's thresholds ever
    # move without schedules.py's mapping following.
    assert quarters_engine.bucket_index(__import__("datetime").date(2025, 6, 15), 2025) == 0
    assert quarters_engine.bucket_index(__import__("datetime").date(2025, 9, 15), 2025) == 1


# ---------------------------------------------------------------------------
# D2/D3: unparseable labels warn and leave the window None, never a guess
# ---------------------------------------------------------------------------

def test_unparseable_period_label_warns_and_leaves_window_none():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 10.0, series=ORDINARY),
        pf.QuarterlyBucket("Some Garbled Header", 999.0, series=ORDINARY),
    ]
    dividend = pf.DividendData(total=1009.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report)

    # The parseable one still lands correctly...
    assert os_.foreign_dividend_quarters[0] == 10.0
    # ...but the unparseable one is nowhere in the 5 windows -- not guessed
    # into any bucket, and not silently dropped from having a warning.
    assert 999.0 not in os_.foreign_dividend_quarters
    assert any(
        "Some Garbled Header" in w and "could not parse" in w
        for w in report.warnings
    ), report.warnings


# ---------------------------------------------------------------------------
# Collision: two periods of the same series resolving to the same 234C
# window must never let the second silently clobber the first.
# ---------------------------------------------------------------------------

def test_two_periods_colliding_on_same_window_are_summed_not_overwritten():
    quarterly = [
        # Two distinct labels that both parse to an end-date inside the
        # same Q1 window (1-Apr to 15-Jun) -- e.g. a vendor re-issuing an
        # overlapping/corrected period rather than a clean partition.
        pf.QuarterlyBucket("1-Apr to 15-Jun", 10.0, series=ORDINARY),
        pf.QuarterlyBucket("2-Apr to 15-Jun", 5.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 20.0, series=ORDINARY),
    ]
    dividend = pf.DividendData(total=35.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report)

    # Neither value vanished -- they were summed into the shared window.
    assert os_.foreign_dividend_quarters[0] == pytest.approx(15.0)
    assert os_.foreign_dividend_quarters[1] == 20.0
    # The gross total (computed independently from the raw series, not the
    # mapped windows) still reflects both periods either way.
    assert os_.foreign_dividend_gross == pytest.approx(35.0)
    # And the collision is visible, naming both periods and the window.
    assert any(
        "1-Apr to 15-Jun" in w and "2-Apr to 15-Jun" in w and "Q1" in w
        for w in report.warnings
    ), report.warnings


def test_collision_with_one_placeholder_side_keeps_the_numeric_value():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("2-Apr to 15-Jun", 5.0, series=ORDINARY),
    ]
    dividend = pf.DividendData(total=5.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report)

    # The numeric side must win, not be lost because a placeholder arrived
    # for the same window.
    assert os_.foreign_dividend_quarters[0] == 5.0
    assert any(
        "1-Apr to 15-Jun" in w and "2-Apr to 15-Jun" in w and "Q1" in w
        for w in report.warnings
    ), report.warnings


# ---------------------------------------------------------------------------
# D3: None/placeholder buckets stay None, never become an invented 0.0
# ---------------------------------------------------------------------------

def test_none_placeholder_bucket_stays_none_not_zero():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", 200.0, series=ORDINARY),
        pf.QuarterlyBucket("16-Sep to 15-Dec", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Dec to 15-Mar", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Mar to 31-Mar", None, flag="placeholder:-", series=ORDINARY),
    ]
    dividend = pf.DividendData(total=200.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report)

    assert os_.foreign_dividend_quarters[0] is None
    assert os_.foreign_dividend_quarters[0] != 0.0
    assert os_.foreign_dividend_quarter_flags[0] == "placeholder:-"
    assert os_.foreign_dividend_quarters[1] == 200.0
    # Gross is still knowable even though most buckets are placeholders.
    assert os_.foreign_dividend_gross == pytest.approx(200.0)


def test_all_none_series_gives_none_gross_not_zero():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Jun to 15-Sep", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Sep to 15-Dec", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Dec to 15-Mar", None, flag="placeholder:-", series=ORDINARY),
        pf.QuarterlyBucket("16-Mar to 31-Mar", None, flag="placeholder:-", series=ORDINARY),
    ]
    dividend = pf.DividendData(total=None, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report)

    assert os_.foreign_dividend_gross is None
    assert os_.foreign_dividend_gross != 0.0
    assert os_.foreign_dividend_quarters == [None] * 5


# ---------------------------------------------------------------------------
# D4: warn loudly on silence
# ---------------------------------------------------------------------------

def test_unrecognised_series_name_warns():
    quarterly = [
        pf.QuarterlyBucket("1-Apr to 15-Jun", 10.0, series=ORDINARY),
        pf.QuarterlyBucket("1-Apr to 15-Jun", 999.0, series="Some New Broker Category"),
    ]
    dividend = pf.DividendData(total=1009.0, quarterly=quarterly, financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report)

    assert os_.foreign_dividend_quarters[0] == 10.0
    assert any(
        "Some New Broker Category" in w and "unrecognised" in w
        for w in report.warnings
    ), report.warnings


def test_parsed_report_with_zero_quarterly_buckets_warns():
    dividend = pf.DividendData(total=0.0, quarterly=[], financial_year="2025-26")
    report = _empty_report(dividend)

    os_ = _build_os(report)

    assert os_.foreign_dividend_source is None
    assert os_.foreign_dividend_gross is None
    assert any(
        "zero quarterly dividend buckets" in w for w in report.warnings
    ), report.warnings


# ---------------------------------------------------------------------------
# Absent-input invariant: no foreign report -> byte-identical to today
# ---------------------------------------------------------------------------

def test_no_foreign_report_supplied_matches_default_absent_state():
    os_default = sch.OtherSourcesSchedule()
    os_no_kw = _build_os()          # foreign_report omitted entirely
    os_explicit_none = _build_os(None)   # foreign_report=None explicitly

    for os_ in (os_no_kw, os_explicit_none):
        assert os_.foreign_dividend_gross is None
        assert os_.foreign_dividend_quarters == [None] * 5
        assert os_.foreign_dividend_quarter_flags == [None] * 5
        assert os_.foreign_deemed_dividend_gross is None
        assert os_.foreign_deemed_dividend_quarters == [None] * 5
        assert os_.foreign_dividend_source is None

    # And the whole new-field surface matches a bare, freshly-constructed
    # OtherSourcesSchedule() -- i.e. the "absent" state defined by the
    # dataclass defaults themselves, not just this test's expectations.
    assert os_no_kw.foreign_dividend_gross == os_default.foreign_dividend_gross
    assert os_no_kw.foreign_dividend_quarters == os_default.foreign_dividend_quarters
    assert os_no_kw.foreign_dividend_quarter_flags == os_default.foreign_dividend_quarter_flags
    assert os_no_kw.foreign_deemed_dividend_gross == os_default.foreign_deemed_dividend_gross
    assert os_no_kw.foreign_deemed_dividend_quarters == os_default.foreign_deemed_dividend_quarters
    assert os_no_kw.foreign_dividend_source == os_default.foreign_dividend_source
