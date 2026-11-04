"""
tests/test_pipeline_bank_warnings_surfaced.py -- regression guard for IMP-04:
skill_gnucash_pipeline.agent.run() calls each dedicated bank skill's
parse() and gets back a BankResult carrying ``warnings`` (missing/
overlapping-statement gaps from multi-file consolidation, extracted-vs-
expected transaction count mismatches, unparseable rows, running-balance
mismatches, ...) but previously never read that field -- the warnings were
computed by every bank skill and then silently discarded on the floor of
the pipeline's Step 1, never reaching the user.

This verifies the fix: bank_result.warnings now appear in the string
run() returns, clearly marked as warnings (a dedicated "Step 1 warnings"
line plus one ``⚠`` line per warning), for each of the three warning
kinds the brief calls out:
  - missing-statement  (POSSIBLE MISSING STATEMENT, via BoB multi-file gap)
  - overlapping-statement (OVERLAPPING/OUT-OF-ORDER STATEMENTS, via BoB
    multi-file overlap)
  - count-mismatch (TRANSACTION COUNT MISMATCH, via a tampered HDFC
    STATEMENT SUMMARY block)
and that a clean statement set produces no spurious warning line.
"""
from __future__ import annotations

import gzip
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _sub in ("skill_bob", "skill_hdfc"):
    p = ROOT / _sub
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

REPO_ROOT = ROOT.parent
SRC = REPO_ROOT / "src"
for _p in (SRC, SRC / "agents"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import bob_fixture_gen  # noqa: E402
import hdfc_fixture_gen  # noqa: E402
from agents.skill_gnucash_pipeline.agent import run as pipeline_run  # noqa: E402

_NS_DECL = 'xmlns:gnc="http://www.gnucash.org/XML/gnc" xmlns:act="http://www.gnucash.org/XML/act"'


def _account_xml(name: str, aid: str, atype: str, parent: str | None) -> str:
    parts = [
        '  <gnc:account version="2.0.0">',
        f"    <act:name>{name}</act:name>",
        f'    <act:id type="guid">{aid}</act:id>',
        f"    <act:type>{atype}</act:type>",
    ]
    if parent is not None:
        parts.append(f'    <act:parent type="guid">{parent}</act:parent>')
    parts.append("  </gnc:account>")
    return "\n".join(parts)


def _empty_book(tmp_path: Path) -> str:
    """A book with no bank account at all -- irrelevant to this test (we're
    only checking Step 1 statement-parsing warnings), but the pipeline still
    needs a readable .gnucash file to get that far."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<gnc-v2 {_NS_DECL}>\n"
        '<gnc:book version="2.0.0">\n'
        + _account_xml("Root Account", "root", "ROOT", None)
        + "\n"
        + _account_xml("Assets", "asset", "ASSET", "root")
        + "\n</gnc:book>\n</gnc-v2>\n"
    )
    p = tmp_path / "book.gnucash"
    p.write_bytes(gzip.compress(xml.encode("utf-8")))
    return str(p)


_JAN_TXNS = [
    ("15-01-2025", "NEFT SALARY CREDIT-SYNCO", "", "", "50,000.00", "1,50,000.00Cr"),
]
_MAR_TXNS = [
    ("05-03-2025", "ACH D-BD-SYNTH MF-SIP0001", "", "5,000.00", "", "1,43,000.00Cr"),
]
_JAN_FULL_TXNS = [
    ("01-01-2025", "NEFT SALARY CREDIT-SYNCO", "", "", "50,000.00", "1,50,000.00Cr"),
    ("31-01-2025", "UPI-REFUND ORDER-SYNSHOP", "", "", "500.00", "1,50,500.00Cr"),
]
_JAN_OVERLAP_TXNS = [
    ("15-01-2025", "UPI-GROCERY STORE-SYN", "", "2,000.00", "", "1,48,500.00Cr"),
]


def test_missing_statement_gap_warning_reaches_pipeline_result(tmp_path):
    """A dated gap between two BoB statements in the same batch (Jan, then
    Mar with Feb missing) must surface as a POSSIBLE MISSING STATEMENT
    warning in the pipeline's returned result, not be silently dropped."""
    (tmp_path / "stmt_jan.pdf").write_bytes(
        bob_fixture_gen.build_pdf_for(_JAN_TXNS, "01-01-2025", "31-01-2025"))
    (tmp_path / "stmt_mar.pdf").write_bytes(
        bob_fixture_gen.build_pdf_for(_MAR_TXNS, "01-03-2025", "31-03-2025"))
    gnucash_file = _empty_book(tmp_path)
    out_path = tmp_path / "out.csv"

    result = pipeline_run(
        bank="Bank of Baroda",
        statement_files=str(tmp_path),
        gnucash_file=gnucash_file,
        output_path=str(out_path),
        config_path=None,
    )

    assert "Step 1 warnings" in result, (
        f"bank_result.warnings was non-empty but no warnings section "
        f"appeared in the pipeline result.\nlog={result}"
    )
    assert "POSSIBLE MISSING STATEMENT" in result, (
        f"the missing-statement gap warning was swallowed instead of "
        f"reaching the pipeline's returned result.\nlog={result}"
    )


def test_overlapping_statement_warning_reaches_pipeline_result(tmp_path):
    """Two BoB statements whose transaction-date periods overlap must
    surface an OVERLAPPING/OUT-OF-ORDER STATEMENTS warning in the pipeline
    result."""
    (tmp_path / "stmt_jan_full.pdf").write_bytes(
        bob_fixture_gen.build_pdf_for(_JAN_FULL_TXNS, "01-01-2025", "31-01-2025"))
    (tmp_path / "stmt_jan_overlap.pdf").write_bytes(
        bob_fixture_gen.build_pdf_for(_JAN_OVERLAP_TXNS, "15-01-2025", "15-01-2025"))
    gnucash_file = _empty_book(tmp_path)
    out_path = tmp_path / "out.csv"

    result = pipeline_run(
        bank="Bank of Baroda",
        statement_files=str(tmp_path),
        gnucash_file=gnucash_file,
        output_path=str(out_path),
        config_path=None,
    )

    assert "Step 1 warnings" in result
    assert "OVERLAPPING/OUT-OF-ORDER" in result, (
        f"the overlapping-statement warning was swallowed instead of "
        f"reaching the pipeline's returned result.\nlog={result}"
    )


def _tampered_count_mismatch_hdfc_pdf() -> bytes:
    """A synthetic HDFC PDF whose STATEMENT SUMMARY line claims 3 more
    credits than actually appear in the transaction table (Cr Count 2 -> 5),
    so skill_hdfc.agent.parse() emits a TRANSACTION COUNT MISMATCH warning
    (extracted 5 transactions total, statement claims Dr=3+Cr=5=8)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    lines = hdfc_fixture_gen._pdf_lines()
    tampered_cr_count = hdfc_fixture_gen.SYN_CR_COUNT + 3
    lines[-1] = lines[-1].replace(
        f"{hdfc_fixture_gen.SYN_DR_COUNT} {hdfc_fixture_gen.SYN_CR_COUNT} ",
        f"{hdfc_fixture_gen.SYN_DR_COUNT} {tampered_cr_count} ",
    )
    assert lines[-1] != hdfc_fixture_gen._pdf_lines()[-1]  # sanity: tamper took effect

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    c.setFont("Helvetica", 9)
    y = height - 50
    for line in lines:
        c.drawString(40, y, line)
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


def test_count_mismatch_warning_reaches_pipeline_result(tmp_path):
    """A statement whose own summary block disagrees with the extracted
    transaction count must surface a TRANSACTION COUNT MISMATCH warning in
    the pipeline result."""
    pdf_path = tmp_path / "syn_hdfc_mismatch.pdf"
    pdf_path.write_bytes(_tampered_count_mismatch_hdfc_pdf())
    gnucash_file = _empty_book(tmp_path)
    out_path = tmp_path / "out.csv"

    result = pipeline_run(
        bank="HDFC",
        statement_files=str(pdf_path),
        gnucash_file=gnucash_file,
        output_path=str(out_path),
        config_path=None,
    )

    assert "Step 1 warnings" in result
    assert "TRANSACTION COUNT MISMATCH" in result, (
        f"the count-mismatch warning was swallowed instead of reaching the "
        f"pipeline's returned result.\nlog={result}"
    )


# ── Negative test: a clean statement set must not fabricate a warning ──────

def test_clean_single_statement_produces_no_spurious_warning(tmp_path):
    """A single, well-formed BoB statement (no gaps, no overlaps, no count
    mismatch) must NOT produce a "Step 1 warnings" section at all -- the fix
    must surface real warnings, not invent one for every run."""
    pdf_path = tmp_path / "syn_bob.pdf"
    pdf_path.write_bytes(bob_fixture_gen.build_pdf())
    gnucash_file = _empty_book(tmp_path)
    out_path = tmp_path / "out.csv"

    result = pipeline_run(
        bank="Bank of Baroda",
        statement_files=str(pdf_path),
        gnucash_file=gnucash_file,
        output_path=str(out_path),
        config_path=None,
    )

    assert "Step 1 warnings" not in result, (
        f"a clean, single-file statement produced a spurious Step 1 "
        f"warnings section.\nlog={result}"
    )
    assert "POSSIBLE MISSING STATEMENT" not in result
    assert "OVERLAPPING/OUT-OF-ORDER" not in result
    assert "TRANSACTION COUNT MISMATCH" not in result
