"""KRC-02: Pay-In/Pay-Out follow the Dr/Cr side; a bill matches its own line first.
Synthetic narrations only (no names, no real amounts)."""
from __future__ import annotations
import importlib.util, sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "agents"


def _load(rel, name):
    spec = importlib.util.spec_from_file_location(name, SRC / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


led = _load("skill_krc/scripts/parse_krc_ledger.py", "krc02_ledger")
rec = _load("skill_krc_recon/scripts/parse_krc_bills.py", "krc02_recon")


def _row(p, debit, credit, chq="", date="01/04/2025"):
    return {"date": date, "vno": "1", "particulars": p, "chqno": chq,
            "debit": debit, "credit": credit}


RECEIPT = "BSE_CASH RECEIVED AMOUNT THROUGH HDFC-CMS(FUND TRANS), MERCHANT TXNID : T000111"


def test_credit_side_receipt_is_pay_in_even_when_debit_is_zero_string():
    assert led._tag_row(_row(RECEIPT, "0.00", "1,000.00"), set()) == "Bank Pay-In"
    assert led._tag_row(_row(RECEIPT, None, "1000.00"), set()) == "Bank Pay-In"


def test_credit_side_receipt_never_pay_out():
    for d in ("0.00", "", None, "0"):
        assert led._tag_row(_row(RECEIPT, d, "500.00"), set()) != "Bank Pay-Out"


def test_debit_side_bank_row_is_pay_out_even_with_zero_credit_string():
    assert led._tag_row(_row("NEFT PAYOUT", "700.00", "0.00", chq="123"), set()) == "Bank Pay-Out"


def _bill(cn, amt, setl="9001"):
    return {"cn_no": cn, "net_amount": amt, "settlement": setl, "type": "TRADE"}


def _lrow(p, debit, credit, tag):
    return {"date": "01/04/2025", "vno": "1", "particulars": p, "chqno": "",
            "debit": debit, "credit": credit, "tag": tag}


def test_bill_matches_own_line_not_earlier_bank_receipt():
    rows = [_lrow(RECEIPT, None, 1000.0, "Bank Pay-In"),
            _lrow("BSE_CASH BILL M-9001", 1000.0, None, "Settlement Movement")]
    out, unmatched = rec.classify_and_match(rows, [_bill("CN1", 1000.0)], set(), [])
    assert out[0]["bill_cn"] is None and out[0]["category"] == "Bank Pay-In"
    assert out[1]["bill_cn"] == "CN1" and out[1]["category"] == "Trade Bill"
    assert unmatched == []


def test_bill_never_taken_from_bank_receipt_even_as_fallback():
    rows = [_lrow(RECEIPT, None, 1000.0, "Bank Pay-In")]
    out, unmatched = rec.classify_and_match(rows, [_bill("CN1", 1000.0)], set(), [])
    assert out[0]["bill_cn"] is None
    assert [b["cn_no"] for b in unmatched] == ["CN1"]


def test_totals_unchanged():
    rows = [_lrow(RECEIPT, None, 1000.0, "Bank Pay-In"),
            _lrow("BILL", 1000.0, None, "Settlement Movement"),
            _lrow("BILL2", 250.0, None, "Settlement Movement")]
    out, _ = rec.classify_and_match(rows, [_bill("CN1", 1000.0), _bill("CN2", 250.0, "9002")], set(), [])
    assert sum(r["debit"] or 0 for r in out) == 1250.0
    assert sum(r["credit"] or 0 for r in out) == 1000.0
    assert len(out) == len(rows)
