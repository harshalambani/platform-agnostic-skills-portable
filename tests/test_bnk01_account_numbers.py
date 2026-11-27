"""BNK-01 commit 1 follow-up: HSBC and HDFC read the statement's own account
number; several statements that show no number fall back to the one account
picked; the book-account resolution never regresses when the number fits none
of several same-bank accounts.

Synthetic data only. Layout SHAPES follow the real statements (see the module
docstrings of agents.skill_hsbc.acct_number and agents.skill_hdfc.agent).
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import gnc_book_fixture as fx  # noqa: E402
import hdfc_fixture_gen as hfx  # noqa: E402
import parse_tsv  # noqa: E402
from agents.bank_common.statements import StatementStop, read_statements  # noqa: E402
from agents.bank_contract import BankResult, BankStatementMeta  # noqa: E402
from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402
from agents.skill_hsbc.acct_number import find_account_number, find_savings_heading_number  # noqa: E402

HSBC_NO = "111222222333"            # dashed 111-222222-333 on the statement
HDFC_NO = "12345678901234"


# ===========================================================================
# HSBC: the SAVINGS heading line
# ===========================================================================

_TSV_HEADER = ["level", "page_num", "block_num", "par_num", "line_num",
               "word_num", "left", "top", "width", "height", "conf", "text"]


def _tsv(path: Path, lines: list[str]) -> Path:
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(_TSV_HEADER)
        for i, line in enumerate(lines, start=1):
            for j, word in enumerate(line.split(), start=1):
                w.writerow([5, 1, i, 1, 1, j, 40 * j, 30 * i, 35, 20, 95, word])
    return path


PAGE1 = ["HSBC Premier Savings Statement", "Mr Synthetic Person", "12 Example Road",
         "Customer Number 123-456789", "Statement period 01 Apr 2025 to 30 Apr 2025"]
HEADING = "SAVINGS PREMIER-INR 111-222222-333 Opening balance 1,000.00"
PAGE2 = ["Summary of Your Accounts", "| SAVINGS_PREMIER-INR | 111-222222-333 | 1,000.00 |",
         "Details of Your Accounts", HEADING, "Date Transaction Details Deposit Withdrawals Balance"]
PAGE3 = [HEADING, "02APR2025 TRANSFER FROM", "444-555555-666", "03APR2025 SYN SHOP 20.00"]


def _pages(tmp_path, *page_lines) -> list[Path]:
    return [_tsv(tmp_path / f"page-{i}.tsv", lines) for i, lines in enumerate(page_lines, start=1)]


def test_hsbc_the_savings_heading_number_is_taken(tmp_path):
    pages = _pages(tmp_path, PAGE1, PAGE2, PAGE3)
    assert parse_tsv.statement_account_number(pages) == HSBC_NO


def test_hsbc_the_customer_number_is_never_taken(tmp_path):
    pages = _pages(tmp_path, PAGE1, ["Details of Your Accounts", "nothing here"])
    assert parse_tsv.statement_account_number(pages) is None
    assert find_savings_heading_number([PAGE1]) is None


def test_hsbc_a_dashed_number_in_a_narration_is_never_taken(tmp_path):
    no_heading = _pages(tmp_path, PAGE1, ["Summary of Your Accounts", "02APR2025 TRANSFER FROM",
                                          "444-555555-666", "444-555555-666 SYN"])
    assert parse_tsv.statement_account_number(no_heading) is None
    # and with the heading present the narration number never wins or conflicts
    d = tmp_path / "with"
    d.mkdir()
    with_heading = _pages(d, PAGE1, PAGE2, PAGE3)
    assert parse_tsv.statement_account_number(with_heading) == HSBC_NO


def test_hsbc_two_different_heading_numbers_give_none(tmp_path):
    other = ["SAVINGS PREMIER-INR 999-888888-777 Opening balance 5.00"]
    pages = _pages(tmp_path, PAGE1, PAGE2, other)
    assert parse_tsv.statement_account_number(pages) is None


def test_hsbc_a_page_one_only_scan_misses_the_number(tmp_path):
    """Pins the bug: IMP-08's labelled, page-1-only rule finds nothing on a
    real statement; the number is only on page 2 onwards."""
    pages = _pages(tmp_path, PAGE1, PAGE2, PAGE3)
    assert find_account_number(PAGE1) is None
    assert parse_tsv.statement_account_number(pages[:1]) is None
    assert parse_tsv.statement_account_number(pages) == HSBC_NO


def test_hsbc_a_heading_form_not_seen_gives_none():
    assert find_savings_heading_number([["CURRENT PREMIER-INR 111-222222-333 x"]]) is None
    assert find_savings_heading_number([["see SAVINGS PREMIER-INR 111-222222-333"]]) is None


# ===========================================================================
# HSBC: several PDFs through the shared rule (OCR stubbed, enriched workbooks)
# ===========================================================================

_HEAD = ["Date", "Transaction Details", "Transaction Date", "Transaction Number",
         "Extra Information", "Deposit", "Withdrawals", "Balance"]


def _hsbc_wb(path: Path, rows, number):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "HSBC Savings"
    ws.append(_HEAD)
    for r in rows:
        ws.append(list(r))
    s = wb.create_sheet("Summary")
    s.append(["Metric", "Value"])
    s.append(["Account Number", number or ""])
    wb.save(path)


JAN_H = [(date(2025, 1, 1), "BALANCE BROUGHT FORWARD", None, "", "", None, None, 1000.0),
         (date(2025, 1, 5), "SYN SALARY", date(2025, 1, 5), "T1", "", 500.0, None, 1500.0)]
FEB_H = [(date(2025, 2, 1), "BALANCE BROUGHT FORWARD", None, "", "", None, None, 1500.0),
         (date(2025, 2, 3), "SYN SHOP", date(2025, 2, 3), "T2", "", None, 100.0, 1400.0)]


@pytest.fixture
def hsbc_folder(tmp_path, monkeypatch):
    from agents.skill_hsbc import agent as hagent
    made = {}

    def fake_ocr(pdf_paths, password=None):
        return made[Path(pdf_paths[0]).name]

    monkeypatch.setattr(hagent, "_run_ocr_pipeline", fake_ocr)
    up = tmp_path / "up"
    up.mkdir()

    def add(name, rows, number):
        (up / name).write_bytes(b"%PDF-1.4 synthetic")
        wb = tmp_path / (name + ".xlsx")
        _hsbc_wb(wb, rows, number)
        made[name] = wb

    return up, add, hagent


def test_hsbc_several_pdfs_with_the_same_number_join_like_the_old_consolidation(hsbc_folder):
    up, add, hagent = hsbc_folder
    add("a_jan.pdf", JAN_H, HSBC_NO)
    add("b_feb.pdf", FEB_H, HSBC_NO)
    got = hagent.HSBCSkill().parse(up)
    skill = hagent.HSBCSkill()
    jan = skill.parse_one(up / "a_jan.pdf").rows
    feb = skill.parse_one(up / "b_feb.pdf").rows
    assert got.meta.account_number == HSBC_NO
    # the old consolidation: date order, the later statement's carry-forward row dropped
    assert got.rows == jan + [r for r in feb if r["Description"] != "BALANCE BROUGHT FORWARD"]
    assert not any("account number" in w.lower() for w in got.warnings)


def test_hsbc_two_copies_of_the_same_statement_are_kept_once(hsbc_folder):
    up, add, hagent = hsbc_folder
    add("jan.pdf", JAN_H, HSBC_NO)
    add("jan (1).pdf", JAN_H, HSBC_NO)
    got = hagent.HSBCSkill().parse(up)
    single = hagent.HSBCSkill().parse_one(up / "jan.pdf").rows
    assert got.rows == single
    assert any(w.startswith("Overlapping statements") for w in got.warnings)


def test_hsbc_several_pdfs_without_any_number_are_one_account_with_a_warning(hsbc_folder):
    up, add, hagent = hsbc_folder
    add("a_jan.pdf", JAN_H, None)
    add("b_feb.pdf", FEB_H, None)
    got = hagent.HSBCSkill().parse(up)
    assert len(got.rows) == 3
    assert any("No statement shows an account number" in w for w in got.warnings)


# ===========================================================================
# read_statements: fallback A (stub bank)
# ===========================================================================

def _row(d, desc, dep, wd, bal):
    return {"Date": d, "Transaction ID": "", "Description": desc, "Account": "",
            "Deposit": str(dep), "Withdrawal": str(wd), "Balance": str(bal), "Currency": "INR"}


class Stub:
    bank_key = "stub"

    def __init__(self, by):
        self.by = by

    def formats(self):
        return (".csv",)

    def parse_one(self, path, password=None):
        return self.by[Path(path).name]


def _res(rows, number):
    return BankResult(rows=rows, bank_key="stub", warnings=[],
                      meta=BankStatementMeta(bank_key="stub", account_number=number))


A_ROWS = [_row("2026-01-05", "SALARY", 1000, 0, 1500)]
B_ROWS = [_row("2026-02-03", "SHOP", 0, 100, 1400)]
C_ROWS = [_row("2026-03-03", "FUEL", 0, 100, 1300)]


def _up(tmp_path, names):
    d = tmp_path / "s"
    d.mkdir()
    for n in names:
        (d / n).write_text("x")
    return d


def test_fallback_none_show_a_number_takes_them_as_one_account_with_a_warning(tmp_path):
    by = {"a.csv": _res(A_ROWS, None), "b.csv": _res(B_ROWS, None)}
    got = read_statements(Stub(by), _up(tmp_path, by))
    assert [r["Description"] for r in got.rows] == ["SALARY", "SHOP"]
    assert any("No statement shows an account number" in w for w in got.warnings)
    assert not any(w.startswith("Skipped") for w in got.warnings)


def test_fallback_some_show_a_number_and_some_do_not_stops_naming_the_files(tmp_path):
    by = {"a.csv": _res(A_ROWS, "11112222"), "b.csv": _res(B_ROWS, None), "c.csv": _res(C_ROWS, None)}
    with pytest.raises(StatementStop) as exc:
        read_statements(Stub(by), _up(tmp_path, by))
    msg = str(exc.value)
    assert "`b.csv`" in msg and "`c.csv`" in msg and "`a.csv`" not in msg


def test_fallback_all_show_a_number_is_as_built(tmp_path):
    by = {"a.csv": _res(A_ROWS, "11112222"), "b.csv": _res(B_ROWS, "11112222")}
    got = read_statements(Stub(by), _up(tmp_path, by))
    assert len(got.rows) == 2
    assert not any("No statement shows" in w for w in got.warnings)


def test_fallback_a_single_file_is_untouched(tmp_path):
    by = {"a.csv": _res(A_ROWS, None)}
    got = read_statements(Stub(by), _up(tmp_path, by))
    assert got.rows == A_ROWS and got.warnings == []


# ===========================================================================
# HDFC: the labelled number, nothing else
# ===========================================================================

def _hdfc_pdf(path: Path, header_lines: list[str]):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    _, h = A4
    c.setFont("Helvetica", 9)
    y = h - 50
    for line in header_lines + hfx._pdf_lines():
        c.drawString(40, y, line)
        y -= 14
    c.showPage()
    c.save()


def _hdfc_xlsx(path: Path, preamble: list[list[str]]):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in preamble:
        ws.append(r)
    ws.append(["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.",
               "Deposit Amt.", "Closing Balance"])
    for posting, value, narr, ref, wdl, dep, bal in hfx.SYN_TRANSACTIONS:
        ws.append([posting, narr, ref, value, wdl, dep, bal])
    wb.save(path)


def _hdfc():
    from agents.skill_hdfc.agent import HDFCBankSkill
    return HDFCBankSkill()


RUN_TOGETHER = ["Page No .: 1 Statement of account",
                "CustID : 87654321 Branch Synthetic",
                f"AccountNo : {HDFC_NO} Statementfrom 01/04/2025"]


def test_hdfc_the_run_together_account_no_in_a_pdf_is_read(tmp_path):
    f = tmp_path / "s.pdf"
    _hdfc_pdf(f, RUN_TOGETHER)
    assert _hdfc().parse_one(f).meta.account_number == HDFC_NO


def test_hdfc_custid_alone_is_never_taken(tmp_path):
    f = tmp_path / "s.pdf"
    _hdfc_pdf(f, ["CustID : 87654321 Branch Synthetic"])
    assert _hdfc().parse_one(f).meta.account_number is None


PREAMBLE = [["Statement of Account"], [f"Account No :{HDFC_NO}"], ["A/C Open Date :01/01/2010"],
            ["Cust ID :87654321"], [], ["****"] * 7]


def test_hdfc_the_xls_preamble_account_no_is_read(tmp_path):
    f = tmp_path / "s.xlsx"
    _hdfc_xlsx(f, PREAMBLE)
    assert _hdfc().parse_one(f).meta.account_number == HDFC_NO


def test_hdfc_ac_open_date_and_custid_are_never_taken(tmp_path):
    f = tmp_path / "s.xlsx"
    _hdfc_xlsx(f, [["A/C Open Date :01/01/2010"], ["Cust ID :87654321"], ["****"] * 7])
    assert _hdfc().parse_one(f).meta.account_number is None


def test_hdfc_a_headerless_xls_and_a_csv_carry_no_number(tmp_path):
    f = tmp_path / "s.xlsx"
    _hdfc_xlsx(f, [])
    assert _hdfc().parse_one(f).meta.account_number is None
    c = tmp_path / "s.csv"
    c.write_text(hfx.build_csv_text(), encoding="utf-8")
    assert _hdfc().parse_one(c).meta.account_number is None


def test_hdfc_reading_the_number_changes_no_row(tmp_path):
    a = tmp_path / "with.xlsx"
    b = tmp_path / "without.xlsx"
    _hdfc_xlsx(a, PREAMBLE)
    _hdfc_xlsx(b, [["Statement of Account"], [], ["****"] * 7])
    ra, rb = _hdfc().parse_one(a), _hdfc().parse_one(b)
    assert ra.meta.account_number == HDFC_NO and rb.meta.account_number is None
    assert ra.rows == rb.rows


def test_hdfc_two_copies_of_a_headerless_export_are_kept_once_with_both_warnings(tmp_path):
    up = tmp_path / "up"
    up.mkdir()
    for n in ("one.csv", "two.csv"):
        (up / n).write_text(hfx.build_csv_text(), encoding="utf-8")
    got = _hdfc().parse(up)
    single = _hdfc().parse_one(up / "one.csv").rows
    assert got.rows == single
    assert any("No statement shows an account number" in w for w in got.warnings)
    assert any(w.startswith("Overlapping statements") for w in got.warnings)


def test_hdfc_two_numbered_files_of_one_account_join(tmp_path):
    up = tmp_path / "up"
    up.mkdir()
    for n in ("one.xlsx", "two.xlsx"):
        _hdfc_xlsx(up / n, PREAMBLE)
    got = _hdfc().parse(up)
    assert got.meta.account_number == HDFC_NO
    assert len(got.rows) == len(hfx.SYN_TRANSACTIONS)


# ===========================================================================
# Book-account resolution when the number fits none of several accounts
# ===========================================================================

def _book(tmp_path, names, txns=()):
    accs = [fx.account_xml("root", "Root Account", "ROOT", None),
            fx.account_xml("assets", "Assets", "ASSET", "root", ["placeholder"]),
            fx.account_xml("cab", "Cash and Bank", "ASSET", "assets", ["placeholder"]),
            fx.account_xml("eq", "Equity", "EQUITY", "root", ["placeholder"]),
            fx.account_xml("obe", "Opening Balances", "EQUITY", "eq",
                           [("equity-type", "opening-balance")])]
    for aid, name in names:
        accs.append(fx.account_xml(aid, name, "BANK", "cab"))
    return fx.write_book(tmp_path / "b.gnucash", accs, list(txns))


def _bal(acc, paise):
    return fx.txn_xml("OPEN", "2025-03-01", [(acc, paise), ("obe", -paise)])


NUMBERED = ("num", "HDFC Bank - 111122223333")
PLAIN = ("plain", "HDFC Bank Savings")


def test_resolution_a_number_that_fits_one_account_picks_it(tmp_path):
    book = _book(tmp_path, [("num", f"HDFC Bank - {HDFC_NO}"), PLAIN])
    res = pipe._get_gnucash_account_balance(book, "HDFC", HDFC_NO)
    assert res["found"] is True and res["account_name"].endswith(HDFC_NO)


def test_resolution_no_fit_but_evidence_picks_the_account_without_digits(tmp_path):
    book = _book(tmp_path, [NUMBERED, PLAIN], [_bal("plain", 100000), _bal("num", 777700)])
    res = pipe._get_gnucash_account_balance(
        book, "HDFC", HDFC_NO, opening_balance=1000.00, start_date="2025-04-01")
    assert res["found"] is True and res["ambiguous"] is False
    assert res["account_name"].endswith("HDFC Bank Savings")
    assert "account name" in (res["match_warning"] or "")


def test_resolution_no_fit_and_no_evidence_asks(tmp_path):
    book = _book(tmp_path, [NUMBERED, PLAIN], [_bal("plain", 100000), _bal("num", 777700)])
    res = pipe._get_gnucash_account_balance(book, "HDFC", HDFC_NO)
    assert res["found"] is False and res["ambiguous"] is True


def test_resolution_never_picks_an_account_carrying_other_digits(tmp_path):
    # the numbered account carries the matching opening balance; the plain one does not
    book = _book(tmp_path, [NUMBERED, PLAIN], [_bal("num", 100000), _bal("plain", 777700)])
    res = pipe._get_gnucash_account_balance(
        book, "HDFC", HDFC_NO, opening_balance=1000.00, start_date="2025-04-01")
    assert res["found"] is False
    assert "111122223333" not in (res["account_name"] or "")


def test_resolution_all_candidates_with_other_digits_still_refuse(tmp_path):
    book = _book(tmp_path, [NUMBERED, ("num2", "HDFC Bank - 444455556666")])
    res = pipe._get_gnucash_account_balance(book, "HDFC", HDFC_NO)
    assert res["found"] is False and not res["account_name"]


def test_resolution_a_single_candidate_is_unchanged(tmp_path):
    book = _book(tmp_path, [PLAIN])
    res = pipe._get_gnucash_account_balance(book, "HDFC", HDFC_NO)
    assert res["found"] is True and res["account_name"].endswith("HDFC Bank Savings")
    assert "fell back to name match" in res["match_warning"]


# ===========================================================================
# The labelled finder never joins a separate digit group to the number
# ===========================================================================

@pytest.mark.parametrize("line", [
    f"Account No : {HDFC_NO} 01/04/2025",          # a date
    f"AccountNo : {HDFC_NO} 123456 cheque",         # a cheque number
    f"Account No :{HDFC_NO} 444-555555-666",        # a 3-6-3 narration number
    f"Account No :{HDFC_NO} 12 04 2025",            # a date written with spaces
])
def test_finder_a_separate_digit_group_is_never_joined_to_the_number(line):
    assert find_account_number([line]) == HDFC_NO


@pytest.mark.parametrize("line,want", [
    ("Account No : 013 065 123 456", "013065123456"),
    ("A/C No. 0130 6512 3456", "013065123456"),
    ("Account Number 0130-6512-3456", "013065123456"),
    ("Account No : 013 065 123 456 01/04/2025", "013065123456"),
    ("Account No : 0130 6512 3456 14:30", "013065123456"),
])
def test_finder_a_labelled_number_with_spaces_or_dashes_inside_is_still_read(line, want):
    assert find_account_number([line]) == want
