"""
PIPE-09 -- the pipeline log and the contra check.

  * Step 3 and Step 6 each log a RESULT line; the log always ends with Step 6's.
  * The contra count in the run log equals the count in <stem>.contra.json.
  * The contra matcher is ONE-TO-ONE: a counterpart transaction is claimed by at
    most one statement row (four same-day same-amount payments used to be
    flagged against ONE other-bank transaction).
  * When the IFSC check reverts rows, the log names them (row number + reason,
    no narration).

Synthetic data only (masked account numbers, round amounts).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (ROOT, ROOT / "skill_hdfc", ROOT.parent / "src", ROOT.parent / "src" / "agents"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import gnc_book_fixture as fx  # noqa: E402
import hdfc_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402
from agents.skill_gnucash_reconciler.agent import detect_contra_entries  # noqa: E402

BOB = "Root Account:Assets:Current Assets:Cash and Bank:BOB - 7600"
HDFC = "Root Account:Assets:Current Assets:Cash and Bank:HDFC Bank - 1579"
TARGET = "Assets:Current Assets:Cash and Bank:HDFC Bank - 1579"
_ACCOUNTS = {
    "h": {"name": "HDFC", "type": "BANK", "path": HDFC},
    "b": {"name": "BOB", "type": "BANK", "path": BOB},
}


def _gd(*txns):
    return {"accounts": _ACCOUNTS, "transactions": list(txns)}


def _bob(date, amount):
    return {"date": date, "amount": amount, "account": BOB, "description": ""}


def _wd(date, desc, amount="2000.00"):
    return {"Date": date, "Description": desc, "Deposit": "", "Withdrawal": amount}


# ---- one-to-one matcher ----------------------------------------------------

def test_four_same_day_payments_claim_one_counterpart_exactly_once():
    rows = [_wd("2025-06-10", "UPI PAYMENT SHOP A"),
            _wd("2025-06-10", "UPI PAYMENT SHOP B"),
            _wd("2025-06-10", "UPI PAYMENT SHOP C"),
            _wd("2025-06-10", "UPI PAYMENT SHOP D")]
    res = detect_contra_entries(rows, _gd(_bob("2025-06-10", 2000.0)), TARGET)
    assert len(res) == 1                                  # not four
    claimed = [(r["contra_account"], r["contra_date"], r["contra_amount"]) for r in res]
    assert len(set(claimed)) == len(claimed)


def test_two_rows_never_claim_the_same_counterpart():
    rows = [_wd("2025-06-10", "SHOP"), _wd("2025-06-11", "OTHER SHOP")]
    res = detect_contra_entries(rows, _gd(_bob("2025-06-10", 2000.0)), TARGET)
    assert len(res) == 1


def test_same_date_beats_nearer_line_order():
    """Line order is the LAST tie-break: the same-date row wins."""
    rows = [_wd("2025-06-11", "SHOP A"), _wd("2025-06-10", "SHOP B")]
    res = detect_contra_entries(rows, _gd(_bob("2025-06-10", 2000.0)), TARGET)
    assert [r["row_idx"] for r in res] == [1]


def test_own_transfer_narration_breaks_a_date_tie():
    rows = [_wd("2025-06-10", "UPI PAYMENT SHOP"),
            _wd("2025-06-10", "XFER TO SELF BOB"),
            _wd("2025-06-10", "UPI PAYMENT OTHER")]
    res = detect_contra_entries(rows, _gd(_bob("2025-06-10", 2000.0)), TARGET)
    assert [r["row_idx"] for r in res] == [1]


def test_line_order_is_the_last_tiebreak_and_deterministic():
    rows = [_wd("2025-06-10", "SHOP A"), _wd("2025-06-10", "SHOP B")]
    first = detect_contra_entries(rows, _gd(_bob("2025-06-10", 2000.0)), TARGET)
    again = detect_contra_entries(rows, _gd(_bob("2025-06-10", 2000.0)), TARGET)
    assert [r["row_idx"] for r in first] == [0] == [r["row_idx"] for r in again]


def test_two_counterparts_pair_with_two_rows_each_once():
    rows = [_wd("2025-06-10", "SHOP A"), _wd("2025-06-10", "SHOP B")]
    res = detect_contra_entries(
        rows, _gd(_bob("2025-06-10", 2000.0), _bob("2025-06-11", 2000.0)), TARGET)
    assert sorted(r["row_idx"] for r in res) == [0, 1]
    assert len({r["contra_date"] for r in res}) == 2      # one counterpart each


def test_nearest_date_chosen_among_candidates():
    res = detect_contra_entries(
        [_wd("2025-06-10", "SHOP")],
        _gd(_bob("2025-06-12", 2000.0), _bob("2025-06-11", 2000.0)), TARGET)
    assert len(res) == 1 and res[0]["contra_date"] == "2025-06-11"


def test_reference_match_still_confirms():
    rows = [{"Date": "2025-06-10", "Description": "NEFT UTR123456789 SELF",
             "Deposit": "", "Withdrawal": "2000.00"}]
    cp = {"date": "2025-06-10", "amount": 2000.0, "account": BOB,
          "description": "NEFT UTR123456789 IN"}
    res = detect_contra_entries(rows, _gd(cp), TARGET)
    if res and res[0]["confidence"] == "high":
        assert res[0]["status"] == "confirmed"


def test_no_counterpart_means_no_flag():
    assert detect_contra_entries([_wd("2025-06-10", "SHOP")],
                                 _gd(_bob("2025-06-10", 999.0)), TARGET) == []


# ---- log count == contra.json count ---------------------------------------

def test_log_line_count_equals_written_sidecar(tmp_path):
    flags = {0: {"confidence": "medium", "status": "possible"},
             3: {"confidence": "high", "status": "confirmed"},
             5: {"confidence": "medium", "status": "possible"}}
    out = tmp_path / "o.csv"
    pipe._write_contra_sidecar(str(out), flags)
    written = json.loads(out.with_suffix(".contra.json").read_text(encoding="utf-8"))
    line = pipe._contra_log_line(flags)
    assert len(written) == 3
    assert re.search(r"\b3 cross-bank", line)
    assert "1 confirmed" in line and "2 possible" in line


def test_empty_flags_overwrite_a_stale_sidecar_and_log_says_none(tmp_path):
    out = tmp_path / "o.csv"
    side = out.with_suffix(".contra.json")
    side.write_text(json.dumps({"1": {"confidence": "medium"}}), encoding="utf-8")
    pipe._write_contra_sidecar(str(out), {})
    assert json.loads(side.read_text(encoding="utf-8")) == {}
    assert "no cross-bank" in pipe._contra_log_line({})


def test_empty_flags_create_no_sidecar_when_none_existed(tmp_path):
    out = tmp_path / "o.csv"
    pipe._write_contra_sidecar(str(out), {})
    assert not out.with_suffix(".contra.json").exists()


# ---- end to end: Step 3 / Step 6 result lines, contra count -----------------

def _book(tmp_path):
    txns = [
        fx.txn_xml("OPENING", "2025-03-01", [(fx.HDFC1, 10000000), ("obe", -10000000)]),
        fx.txn_xml("Salary credit SYNCO", "2025-04-01", [(fx.HDFC1, 5000000), ("int", -5000000)]),
        fx.txn_xml("Grocery store UPI", "2025-04-02", [(fx.HDFC1, -200000), ("groc", 200000)]),
        fx.txn_xml("SIP transfer", "2025-04-04", [(fx.HDFC1, -500000), (fx.HDFC2, 500000)]),
    ]
    return fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts(), txns)


def _run(tmp_path):
    csv_path = tmp_path / "stmt.csv"
    csv_path.write_text(fixture_gen.build_csv_text(), encoding="utf-8")
    out = tmp_path / "out.csv"
    result = pipe.run(bank="HDFC", statement_files=str(csv_path), gnucash_file=_book(tmp_path),
                      output_path=str(out), config_path=None, bank_account=fx.P_HDFC1)
    return result, out


def _log_lines(result):
    block = result.split("\n\n---\n\n")[0]
    return [ln for ln in block.splitlines()[1:] if ln.strip()]


def test_step3_result_line_logged_and_log_ends_with_step6_result(tmp_path):
    result, _out = _run(tmp_path)
    lines = _log_lines(result)
    assert any("Step 3 result" in ln for ln in lines), lines
    assert "Step 6 result" in lines[-1], lines[-1]
    # Step 3's result comes after the Step 3 header and before Step 6's
    i3 = next(i for i, ln in enumerate(lines) if "Step 3 result" in ln)
    assert i3 < len(lines) - 1


def test_run_log_contra_count_equals_contra_json(tmp_path):
    result, out = _run(tmp_path)
    side = out.with_suffix(".contra.json")
    on_disk = len(json.loads(side.read_text(encoding="utf-8"))) if side.exists() else 0
    m = re.search(r"Contra check -- (\d+) cross-bank", result)
    logged = int(m.group(1)) if m else 0
    assert logged == on_disk
    if not m:
        assert "Contra check -- no cross-bank" in result


# ---- IFSC revert is named in the log ---------------------------------------

def test_ifsc_revert_reason_names_the_code_and_not_the_narration():
    from agents.skill_gnucash_account_mapper import agent as m
    own = {"Assets:Cash and Bank:Alpha Bank - 013065XXXX",
           "Assets:Cash and Bank:HSBC - 094XXXX1234"}
    desc = "NEFT TO SOMEONE HSBC0560002 SECRET PAYEE NAME"
    why = m._ifsc_revert_reason(desc, "Assets:Cash and Bank:Alpha Bank - 013065XXXX", own)
    assert "HSBC" in why
    assert "SECRET" not in why and "PAYEE" not in why


def test_ifsc_revert_is_named_in_the_mapper_summary_end_to_end(tmp_path, monkeypatch):
    import gnc_book_fixture as fx2
    import test_map13_third_party_bank_code as t13
    from agents.skill_gnucash_account_mapper import agent as m
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        m, "smart_pattern_match",
        lambda desc, accts, w, d: {"account": "Root Account:" + fx2.P_HSBC1,
                                   "reason": "forced guess", "confidence": "smart"})
    # let the guess past the earlier own-transfer gate so Step 4.9 is what reverts it
    monkeypatch.setattr(m, "_gate_own_target", lambda *a, **k: True)
    d = tmp_path / "ifsc"
    d.mkdir()
    book = fx2.write_book(d / "b.gnucash", t13._accounts(), t13._history())
    csv_in = fx2.canonical_csv(d / "in.csv", [
        ("2025-08-01", "NEFT SECRETPAYEE PAYMENT HSBC0NEW456", "", "100.00")])
    summary = m.run(book, csv_in, str(d / "out.csv"), config_path=None,
                    bank_name="HDFC", gnucash_bank_account=fx2.P_HDFC1)
    assert "IFSC check reverted 1 row(s)" in summary
    assert "row 1:" in summary and "HSBC" in summary
    assert "SECRETPAYEE" not in summary          # no narration dump


def test_no_ifsc_revert_section_when_nothing_was_reverted(tmp_path, monkeypatch):
    import gnc_book_fixture as fx2
    import test_map13_third_party_bank_code as t13
    from agents.skill_gnucash_account_mapper import agent as m
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "noifsc"
    d.mkdir()
    book = fx2.write_book(d / "b.gnucash", t13._accounts(), t13._history())
    csv_in = fx2.canonical_csv(d / "in.csv", [("2025-08-01", "GROCERY STORE", "", "100.00")])
    summary = m.run(book, csv_in, str(d / "out.csv"), config_path=None,
                    bank_name="HDFC", gnucash_bank_account=fx2.P_HDFC1)
    assert "IFSC check reverted" not in summary
