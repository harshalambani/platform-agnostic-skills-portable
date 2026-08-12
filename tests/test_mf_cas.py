"""
tests/test_mf_cas.py -- agents.skill_mf_cas (MF CAS: CAMS/KFintech
Consolidated Account Statement parser + FIFO capital-gains derivation).
Deterministic, no LLM, no network.

Synthetic-only fixtures: every folio/scheme/AMC/ISIN below is invented for
this test file. No real CAS PDF is ever generated or read here -- per the
skill's hard architectural split, everything except the password-error test
exercises the PURE parser/lots functions with synthetic text lines.

Covers:
  * multi-lot FIFO disposal splitting (one row per consumed lot)
  * partial-lot disposal (a lot only partially consumed)
  * a pre-2018-02-01 buy raises the grandfathering flag
  * a units-reconciliation breach is reported, not silently absorbed
  * a disposal reaching into a nonzero Opening Unit Balance is flagged
    "unattributed -- review", never guessed
  * switch-in / switch-out classification
  * a wrong CAS PDF password surfaces password_error_message() without the
    password ever appearing in the raised error
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_mf_cas import agent as mf_agent  # noqa: E402
from agents.skill_mf_cas import lots  # noqa: E402
from agents.skill_mf_cas.parser import classify_row, parse_cas_text  # noqa: E402


def _folio_block(
    folio="TEST/001", amc="Test AMC Mutual Fund", isin="INF000A00123",
    scheme="Test Equity Growth Fund", rta="Test RTA", scheme_type="EQUITY",
    opening="0.0000", closing="0.0000", txn_lines=(), rta_gain=None,
):
    lines = [
        f"Folio No: {folio}",
        f"AMC: {amc}",
        f"Scheme: {scheme} (ISIN: {isin})",
        f"Registrar: {rta}",
        f"Scheme Type: {scheme_type}",
        f"Opening Unit Balance: {opening}",
        *txn_lines,
        f"Closing Unit Balance: {closing}",
    ]
    if rta_gain is not None:
        lines.append(f"RTA Realised Gain: {rta_gain}")
    return lines


def _txn(date, desc, amount, units, nav, balance):
    return f"{date} {desc} {amount} {units} {nav} {balance}"


# ---------------------------------------------------------------------------
# classify_row
# ---------------------------------------------------------------------------

def test_classify_row_switch_in_is_acquisition():
    assert classify_row("Switch in from Test Debt Fund", 50.0) == "ACQUISITION"


def test_classify_row_switch_out_is_disposal():
    assert classify_row("Switch out to Test Debt Fund", -50.0) == "DISPOSAL"


def test_classify_row_zero_units_is_neither():
    assert classify_row("IDCW Payout", 0.0) == "NEITHER"


def test_classify_row_dividend_reinvestment_is_acquisition():
    assert classify_row("Dividend Reinvestment", 5.25) == "ACQUISITION"


# ---------------------------------------------------------------------------
# parse_cas_text
# ---------------------------------------------------------------------------

def test_parse_basic_block_fields():
    lines = _folio_block(
        txn_lines=[_txn("01-Apr-2020", "Purchase", "10000.00", "100.0000", "100.00", "100.0000")],
        closing="100.0000",
    )
    blocks = parse_cas_text(lines)
    assert len(blocks) == 1
    b = blocks[0]
    assert b.folio == "TEST/001"
    assert b.isin == "INF000A00123"
    assert b.scheme_type == "EQUITY"
    assert len(b.transactions) == 1
    assert b.transactions[0].kind == "ACQUISITION"


def test_parse_never_captures_pan_or_name_lines():
    lines = [
        "Investor Name: Some Person",
        "PAN: ABCDE1234X",
        "Email: someone@example.com",
        "Mobile: 9999999999",
        *_folio_block(txn_lines=[
            _txn("01-Apr-2020", "Purchase", "10000.00", "100.0000", "100.00", "100.0000")
        ], closing="100.0000"),
    ]
    blocks = parse_cas_text(lines)
    assert len(blocks) == 1
    # None of the PII lines produced any field anywhere in the parsed block.
    dumped = repr(blocks[0])
    assert "Some Person" not in dumped
    assert "ABCDE1234X" not in dumped
    assert "someone@example.com" not in dumped
    assert "9999999999" not in dumped


# ---------------------------------------------------------------------------
# FIFO derivation
# ---------------------------------------------------------------------------

def test_multi_lot_disposal_splits_into_one_row_per_lot():
    lines = _folio_block(
        txn_lines=[
            _txn("01-Apr-2020", "Purchase", "10000.00", "100.0000", "100.00", "100.0000"),
            _txn("01-May-2020", "Purchase", "11000.00", "100.0000", "110.00", "200.0000"),
            # Redeem 150 units -- must draw 100 from lot 1, 50 from lot 2.
            _txn("01-Jun-2020", "Redemption", "-18000.00", "-150.0000", "120.00", "50.0000"),
        ],
        closing="50.0000",
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])

    assert len(recon.disposal_lots) == 2
    units_taken = sorted(dl.units for dl in recon.disposal_lots)
    assert units_taken == [50.0, 100.0]
    # FIFO order: first lot (100 units @ NAV 100) consumed first and fully.
    first = next(dl for dl in recon.disposal_lots if dl.units == 100.0)
    assert first.buy_nav == 100.0
    second = next(dl for dl in recon.disposal_lots if dl.units == 50.0)
    assert second.buy_nav == 110.0
    assert recon.units_ok
    assert recon.matched_vs_disposed_ok


def test_partial_lot_disposal_leaves_remainder_in_lot():
    lines = _folio_block(
        txn_lines=[
            _txn("01-Apr-2020", "Purchase", "10000.00", "100.0000", "100.00", "100.0000"),
            # Redeem only 40 of the 100 units in the single lot.
            _txn("01-Jun-2020", "Redemption", "-4800.00", "-40.0000", "120.00", "60.0000"),
        ],
        closing="60.0000",
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])

    assert len(recon.disposal_lots) == 1
    dl = recon.disposal_lots[0]
    assert dl.units == 40.0
    assert dl.buy_cost == pytest.approx(4000.00)  # 40/100 * 10000
    assert dl.sale_proceeds == pytest.approx(4800.00)
    assert dl.gain == pytest.approx(800.00)
    assert recon.units_ok


def test_grandfathering_flag_on_pre_2018_buy():
    lines = _folio_block(
        txn_lines=[
            _txn("15-Jan-2017", "Purchase", "5000.00", "50.0000", "100.00", "50.0000"),
            _txn("01-Jun-2020", "Redemption", "-6000.00", "-50.0000", "120.00", "0.0000"),
        ],
        closing="0.0000",
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])

    assert len(recon.disposal_lots) == 1
    assert lots.GRANDFATHER_FLAG in recon.disposal_lots[0].flags


def test_post_2018_buy_has_no_grandfathering_flag():
    lines = _folio_block(
        txn_lines=[
            _txn("15-Mar-2018", "Purchase", "5000.00", "50.0000", "100.00", "50.0000"),
            _txn("01-Jun-2020", "Redemption", "-6000.00", "-50.0000", "120.00", "0.0000"),
        ],
        closing="0.0000",
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])

    assert lots.GRANDFATHER_FLAG not in recon.disposal_lots[0].flags


def test_units_reconciliation_breach_is_reported():
    # Opening 0 + acquired 100 - disposed 40 = 60, but statement claims 55.
    lines = _folio_block(
        txn_lines=[
            _txn("01-Apr-2020", "Purchase", "10000.00", "100.0000", "100.00", "100.0000"),
            _txn("01-Jun-2020", "Redemption", "-4800.00", "-40.0000", "120.00", "60.0000"),
        ],
        closing="55.0000",  # deliberately wrong
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])

    assert not recon.units_ok
    assert recon.units_diff == pytest.approx(5.0)


def test_unattributed_flag_when_disposal_draws_on_opening_balance():
    # Opening balance of 30 units with no visible acquisition in this
    # statement's window; a 30-unit redemption must draw entirely on it.
    lines = _folio_block(
        opening="30.0000",
        txn_lines=[
            _txn("01-Jun-2020", "Redemption", "-3600.00", "-30.0000", "120.00", "0.0000"),
        ],
        closing="0.0000",
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])

    assert len(recon.disposal_lots) == 1
    dl = recon.disposal_lots[0]
    assert lots.UNATTRIBUTED in dl.flags
    assert dl.buy_date is None
    assert dl.buy_cost is None
    assert dl.gain is None  # never guessed
    assert recon.units_ok  # 30 opening - 30 disposed == 0 closing, still balances


def test_unattributed_flag_never_fabricates_cost_or_date():
    lines = _folio_block(
        opening="30.0000",
        txn_lines=[_txn("01-Jun-2020", "Redemption", "-3600.00", "-30.0000", "120.00", "0.0000")],
        closing="0.0000",
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])
    dl = recon.disposal_lots[0]
    # Sale proceeds/units are known from the statement row itself, but
    # anything requiring the (unknown) buy side must stay None, not guessed.
    assert dl.sale_proceeds == pytest.approx(3600.00)
    assert dl.units == 30.0
    assert dl.buy_nav is None


def test_rta_gain_variance_reported_when_present():
    lines = _folio_block(
        txn_lines=[
            _txn("01-Apr-2020", "Purchase", "10000.00", "100.0000", "100.00", "100.0000"),
            _txn("01-Jun-2020", "Redemption", "-12000.00", "-100.0000", "120.00", "0.0000"),
        ],
        closing="0.0000",
        rta_gain="1500.00",  # FIFO derives 2000.00 -- deliberate variance
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])

    assert recon.rta_gain_reported == pytest.approx(1500.00)
    assert recon.rta_gain_derived == pytest.approx(2000.00)
    assert recon.rta_gain_variance == pytest.approx(500.00)


def test_no_rta_gain_line_leaves_reported_as_none():
    lines = _folio_block(
        txn_lines=[_txn("01-Apr-2020", "Purchase", "10000.00", "100.0000", "100.00", "100.0000")],
        closing="100.0000",
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])
    assert recon.rta_gain_reported is None
    assert recon.rta_gain_variance is None


# ---------------------------------------------------------------------------
# Review-finding regression tests (PR #176 follow-up: matched_vs_disposed_ok
# was decorative, rta_gain_derived treated unknown gain as zero, and a
# DISPOSAL row could silently vanish if its sign guarantee ever broke).
# ---------------------------------------------------------------------------

def test_shortfall_disposal_makes_matched_vs_disposed_ok_false():
    # Only 50 units ever exist (one 50-unit lot), but the statement's own
    # redemption row claims 80 units sold -- a 30-unit phantom shortfall
    # that exceeds every available lot, including the (absent) opening
    # balance. Under the OLD code, the phantom top-up used to cover this
    # shortfall was folded straight into total_matched_units, which forced
    # matched_vs_disposed_ok to True for every possible input -- the
    # invariant could never fail. It must now be able to report False.
    lines = _folio_block(
        txn_lines=[
            _txn("01-Apr-2020", "Purchase", "5000.00", "50.0000", "100.00", "50.0000"),
            _txn("01-Jun-2020", "Redemption", "-9600.00", "-80.0000", "120.00", "-30.0000"),
        ],
        closing="-30.0000",
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])

    assert recon.unattributed_shortfall_units == pytest.approx(30.0)
    assert recon.matched_vs_disposed_ok is False


def test_all_unattributed_disposals_cannot_cross_check_rta_gain():
    # Every disposed unit here draws on the unknown-cost OPENING lot, so
    # nothing about this scheme's realised gain is actually derivable.
    # Under the OLD code, rta_gain_derived silently became 0.0 (treating
    # "unknown" as "zero gain"), and rta_gain_variance then read as a real
    # discrepancy against the RTA's stated figure -- a false signal about
    # genuinely undecidable input.
    lines = _folio_block(
        opening="40.0000",
        txn_lines=[
            _txn("01-Jun-2020", "Redemption", "-4800.00", "-40.0000", "120.00", "0.0000"),
        ],
        closing="0.0000",
        rta_gain="900.00",
    )
    blocks = parse_cas_text(lines)
    recon = lots.derive_scheme(blocks[0])

    assert lots.UNATTRIBUTED in recon.disposal_lots[0].flags
    assert recon.rta_gain_derived is None
    assert recon.rta_gain_variance is None
    assert recon.rta_gain_note == lots.CANNOT_CROSS_CHECK

    # And this must reach the rendered workbook -- a blank/absent Exceptions
    # row on undecidable input would misread as "no variance found".
    from openpyxl import Workbook

    from agents.skill_mf_cas import excel_writer

    wb = Workbook()
    wb.remove(wb.active)
    excel_writer._write_exceptions_sheet(wb, [recon])
    ws = wb["Exceptions"]
    details = [ws.cell(row=r, column=4).value for r in range(2, ws.max_row + 1)]
    assert any(lots.CANNOT_CROSS_CHECK in (d or "") for d in details)


def test_disposal_with_nonnegative_units_is_flagged_not_dropped():
    # qty_needed = -txn.units (and the whole matching loop) relies on
    # classify_row only ever returning DISPOSAL for negative units. This
    # test builds a TxnRow directly (bypassing the parser, which today
    # cannot produce this shape) to prove that IF that guarantee ever broke
    # upstream, derive_scheme would not let the row vanish with no lot and
    # no flag -- under the OLD code, a non-negative qty_needed would break
    # the matching loop immediately (remaining_to_consume <= 0) AND fail
    # the shortfall check (remaining_to_consume > 0 also false), so the
    # transaction would disappear silently.
    from datetime import date as _date

    from agents.skill_mf_cas.parser import DISPOSAL, SchemeBlock, TxnRow

    txn = TxnRow(
        date=_date(2020, 6, 1), description="Malformed disposal row",
        amount=-1000.0, units=25.0,  # non-negative units on a DISPOSAL row
        nav=100.0, balance=0.0, kind=DISPOSAL,
    )
    block = SchemeBlock(
        folio="TEST/999", amc="Test AMC", rta="Test RTA",
        scheme_name="Test Fund", isin="INF000A00999", scheme_type="EQUITY",
        opening_units=25.0, closing_units=0.0, transactions=[txn],
    )
    recon = lots.derive_scheme(block)

    flagged = [dl for dl in recon.disposal_lots if lots.INVALID_DISPOSAL_SIGN in dl.flags]
    assert len(flagged) == 1
    assert flagged[0].units == 25.0


def test_switch_out_then_switch_in_different_scheme_each_folio():
    lines = [
        *_folio_block(
            folio="TEST/001", scheme="Source Fund", isin="INF000A00001",
            txn_lines=[
                _txn("01-Apr-2020", "Purchase", "10000.00", "100.0000", "100.00", "100.0000"),
                _txn("01-Jul-2020", "Switch out to Target Fund", "-11000.00", "-100.0000", "110.00", "0.0000"),
            ],
            closing="0.0000",
        ),
        *_folio_block(
            folio="TEST/002", scheme="Target Fund", isin="INF000A00002",
            txn_lines=[
                _txn("01-Jul-2020", "Switch in from Source Fund", "11000.00", "55.0000", "200.00", "55.0000"),
            ],
            closing="55.0000",
        ),
    ]
    blocks = parse_cas_text(lines)
    assert len(blocks) == 2
    source_recon = lots.derive_scheme(blocks[0])
    target_recon = lots.derive_scheme(blocks[1])

    assert len(source_recon.disposal_lots) == 1
    assert source_recon.disposal_lots[0].gain == pytest.approx(1000.00)
    assert target_recon.units_ok
    assert blocks[1].transactions[0].kind == "ACQUISITION"


# ---------------------------------------------------------------------------
# Password handling (agent.py's PDF boundary) -- the password must never
# appear in any raised error.
# ---------------------------------------------------------------------------

def test_wrong_password_error_never_echoes_password(monkeypatch):
    import pdfplumber

    secret = "hunter2-secret-pan"

    def _raise_open(path, password=""):
        raise Exception("Incorrect password")

    monkeypatch.setattr(pdfplumber, "open", _raise_open)

    with pytest.raises(ValueError) as excinfo:
        mf_agent._extract_pdf_text("does-not-matter.pdf", secret)

    message = str(excinfo.value)
    assert secret not in message
    assert "password" in message.lower()


def test_no_extractable_text_fails_loud(monkeypatch, tmp_path):
    import pdfplumber

    class _FakePage:
        def extract_text(self, x_tolerance=1):
            return ""

    class _FakePdf:
        pages = [_FakePage()]

        def close(self):
            pass

    monkeypatch.setattr(pdfplumber, "open", lambda path, password="": _FakePdf())

    with pytest.raises(ValueError) as excinfo:
        mf_agent._extract_pdf_text("does-not-matter.pdf", None)
    assert "No text could be extracted" in str(excinfo.value)


# ---------------------------------------------------------------------------
# End-to-end run() smoke test (writes a real workbook to tmp_path).
# ---------------------------------------------------------------------------

def test_run_end_to_end_writes_workbook_and_csv_siblings(monkeypatch, tmp_path):
    import pdfplumber

    text = "\n".join(_folio_block(
        txn_lines=[
            _txn("01-Apr-2020", "Purchase", "10000.00", "100.0000", "100.00", "100.0000"),
            _txn("01-Jun-2021", "Redemption", "-12000.00", "-100.0000", "120.00", "0.0000"),
        ],
        closing="0.0000",
    ))

    class _FakePage:
        def extract_text(self, x_tolerance=1):
            return text

    class _FakePdf:
        pages = [_FakePage()]

        def close(self):
            pass

    monkeypatch.setattr(pdfplumber, "open", lambda path, password="": _FakePdf())

    cas_path = tmp_path / "fake_cas.pdf"
    cas_path.write_bytes(b"%PDF-1.4 not a real pdf")
    out_path = tmp_path / "out.xlsx"

    summary = mf_agent.run(str(cas_path), str(out_path))

    assert "ERROR" not in summary
    assert out_path.is_file()
    assert (tmp_path / "out_transactions.csv").is_file()
    assert (tmp_path / "out_realised_gains.csv").is_file()
