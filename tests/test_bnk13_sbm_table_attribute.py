"""BNK-13 -- the SBM reader opens a workbook whose table definition carries an
attribute the full openpyxl loader rejects.

The synthetic workbook has one table whose first column carries a dxfId
attribute (a shape the bank's real export has). openpyxl's full loader raises a
TypeError on it; read-only mode never parses table definitions. Synthetic data
only: a dummy password and a fake account number.
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
for _p in (ROOT.parent / "src", ROOT.parent / "src" / "agents"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import openpyxl  # noqa: E402
from openpyxl.worksheet.table import Table  # noqa: E402
from agents.skill_sbm import agent as sbm  # noqa: E402

PW = "dummy-Pass-0000"
HEAD = ["Transaction Timestamp", "Narration", "RRN", "Withdrawal(Dr)", "Deposit(Cr)", "Balance"]
ROWS = [
    ["2025-05-01", "UPI/P2P/500000000001/MASKED PAYEE", "500000000001", 100, None, 900],
    ["2025-05-02", "ATM Cash Withdrawal", None, 350.5, None, 549.5],
    ["2025-05-31", "00000099999:Int.Pd:01-05-2025 to 31-05-2025", None, None, 50, 599.5],
]


def _plain_bytes(with_bad_attribute: bool) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transaction-Statement"
    ws.append(HEAD)
    for r in ROWS:
        ws.append(r)
    ws.add_table(Table(displayName="Statement", ref="A1:F%d" % (len(ROWS) + 1)))
    buf = io.BytesIO()
    wb.save(buf)
    if not with_bad_attribute:
        return buf.getvalue()
    src = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("xl/tables/"):
                text = data.decode("utf-8")
                assert "<tableColumn " in text
                text = text.replace("<tableColumn ", '<tableColumn dxfId="1" ', 1)
                data = text.encode("utf-8")
            dst.writestr(item, data)
    return out.getvalue()


def _encrypted(path, plain: bytes):
    import msoffcrypto
    import msoffcrypto.format.ooxml
    out = io.BytesIO()
    msoffcrypto.format.ooxml.OOXMLFile(io.BytesIO(plain)).encrypt(PW, out)
    Path(path).write_bytes(out.getvalue())
    return str(path)


def test_the_fixture_really_breaks_the_full_loader():
    bad = _plain_bytes(True)
    with pytest.raises(TypeError):
        openpyxl.load_workbook(io.BytesIO(bad), data_only=True)
    wb = openpyxl.load_workbook(io.BytesIO(bad), data_only=True, read_only=True)
    try:
        assert "Transaction-Statement" in wb.sheetnames
    finally:
        wb.close()


def test_the_reader_opens_it_and_reads_every_row(tmp_path):
    res = sbm.parse(_encrypted(tmp_path / "bad.xlsx", _plain_bytes(True)), password=PW)
    assert len(res.rows) == len(ROWS)
    assert res.balance_check.ok
    assert res.meta.account_number == "00000099999"


def test_rows_are_identical_with_and_without_the_attribute(tmp_path):
    bad = sbm.parse(_encrypted(tmp_path / "bad.xlsx", _plain_bytes(True)), password=PW)
    good = sbm.parse(_encrypted(tmp_path / "good.xlsx", _plain_bytes(False)), password=PW)
    assert bad.rows == good.rows
    assert (bad.opening_balance, bad.closing_balance) == (good.opening_balance, good.closing_balance)


def test_an_unencrypted_copy_is_read_too(tmp_path):
    p = tmp_path / "plain.xlsx"
    p.write_bytes(_plain_bytes(True))
    assert len(sbm.parse(str(p)).rows) == len(ROWS)


def test_a_wrong_password_still_stops_with_the_password_message(tmp_path):
    p = _encrypted(tmp_path / "bad.xlsx", _plain_bytes(True))
    with pytest.raises(ValueError, match="(?i)password"):
        sbm.parse(p, password="wrong-password")


def test_the_workbook_handle_is_closed_after_a_read(tmp_path, monkeypatch):
    closed = []
    real = sbm._open_workbook

    def spy(path, password):
        wb = real(path, password)
        orig = wb.close
        wb.close = lambda: (closed.append(True), orig())[1]
        return wb

    monkeypatch.setattr(sbm, "_open_workbook", spy)
    sbm.parse(_encrypted(tmp_path / "bad.xlsx", _plain_bytes(True)), password=PW)
    assert closed == [True]


def test_a_sheet_with_no_dimension_tag_gives_the_same_rows(tmp_path):
    """Read-only mode follows the <dimension> tag; the reader pads every row to
    the header width, so a sheet without the tag must read identically."""
    plain = _plain_bytes(False)
    src = zipfile.ZipFile(io.BytesIO(plain))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename.startswith("xl/worksheets/sheet"):
                import re
                data = re.sub(rb"<dimension[^>]*/>", b"", data)
            dst.writestr(item, data)
    nodim = sbm.parse(_encrypted(tmp_path / "nodim.xlsx", out.getvalue()), password=PW)
    good = sbm.parse(_encrypted(tmp_path / "good.xlsx", plain), password=PW)
    assert nodim.rows == good.rows
