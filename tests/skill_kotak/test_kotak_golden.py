"""
tests/skill_kotak/test_kotak_golden.py -- Kotak Mahindra Bank golden-family
tests (bank abstraction P4 -- 5th bank onboarding).

Covers, all offline / synthetic-only:
  - Golden canonical rows for the synthetic multi-page PDF (7 columns,
    separate Dr/Cr, DD Mon YYYY dates, Indian-grouped amounts, running
    balance).
  - The Opening Balance pseudo-row is captured but excluded from canonical
    rows (mirrors BoB's convention).
  - Multi-page continuation with NO repeated header on page 2 parses all
    rows.
  - The trailing abbreviation LEGEND page (~14 rows via extract_tables())
    is structurally excluded -- never appears in parsed output.
  - Sweep-in / sweep-out transfers to a linked FD are kept as real rows.
  - BankStatementMeta is fully populated from the synthetic PDF's front
    matter.
  - Password-protected PDF: correct password succeeds; missing/wrong
    password raises a clear, actionable error (never echoing the password).
  - Text-quality rejection: a page with no structural anchors raises, with
    no OCR fallback.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"

import kotak_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_kotak.agent import KotakSkill  # noqa: E402
from agents.skill_kotak.scripts.extract_kotak_statement import extract  # noqa: E402

EXPECTED_ROWS = [
    {
        "Date": "2026-06-03", "Transaction ID": "", "Description": "NEFT SALARY CREDIT-SYNCO",
        "Account": "", "Deposit": "50000.00", "Withdrawal": "0", "Balance": "250000.00",
        "Currency": "INR",
    },
    {
        "Date": "2026-06-05", "Transaction ID": "", "Description": "UPI-SYNMART GROCERY-SYN",
        "Account": "", "Deposit": "0", "Withdrawal": "3000.00", "Balance": "247000.00",
        "Currency": "INR",
    },
    {
        "Date": "2026-06-09", "Transaction ID": "", "Description": "Sweep transfer to FD-SYN0001",
        "Account": "", "Deposit": "0", "Withdrawal": "100000.00", "Balance": "147000.00",
        "Currency": "INR",
    },
    {
        "Date": "2026-06-15", "Transaction ID": "", "Description": "ACH D-SYNTH MF-SIP0001",
        "Account": "", "Deposit": "0", "Withdrawal": "5000.00", "Balance": "142000.00",
        "Currency": "INR",
    },
    {
        "Date": "2026-06-20", "Transaction ID": "", "Description": "Sweep transfer from FD-SYN0001",
        "Account": "", "Deposit": "100000.00", "Withdrawal": "0", "Balance": "242000.00",
        "Currency": "INR",
    },
    {
        "Date": "2026-06-24", "Transaction ID": "", "Description": "UPI-REFUND ORDER-SYNSHOP",
        "Account": "", "Deposit": "500.00", "Withdrawal": "0", "Balance": "242500.00",
        "Currency": "INR",
    },
    {
        "Date": "2026-06-28", "Transaction ID": "654321", "Description": "CC AUTOPAY SI-SYN",
        "Account": "", "Deposit": "0", "Withdrawal": "3500.00", "Balance": "239000.00",
        "Currency": "INR",
    },
]


# ---------------------------------------------------------------------------
# Golden canonical rows
# ---------------------------------------------------------------------------

def test_synthetic_pdf_produces_expected_canonical_rows(tmp_path):
    pdf_path = tmp_path / "syn_kotak.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())

    result = KotakSkill().parse(pdf_path)

    assert result.rows == EXPECTED_ROWS
    assert result.opening_balance == fixture_gen.SYN_OPENING_BALANCE
    assert result.closing_balance == fixture_gen.SYN_CLOSING_BALANCE
    assert result.balance_check.ok is True


def test_synthetic_pdf_populates_bank_statement_meta(tmp_path):
    pdf_path = tmp_path / "syn_kotak.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())

    result = KotakSkill().parse(pdf_path)

    assert result.meta is not None
    assert result.meta.bank_key == "kotak"
    assert result.meta.account_number == fixture_gen.SYN_ACCOUNT_NUMBER
    assert result.meta.period_from == fixture_gen.SYN_PERIOD_FROM
    assert result.meta.period_to == fixture_gen.SYN_PERIOD_TO
    assert result.meta.source_format == "pdf"
    assert result.meta.fidelity == "exact"
    assert result.meta.password_used is False


def test_synthetic_pdf_detected_with_high_confidence(tmp_path):
    pdf_path = tmp_path / "syn_kotak.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())

    assert KotakSkill().detect(pdf_path) == 0.95


# ---------------------------------------------------------------------------
# Opening-balance pseudo-row -- captured by the extractor, excluded from
# canonical rows.
# ---------------------------------------------------------------------------

def test_opening_balance_row_captured_by_extractor_but_excluded_from_canonical(tmp_path):
    pdf_path = tmp_path / "syn_kotak.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())

    native_rows = extract(pdf_path)
    opening_rows = [r for r in native_rows if r.is_opening_balance]
    assert len(opening_rows) == 1
    assert opening_rows[0].balance == "200000.00"

    result = KotakSkill().parse(pdf_path)
    assert all("opening balance" not in r["Description"].lower() for r in result.rows)


# ---------------------------------------------------------------------------
# Multi-page continuation, no repeated header
# ---------------------------------------------------------------------------

def test_multipage_pdf_without_repeated_header_parses_all_rows(tmp_path):
    pdf_path = tmp_path / "syn_kotak.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())

    rows = extract(pdf_path)
    assert len(rows) == 8  # opening balance + 7 transactions


# ---------------------------------------------------------------------------
# Trailing legend page must never leak into parsed output
# ---------------------------------------------------------------------------

def test_legend_rows_excluded_from_parsed_output(tmp_path):
    pdf_path = tmp_path / "syn_kotak.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())

    rows = extract(pdf_path)
    legend_codes = {code.lower() for code, _meaning in fixture_gen.LEGEND_ROWS}
    legend_meanings = [meaning.lower() for _code, meaning in fixture_gen.LEGEND_ROWS]

    for row in rows:
        desc = row.description.lower()
        assert desc not in legend_codes
        assert not any(desc == meaning for meaning in legend_meanings)
    # And the legend table's own row count (14) must not have been folded in:
    # opening balance (1) + transactions (7) == 8, not 8 + 14 or 8 + 15 (header).
    assert len(rows) == 8


def test_legend_table_row_count_matches_fixture_shape():
    # Sanity on the fixture itself: ~14 abbreviation rows, as documented.
    assert len(fixture_gen.LEGEND_ROWS) == 14


# ---------------------------------------------------------------------------
# Sweep transfers are real transactions, must be kept
# ---------------------------------------------------------------------------

def test_sweep_transfers_are_kept_as_real_rows(tmp_path):
    pdf_path = tmp_path / "syn_kotak.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())

    result = KotakSkill().parse(pdf_path)
    descriptions = [r["Description"] for r in result.rows]
    sweep_rows = [d for d in descriptions if "sweep transfer" in d.lower()]
    assert len(sweep_rows) == 2
    assert any("to fd" in d.lower() for d in sweep_rows)
    assert any("from fd" in d.lower() for d in sweep_rows)


# ---------------------------------------------------------------------------
# Password-protected PDF
# ---------------------------------------------------------------------------

def test_encrypted_pdf_with_correct_password_extracts(tmp_path):
    pdf_path = tmp_path / "syn_kotak_encrypted.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf(password="SYNPWD1"))

    result = KotakSkill().parse(pdf_path, password="SYNPWD1")

    assert len(result.rows) == 7
    assert result.meta.password_used is True
    assert result.meta.source_format == "pw-pdf"


def test_encrypted_pdf_without_password_gives_clear_error(tmp_path):
    pdf_path = tmp_path / "syn_kotak_encrypted.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf(password="SYNPWD1"))

    with pytest.raises(RuntimeError) as exc_info:
        KotakSkill().parse(pdf_path)

    msg = str(exc_info.value).lower()
    assert "password" in msg
    assert "SYNPWD1" not in str(exc_info.value)


def test_encrypted_pdf_with_wrong_password_gives_clear_error(tmp_path):
    pdf_path = tmp_path / "syn_kotak_encrypted.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf(password="SYNPWD1"))

    with pytest.raises(RuntimeError) as exc_info:
        KotakSkill().parse(pdf_path, password="WRONGPWD")

    msg = str(exc_info.value)
    assert "password" in msg.lower()
    assert "SYNPWD1" not in msg
    assert "WRONGPWD" not in msg


# ---------------------------------------------------------------------------
# Text-quality rejection -- no OCR fallback for Kotak
# ---------------------------------------------------------------------------

def test_garbled_pdf_raises_with_no_ocr_fallback(tmp_path):
    pdf_path = tmp_path / "garbled.pdf"
    pdf_path.write_bytes(fixture_gen.build_garbled_pdf())

    with pytest.raises(RuntimeError) as exc_info:
        extract(pdf_path)

    assert "OCR" in str(exc_info.value)


# ---------------------------------------------------------------------------
# formats() / detect() basics
# ---------------------------------------------------------------------------

def test_formats_is_pdf_only():
    assert KotakSkill().formats() == (".pdf",)


def test_detect_returns_zero_for_non_pdf_suffix(tmp_path):
    txt_path = tmp_path / "not_a_statement.txt"
    txt_path.write_text("hello", encoding="utf-8")
    assert KotakSkill().detect(txt_path) == 0.0


# ---------------------------------------------------------------------------
# BNK-03: the REAL page-1 layout (unlabelled period, "Account No. <digits>",
# no bank name on page 1, IFSC KKBK...) -- invented values only.
# ---------------------------------------------------------------------------

def _real(tmp_path, name="real.pdf", password=None, **kw):
    p = tmp_path / name
    p.write_bytes(fixture_gen.build_pdf(password=password, layout="real", **kw))
    return p


def test_real_layout_reads_account_and_period(tmp_path):
    result = KotakSkill().parse(_real(tmp_path))
    assert result.meta.account_number == fixture_gen.SYN_REAL_ACCOUNT_NUMBER
    assert result.meta.period_from == fixture_gen.SYN_PERIOD_FROM
    assert result.meta.period_to == fixture_gen.SYN_PERIOD_TO


def test_real_layout_rows_and_balances_match_old_layout(tmp_path):
    old = tmp_path / "old.pdf"
    old.write_bytes(fixture_gen.build_pdf())
    r_old = KotakSkill().parse(old)
    r_real = KotakSkill().parse(_real(tmp_path))
    assert r_real.rows == r_old.rows == EXPECTED_ROWS
    assert r_real.opening_balance == fixture_gen.SYN_OPENING_BALANCE
    assert r_real.closing_balance == fixture_gen.SYN_CLOSING_BALANCE
    assert r_real.balance_check.ok is True


def test_real_layout_encrypted_same_rows_and_meta(tmp_path):
    plain = KotakSkill().parse(_real(tmp_path))
    enc = KotakSkill().parse(_real(tmp_path, "enc.pdf", password="SYNPWD1"), password="SYNPWD1")
    assert enc.rows == plain.rows
    assert enc.meta.account_number == plain.meta.account_number
    assert enc.meta.period_from == plain.meta.period_from
    assert enc.meta.period_to == plain.meta.period_to
    assert enc.meta.password_used is True


def test_real_layout_wrong_password_gives_password_message(tmp_path):
    p = _real(tmp_path, "enc.pdf", password="SYNPWD1")
    with pytest.raises(RuntimeError) as exc_info:
        KotakSkill().parse(p, password="WRONGPWD")
    msg = str(exc_info.value)
    assert "password" in msg.lower()
    assert "SYNPWD1" not in msg and "WRONGPWD" not in msg


def test_old_layout_still_reads_old_account_and_period(tmp_path):
    p = tmp_path / "old.pdf"
    p.write_bytes(fixture_gen.build_pdf())
    meta = KotakSkill().parse(p).meta
    assert meta.account_number == fixture_gen.SYN_ACCOUNT_NUMBER
    assert meta.period_from == fixture_gen.SYN_PERIOD_FROM
    assert meta.period_to == fixture_gen.SYN_PERIOD_TO


def test_real_layout_without_period_line_leaves_period_none(tmp_path):
    # Footer ("Statement Generated on 01 Jul 2026") and transaction dates are
    # on the page; none of them may be taken as the period.
    result = KotakSkill().parse(_real(tmp_path, include_period=False))
    assert result.meta.period_from is None
    assert result.meta.period_to is None
    assert result.rows == EXPECTED_ROWS
    assert result.meta.account_number == fixture_gen.SYN_REAL_ACCOUNT_NUMBER


def test_footer_and_transaction_dates_never_become_the_period(tmp_path):
    from agents.skill_kotak import agent as kagent

    p = _real(tmp_path, include_period=False)
    _acct, pf, pt = kagent._extract_meta_fields(p)
    assert (pf, pt) == (None, None)
    for bad in ("2026-07-01", "2026-06-03"):
        assert bad not in (pf, pt)


def test_period_line_only_read_from_first_lines(tmp_path):
    # A period-shaped line below the first 5 lines is ignored.
    p = tmp_path / "late.pdf"
    p.write_bytes(fixture_gen.build_plain_pdf(
        ["Account Statement", "a", "b", "c", "d", "e",
         "01 Jun 2026 - 30 Jun 2026"]))
    from agents.skill_kotak import agent as kagent
    assert kagent._extract_meta_fields(p) == (None, None, None)


def test_account_type_status_statement_never_give_an_account_number(tmp_path):
    result = KotakSkill().parse(_real(tmp_path, include_account=False))
    assert result.meta.account_number is None
    from agents.skill_kotak.agent import _ACCOUNT_RE
    for line in ("Account Type Savings", "Account Status Active",
                 "Account Statement", "Account Number 123456"):
        assert _ACCOUNT_RE.search(line) is None
    assert _ACCOUNT_RE.search("Account No : 123").group(1) == "123"
    assert _ACCOUNT_RE.search("HOLDER Account No. 9988776655").group(1) == "9988776655"


# --- detect() ---------------------------------------------------------------

def test_detect_real_layout_unencrypted(tmp_path):
    assert KotakSkill().detect(_real(tmp_path)) == 0.95


def test_detect_old_layout_unencrypted(tmp_path):
    p = tmp_path / "old.pdf"
    p.write_bytes(fixture_gen.build_pdf())
    assert KotakSkill().detect(p) == 0.95


def test_detect_real_layout_encrypted_is_low_but_nonzero(tmp_path):
    assert KotakSkill().detect(_real(tmp_path, "enc.pdf", password="SYNPWD1")) == 0.3


def test_detect_unreadable_pdf_is_low_but_nonzero(tmp_path):
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"this is not a pdf at all")
    assert KotakSkill().detect(p) == 0.3


def test_detect_missing_file_is_zero_and_does_not_raise(tmp_path):
    assert KotakSkill().detect(tmp_path / "nope.pdf") == 0.0


def test_detect_unrelated_pdf_is_zero(tmp_path):
    p = tmp_path / "other.pdf"
    p.write_bytes(fixture_gen.build_plain_pdf(
        ["Quarterly newsletter", "Some text"], [["a", "b"], ["1", "2"]]))
    assert KotakSkill().detect(p) == 0.0


def test_detect_column_words_without_kotak_marker_or_kkbk_is_zero(tmp_path):
    p = tmp_path / "cols.pdf"
    p.write_bytes(fixture_gen.build_plain_pdf(
        ["Other Bank statement", "IFSC Code HDFC0000123"],
        [["Withdrawal", "Deposit", "Balance"], ["1", "2", "3"]]))
    assert KotakSkill().detect(p) == 0.0


def test_detect_real_layout_without_ifsc_is_zero(tmp_path):
    # Column words present, no bank name, no KKBK IFSC -> nothing identifies Kotak.
    assert KotakSkill().detect(_real(tmp_path, ifsc=False)) == 0.0
