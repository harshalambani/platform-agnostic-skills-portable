"""
BNK-04 -- SBM bank parser (OLE-encrypted .xlsx).

Synthetic, masked data only: a dummy password, a fake account number, round
amounts. The fixture is encrypted with msoffcrypto so the decrypt path is real.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
for _p in (ROOT.parent / "src", ROOT.parent / "src" / "agents"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import openpyxl  # noqa: E402
from agents import banks  # noqa: E402
from agents.skill_sbm import agent as sbm  # noqa: E402

PW = "dummy-Pass-0000"
ACCT = "00000099999"
HEAD = ["Transaction Timestamp", "Narration", "RRN", "Withdrawal(Dr)", "Deposit(Cr)", "Balance"]


def _rows():
    """Opening 1000.00. On 2025-05-02 the file order is WRONG: the 650 row
    (which depends on the 1000.50 row) comes first."""
    return [
        ["2025-05-01", "UPI/P2P/500000000001/MASKED PAYEE", "500000000001", 100, None, 900],
        ["2025-05-02", "ATM Cash Withdrawal", None, 350.5, None, 650],
        ["2025-05-02", "IFT/UPI_000111 FROM MASKED", None, None, 100.5, 1000.5],
        ["2025-05-03", "IFT/0000AAAA1111 TO SELF", None, 18, None, 632],
        ["2025-05-31", ACCT + ":Int.Pd:01-05-2025 to 31-05-2025", None, None, 50, 682],
        ["2025-06-01", "ATMISS/POSISS MASKED", None, 682, None, 0],
    ]


def _write(path, rows, password=PW, sheet="Transaction-Statement", header=HEAD):
    import msoffcrypto
    import msoffcrypto.format.ooxml
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for r in rows:
        ws.append(r)
    plain = io.BytesIO()
    wb.save(plain)
    if password is None:
        Path(path).write_bytes(plain.getvalue())
        return str(path)
    plain.seek(0)
    out = io.BytesIO()
    msoffcrypto.format.ooxml.OOXMLFile(plain).encrypt(password, out)
    Path(path).write_bytes(out.getvalue())
    return str(path)


def test_registered_as_a_bank():
    info = banks.get("sbm")
    assert info is not None and info.display_name == "SBM"
    assert banks.load_bank_skill(info).bank_key == "sbm"


def test_parse_orders_by_chain_and_keeps_every_row(tmp_path):
    res = sbm.parse(_write(tmp_path / "s.xlsx", _rows()), password=PW)
    assert len(res.rows) == 6                              # nothing dropped
    day2 = [r["Balance"] for r in res.rows if r["Date"] == "2025-05-02"]
    assert day2 == ["1000.50", "650.00"]                   # reordered, not dropped
    assert any("re-ordered" in w for w in res.warnings)
    assert res.balance_check.ok


def test_opening_derived_and_closing_is_last_chained_balance(tmp_path):
    res = sbm.parse(_write(tmp_path / "s.xlsx", _rows()), password=PW)
    assert res.opening_balance == 1000.0                   # 900 - 0 + 100
    assert res.closing_balance == 0.0 == float(res.rows[-1]["Balance"])


def test_closing_is_last_chained_not_last_in_file(tmp_path):
    rows = [["2025-05-02", "ATM Cash Withdrawal", None, 350.5, None, 650],
            ["2025-05-02", "IFT/UPI_000111 FROM MASKED", None, None, 100.5, 1000.5]]
    res = sbm.parse(_write(tmp_path / "s.xlsx", rows), password=PW)
    assert res.closing_balance == 650.0                    # file's last row says 1000.5
    assert res.opening_balance == 900.0


def test_zero_balance_row_is_kept(tmp_path):
    res = sbm.parse(_write(tmp_path / "s.xlsx", _rows()), password=PW)
    assert res.rows[-1]["Balance"] == "0.00" and res.rows[-1]["Withdrawal"] == "682.00"


def test_account_number_from_interest_narration(tmp_path):
    res = sbm.parse(_write(tmp_path / "s.xlsx", _rows()), password=PW)
    assert res.meta.account_number == ACCT
    assert res.meta.password_used is True


def test_no_interest_row_means_no_account_number(tmp_path):
    rows = [["2025-05-01", "UPI/P2P/1/X", None, 100, None, 900]]
    res = sbm.parse(_write(tmp_path / "s.xlsx", rows), password=PW)
    assert res.meta.account_number is None


def test_canonical_fields_and_types(tmp_path):
    res = sbm.parse(_write(tmp_path / "s.xlsx", _rows()), password=PW)
    r0 = res.rows[0]
    assert r0["Transaction ID"] == "500000000001" and r0["Withdrawal"] == "100.00"
    assert r0["Deposit"] == "" and r0["Currency"] == "INR"
    assert res.rows[1]["Transaction ID"] == ""             # RRN often empty


# ---- negative tests ---------------------------------------------------------

def test_wrong_password_fails_loudly_and_never_echoes_it(tmp_path):
    f = _write(tmp_path / "s.xlsx", _rows())
    with pytest.raises(ValueError) as ei:
        sbm.parse(f, password="not-the-password")
    assert "password" in str(ei.value).lower()
    assert "not-the-password" not in str(ei.value) and PW not in str(ei.value)


def test_missing_password_fails_loudly(tmp_path):
    with pytest.raises(ValueError):
        sbm.parse(_write(tmp_path / "s.xlsx", _rows()), password=None)


def test_broken_balance_chain_fails_the_parse(tmp_path):
    rows = _rows()
    rows[3][5] = 700                                       # 650 - 18 != 700
    with pytest.raises(sbm.SBMParseError) as ei:
        sbm.parse(_write(tmp_path / "s.xlsx", rows), password=PW)
    assert "2025-05-03" in str(ei.value) and "chain" in str(ei.value).lower()
    assert "MASKED" not in str(ei.value) and "SELF" not in str(ei.value)   # no narration


def test_removed_row_breaks_the_chain_not_silently_parsed(tmp_path):
    rows = _rows()
    del rows[2]
    with pytest.raises(sbm.SBMParseError):
        sbm.parse(_write(tmp_path / "s.xlsx", rows), password=PW)


def test_wrong_sheet_name_fails(tmp_path):
    with pytest.raises(sbm.SBMParseError):
        sbm.parse(_write(tmp_path / "s.xlsx", _rows(), sheet="Other"), password=PW)


def test_missing_column_fails(tmp_path):
    with pytest.raises(sbm.SBMParseError):
        sbm.parse(_write(tmp_path / "s.xlsx", [], header=HEAD[:-1]), password=PW)


def test_run_reports_error_instead_of_empty_csv(tmp_path):
    out = tmp_path / "o.csv"
    msg = sbm.run(_write(tmp_path / "s.xlsx", _rows()), str(out), pdf_password="wrong")
    assert msg.startswith("Error processing") and not out.exists()


def test_run_writes_csv_and_sidecar(tmp_path):
    out = tmp_path / "o.csv"
    msg = sbm.run(_write(tmp_path / "s.xlsx", _rows()), str(out), pdf_password=PW)
    assert "extracted 6 transactions" in msg and out.exists()
    assert out.with_suffix(".csv_summary.json").exists()


def test_detect_scores(tmp_path):
    assert sbm.detect(_write(tmp_path / "plain.xlsx", _rows(), password=None)) == 0.9
    assert sbm.detect(_write(tmp_path / "enc.xlsx", _rows())) == 0.2
    assert sbm.detect(tmp_path / "x.pdf") == 0.0


def test_pipeline_routes_sbm_through_the_registry_and_shared_import_path():
    """SBM is a plain registry bank: the pipeline calls skill.parse(), then the
    SAME canonical-CSV -> mapper -> reconcile path every bank uses."""
    import inspect
    from agents.skill_gnucash_pipeline import agent as pipe
    src = inspect.getsource(pipe.run)
    assert "skill.parse(bank_input, password=pdf_password)" in src
    assert "SBM" in pipe.DEDICATED_BANKS
