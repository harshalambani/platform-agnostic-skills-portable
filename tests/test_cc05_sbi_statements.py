"""
tests/test_cc05_sbi_statements.py -- CC-05: the SBI BPCL Octane card.

Everything is synthetic: the line shapes follow the masked layout the planning
session described; every amount, description and name below is made up. Text is
supplied by monkeypatching ``read_lines`` or by passing lines straight in.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_cc_transactions" / "scripts" / "create_cc_transaction_list.py"
for _p in (str(SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_cc_sort import completeness as C          # noqa: E402
from agents.skill_cc_spend_booking import journal as J      # noqa: E402


def _load_module():
    spec = importlib.util.spec_from_file_location("create_cc_transaction_list", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load_module()


def _head(prev="1,000.00", cred="1,000.00", deb="2,500.50", fee="118.00", total="2,618.50"):
    return [
        "Payments,",
        "Previous Balance Reversals & other Purchases & Other Fee, Taxes & Total Outstanding",
        "( ` ) Credits ( ` ) Debits ( ` ) Interest Charges( ` ) ( ` )",
        f"{prev} {cred} {deb} {fee} {total}",
    ]


BLOCK = [
    "Date Transaction Details Amount ( ` )",
    "for Statement Period: 05 Mar 26 to 04 Apr 26",
    "06 Mar 26 PAYMENT RECEIVED AB12CD34 1,000.00 C",
    "10 Mar 26 EXAMPLE FUEL STATION CITY 2,500.50 D",
    "20 Mar 26 ANNUAL FEE EXAMPLE (CARD YEAR 100.00) 100.00 D",
    "IGST DR @ 18.00% 18.00 D",
]
LEGEND = ["Purchases & Other Debits; Fee, Taxes & Interest", "C=Credit ; D=Debit"]


def stmt_lines(head=None, block=None):
    return (head or _head()) + (block or BLOCK) + LEGEND


def parse(lines, bank="SBI", card="BPCL-Octane", name="s.pdf"):
    return m.parse_statement(lines, bank, card, name)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def test_sbi_rows_summary_period_and_tie_out():
    st = parse(stmt_lines())
    rows = st.parsed.rows
    assert [(r.date, r.amount, r.direction, r.kind) for r in rows] == [
        (datetime(2026, 3, 6), 1000.0, "Cr", "payment"),
        (datetime(2026, 3, 10), 2500.5, "Dr", "spend"),
        (datetime(2026, 3, 20), 100.0, "Dr", "fee"),
        (datetime(2026, 3, 20), 18.0, "Dr", "fee"),          # undated IGST takes the date of the fee above it
    ]
    assert not st.parsed.errors
    assert st.period == (date(2026, 3, 5), date(2026, 4, 4))
    t = m.tie_out(st)
    assert t.result == "PASS" and "strictly below 1.00" in t.note


def test_sbi_statement_with_terms_text_is_kept_not_skipped():
    lines = ["Most Important Terms and Conditions", "Fees are shown in the table"] + stmt_lines()
    assert m.non_statement_reason(lines, "SBI") is None
    assert m.non_statement_reason(lines, "Axis") == "Most Important Terms and Conditions"     # scoped to SBI


def test_sbi_through_a_whole_run_lists_the_rows_and_does_not_skip(tmp_path, monkeypatch):
    d = tmp_path / "in" / "SBI-BPCL-Octane"
    d.mkdir(parents=True)
    (d / "a.pdf").write_bytes(b"%PDF-1.4 synthetic")
    lines = ["Most Important Terms and Conditions"] + stmt_lines()
    monkeypatch.setattr(m, "read_lines", lambda p: lines)
    res = m.run_extraction(tmp_path / "in", tmp_path / "o.xlsx", date(2026, 1, 1), date(2026, 12, 31), "x")
    assert len(res.rows) == 4 and not res.skipped
    assert res.ties[0].result == "PASS"


def test_terms_only_document_with_a_date_but_no_amount_c_d_row_is_skipped():          # NEGATIVE
    terms = ["Most Important Terms and Conditions", "The offer is valid until 05 Apr 26 for all cardholders",
             "Annual fee 500.00 per year"]
    assert m.non_statement_reason(terms, "SBI") == "Most Important Terms and Conditions"
    with_amount = terms + ["05 Apr 26 charges revised to 500.00 per year"]               # amount, but no C|D
    assert m.non_statement_reason(with_amount, "SBI") == "Most Important Terms and Conditions"


def test_hsbc_terms_leaflet_is_still_skipped():                                         # NEGATIVE
    leaflet = ["Example Bank Premier Credit Card", "Most Important Terms and Conditions",
               "Annual fee 500.00 per year"]
    assert m.non_statement_reason(leaflet, "HSBC") == "Most Important Terms and Conditions"
    assert m.non_statement_reason(leaflet) == "Most Important Terms and Conditions"


def test_an_sbi_line_without_a_trailing_c_or_d_is_not_a_row():                          # NEGATIVE
    block = BLOCK[:3] + ["11 Mar 26 EXAMPLE SHOP CITY 450.00"]
    st = parse(stmt_lines(block=block))
    assert [r.description for r in st.parsed.rows] == ["PAYMENT RECEIVED AB12CD34"]
    assert any("unparsed line" in e for e in st.parsed.errors)


def test_a_bracketed_inner_amount_is_never_the_row_amount():                            # NEGATIVE
    st = parse(stmt_lines())
    fee = next(r for r in st.parsed.rows if r.description.startswith("ANNUAL FEE"))
    assert fee.amount == 100.0 and "100.00" in fee.description
    block = BLOCK[:2] + ["20 Mar 26 ANNUAL FEE EXAMPLE (CARD YEAR 777.00) 100.00 D", "IGST DR @ 9.00% 27.00 D"]
    st2 = parse(stmt_lines(_head(prev="0.00", cred="0.00", deb="0.00", fee="127.00", total="127.00"), block))
    assert [r.amount for r in st2.parsed.rows] == [100.0, 27.0]                          # rate 9.00% is not an amount
    # a bracket with no row amount after it is not a row at all
    st3 = parse(stmt_lines(block=BLOCK[:2] + ["20 Mar 26 ANNUAL FEE EXAMPLE (CARD YEAR 777.00) D"]))
    assert st3.parsed.rows == [] and not any("777" in str(r.amount) for r in st3.parsed.rows)


def test_a_cardholder_header_line_produces_no_row_and_is_never_copied():                # NEGATIVE
    block = BLOCK[:2] + ["EXAMPLE CARDHOLDER NAMEHERE ONLY"] + BLOCK[2:]
    st = parse(stmt_lines(block=block))
    assert len(st.parsed.rows) == 4
    assert not any("CARDHOLDER" in r.description for r in st.parsed.rows)
    assert not st.parsed.errors


def test_rows_outside_the_block_and_after_the_legend_are_ignored():                     # NEGATIVE
    lines = stmt_lines() + ["12 Apr 26 TERMS TEXT SAMPLE 999.00 D"]
    st = parse(lines)
    assert len(st.parsed.rows) == 4


def test_an_undated_tax_row_before_any_dated_row_is_an_error_not_a_row():               # NEGATIVE
    st = parse(stmt_lines(block=BLOCK[:2] + ["IGST DR @ 18.00% 18.00 D"]))
    assert st.parsed.rows == [] and any("before any dated row" in e for e in st.parsed.errors)


# ---------------------------------------------------------------------------
# tie-out
# ---------------------------------------------------------------------------

def test_tie_out_residual_below_one_rupee_passes_and_exactly_one_fails():
    assert m.tie_out(parse(stmt_lines(_head(total="2,619.49")))).result == "PASS"        # 0.99 off
    assert m.tie_out(parse(stmt_lines(_head(total="2,619.50")))).result == "FAIL"        # exactly 1.00 off     NEGATIVE
    assert m.tie_out(parse(stmt_lines(_head(total="2,617.50")))).result == "FAIL"        # -1.00


def test_a_dropped_row_fails_even_when_it_is_under_one_rupee():                         # NEGATIVE
    # a 0.50 fee row is dropped: the total still ties within the rupee rounding,
    # but the rows no longer equal debits + fees - credits exactly
    head = _head(prev="1,000.00", cred="1,000.00", deb="2,500.50", fee="0.50", total="2,501.00")
    block = BLOCK[:4]
    st = parse(stmt_lines(head, block))
    assert abs(st.parsed.summary["total"] - (1000.0 + sum(r.signed for r in st.parsed.rows))) < 1.0
    t = m.tie_out(st)
    assert t.result == "FAIL" and "dropped, doubled or misread" in t.note


def test_cr_on_the_previous_balance_and_on_the_total_is_negative():
    prev_cr = parse(stmt_lines(_head(prev="100.00 CR", cred="100.00", deb="250.00", fee="0.00", total="50.00"),
                               BLOCK[:2] + ["06 Mar 26 PAYMENT RECEIVED AB12CD34 100.00 C",
                                            "10 Mar 26 EXAMPLE FUEL STATION CITY 250.00 D"]))
    assert prev_cr.parsed.summary["prev"] == -100.0
    assert m.tie_out(prev_cr).result == "PASS"
    total_cr = parse(stmt_lines(_head(prev="100.00", cred="300.00", deb="100.00", fee="0.00", total="100.00 CR"),
                                BLOCK[:2] + ["06 Mar 26 PAYMENT RECEIVED AB12CD34 300.00 C",
                                             "10 Mar 26 EXAMPLE FUEL STATION CITY 100.00 D"]))
    assert total_cr.parsed.summary["total"] == -100.0
    assert m.tie_out(total_cr).result == "PASS"
    # the same statement with the CR ignored would not tie: the sign matters
    wrong = parse(stmt_lines(_head(prev="100.00", cred="300.00", deb="100.00", fee="0.00", total="100.00"),
                             BLOCK[:2] + ["06 Mar 26 PAYMENT RECEIVED AB12CD34 300.00 C",
                                          "10 Mar 26 EXAMPLE FUEL STATION CITY 100.00 D"]))
    assert m.tie_out(wrong).result == "FAIL"


def test_a_cr_on_a_non_balance_summary_figure_is_reported():                            # NEGATIVE
    st = parse(stmt_lines(_head(cred="1,000.00 CR")))
    assert any("unexpected CR" in e for e in st.parsed.errors)


def test_a_missing_summary_is_not_a_pass():
    st = parse(BLOCK + LEGEND)
    assert m.tie_out(st).result.startswith("NOT AVAILABLE")


# ---------------------------------------------------------------------------
# period (CC-01 and CC-02)
# ---------------------------------------------------------------------------

def test_sbi_period_is_read_by_the_completeness_pattern_and_by_detect_period():
    info = C.classify_text("\n".join(stmt_lines()))
    assert info.kind == "period" and info.pattern == "SBI"
    assert (info.start, info.end) == (date(2026, 3, 5), date(2026, 4, 4))
    assert m.detect_period(stmt_lines()) == ((date(2026, 3, 5), date(2026, 4, 4)), "")


def test_the_sbi_period_pattern_does_not_change_other_banks():                          # NEGATIVE
    assert C.classify_text("Billing Period 1 Apr, 2025 - 30 Apr, 2025").pattern == "HDFC"
    assert C.classify_text("01/04/2025 To 30/04/2025").pattern == "YES"
    assert C.classify_text("Statement Period: 01 Apr 2025 to 30 Apr 2025") is None        # 4-digit year is not SBI


def test_other_banks_never_read_sbi_shaped_lines():                                     # NEGATIVE
    for bank in ("Axis", "HDFC", "YES", "SBM", "ICICI", "HSBC"):
        st = parse(stmt_lines(), bank=bank, card="X")
        assert st.parsed.rows == [], bank


# ---------------------------------------------------------------------------
# fees: same convention as the other banks, and CC-03 books them the same way
# ---------------------------------------------------------------------------

def _fee_statement(reversal=False):
    if reversal:
        block = BLOCK[:2] + ["20 Mar 26 ANNUAL FEE EXAMPLE (CARD YEAR 100.00) 100.00 D", "IGST DR @ 18.00% 18.00 D",
                             "25 Mar 26 Annual Fee EXAMPLE (CARD YEAR 100.00) 100.00 C", "IGST CR @ 18.00% 18.00 C"]
        head = _head(prev="0.00", cred="118.00", deb="0.00", fee="118.00", total="0.00")
    else:
        block = BLOCK[:2] + BLOCK[4:6]
        head = _head(prev="0.00", cred="0.00", deb="0.00", fee="118.00", total="118.00")
    return parse(stmt_lines(head, block))


def test_fee_and_gst_rows_go_into_the_fee_block_like_other_banks():
    st = _fee_statement()
    assert [(r.kind, r.amount) for r in st.parsed.rows] == [("fee", 100.0), ("fee", 18.0)]
    block = m.fee_block(m.collect_fees([st]))
    assert "CARD FEES CHARGED (2)" in block and "Total fees SBI-BPCL-Octane: 118.00" in block
    assert m.tie_out(st).result == "PASS"


def test_a_fee_credit_with_its_tax_credit_is_a_reversal_of_both():
    st = _fee_statement(reversal=True)
    kinds = [(r.kind, r.direction) for r in st.parsed.rows]
    assert kinds == [("fee", "Dr"), ("fee", "Dr"), ("refund", "Cr"), ("refund", "Cr")]
    fees = m.collect_fees([st])
    assert len(fees) == 2 and all(f.reversed_by is not None for f in fees)
    assert fees[0].reversed_by is not fees[1].reversed_by                 # one-to-one
    assert m.tie_out(st).result == "PASS"


def test_cc03_books_sbi_fees_and_reversals_once_each():
    st = _fee_statement(reversal=True)
    js, left, issues = J.journals_for_settlement(st, date(2026, 5, 10), m.FEE_REVERSAL_RX, 20000)
    assert sorted((j.kind, j.amount) for j in js) == sorted(
        [(J.K_FEE, 100.0), (J.K_FEE, 18.0), (J.K_FEE_REV, 100.0), (J.K_FEE_REV, 18.0)])
    assert len({j.num for j in js}) == 4 and not left and not issues      # NEGATIVE: no double booking
    assert not any(j.kind in (J.K_SPEND, J.K_REFUND) for j in js)         # a fee is never a spend or a refund
    assert all(not j.needs_mapping for j in js)
