"""
tests/test_gnucash_coverage.py -- agents.skill_gnucash_coverage.agent (the
Coverage Gap Detector). Deterministic, no LLM, no network, no gradio, no
native binaries -- pure XML-in, workbook-out.

Synthetic-only fixtures (a single made-up "Test Person", no real taxpayer
names, PANs, or data files). All books are written into tmp_path.

Covers (COV-02):
  * only BANK and CREDIT accounts are checked by default; ASSET, LIABILITY
    and EQUITY accounts never appear, and with the opt-in on, ASSET and
    LIABILITY appear only in the labelled "Other accounts" section;
  * only the book's financial year is checked: no month before it, none
    after it (or after today), even with history from 2009;
  * a book with no known year uses the year of its latest transaction and
    says so in the opening line;
  * an interior empty month is reported; months before an account's first
    transaction are not; months after its last transaction come first and
    read "no transactions since <Mon YYYY>";
  * a quiet account is told "check before importing";
  * a complete account has no gap line but is still on "Accounts checked";
  * no HIGH/LOW/median/Trailing wording reaches the user;
  * an FY-boundary gap is suppressed when the adjacent FY's registered book
    holds a transaction for the same account in that exact month;
  * the frozen-app cold import still works (COV-01).
"""
from __future__ import annotations

import gzip
import sys
from datetime import date
from pathlib import Path

import pytest
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ITR_SCRIPTS = SRC / "agents" / "skill_itr_workbook" / "scripts"
COVERAGE_DIR = SRC / "agents" / "skill_gnucash_coverage"

# NOTE: COVERAGE_DIR is deliberately NOT added to sys.path here. The frozen
# PortableApps build never puts a skill's own folder on sys.path, so a test
# that did so was hand-building a condition the real app never has -- see
# test_run_works_without_own_dir_on_syspath below, which pins this down as a
# regression guard (ledger item COV-01).
for _p in (str(SRC), str(ITR_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import configs  # noqa: E402
from agents.skill_gnucash_coverage import agent as cov  # noqa: E402

PAN = "ABCDE1234X"

# ---------------------------------------------------------------------------
# Synthetic .gnucash book builder (accounts + transactions).
# ---------------------------------------------------------------------------

_NS_DECL = (
    'xmlns:gnc="http://www.gnucash.org/XML/gnc" '
    'xmlns:act="http://www.gnucash.org/XML/act" '
    'xmlns:trn="http://www.gnucash.org/XML/trn" '
    'xmlns:split="http://www.gnucash.org/XML/split" '
    'xmlns:ts="http://www.gnucash.org/XML/ts" '
    'xmlns:slot="http://www.gnucash.org/XML/slot"'
)

# Fixed account IDs shared across both FY books so account.path matching in
# the FY-boundary check lines up.
ACC = {
    "root": ("Root Account", "ROOT", None, None),
    "assets": ("Assets", "ASSET", "root", [("placeholder", "true")]),
    "bank_parent": ("Bank", "ASSET", "assets", [("placeholder", "true")]),
    "bank": ("TestBank", "BANK", "bank_parent", None),
    "savings": ("SavingsQuarterly", "BANK", "bank_parent", None),
    "liab": ("Liabilities", "LIABILITY", "root", [("placeholder", "true")]),
    "card": ("TestCard", "CREDIT", "liab", None),
    "equity": ("Equity", "EQUITY", "root", [("placeholder", "true")]),
    "ob": ("Opening Balances", "EQUITY", "equity",
           [("equity-type", "opening-balance")]),
    "expense": ("Misc Expense", "EXPENSE", "root", None),
}


def _account_xml(aid: str) -> str:
    name, atype, parent, slots = ACC[aid]
    parts = [
        '  <gnc:account version="2.0.0">',
        f"    <act:name>{name}</act:name>",
        f'    <act:id type="guid">{aid}</act:id>',
        f"    <act:type>{atype}</act:type>",
    ]
    if parent is not None:
        parts.append(f'    <act:parent type="guid">{parent}</act:parent>')
    if slots:
        parts.append("    <act:slots>")
        for key, value in slots:
            parts.append(
                "      <slot>"
                f"<slot:key>{key}</slot:key>"
                f'<slot:value type="string">{value}</slot:value>'
                "</slot>"
            )
        parts.append("    </act:slots>")
    parts.append("  </gnc:account>")
    return "\n".join(parts)


def _txn_xml(date_str: str, legs: list[str], desc: str = "txn") -> str:
    """legs: account-ids for a simple N-way split (each gets an equal and
    opposite dummy value -- date-bucketing is all this skill reads)."""
    splits = []
    for i, aid in enumerate(legs):
        value = "10000/100" if i == 0 else "-10000/100"
        splits.append(
            "   <trn:split>"
            f"<split:account>{aid}</split:account>"
            f"<split:value>{value}</split:value>"
            "</trn:split>"
        )
    return (
        '  <gnc:transaction version="2.0.0">\n'
        f"   <trn:description>{desc}</trn:description>\n"
        f"   <trn:date-posted><ts:date>{date_str} 00:00:00 +0000</ts:date></trn:date-posted>\n"
        "   <trn:splits>\n" + "\n".join(splits) + "\n   </trn:splits>\n"
        "  </gnc:transaction>"
    )


def _book_xml(account_ids: list[str], txns: list[str]) -> str:
    body = [_account_xml(a) for a in account_ids] + txns
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<gnc-v2 {_NS_DECL}>\n"
        '<gnc:book version="2.0.0">\n'
        + "\n".join(body)
        + "\n</gnc:book>\n</gnc-v2>\n"
    )


def _write_gz(path: Path, xml: str) -> None:
    path.write_bytes(gzip.compress(xml.encode("utf-8")))


def _dates_in_month(year: int, month: int, days: list[int]) -> list[str]:
    return [f"{year:04d}-{month:02d}-{d:02d}" for d in days]


# ---------------------------------------------------------------------------
# FY2024-25 book: TestBank (busy, one interior gap, one trailing gap),
# SavingsQuarterly (quiet, LOW confidence), TestCard (credit-card scope).
# ---------------------------------------------------------------------------

_ALL_ACCOUNTS = ["root", "assets", "bank_parent", "bank", "savings",
                 "liab", "card", "equity", "ob", "expense"]


def _busy_month_txns(aid: str, year: int, month: int, n: int = 5) -> list[str]:
    days = [1, 5, 10, 15, 20][:n]
    return [_txn_xml(d, [aid, "expense"]) for d in _dates_in_month(year, month, days)]


def _build_fy2425_book() -> str:
    txns = []
    # TestBank: opening-balance-only genesis month (2024-04), then 5
    # non-OB txns/month straight through except October (interior gap) and
    # March (trailing gap, also the FY's own last calendar month).
    txns.append(_txn_xml("2024-04-05", ["bank", "ob"], desc="opening balance"))
    for (y, m) in [(2024, 5), (2024, 6), (2024, 7), (2024, 8), (2024, 9),
                   (2024, 11), (2024, 12), (2025, 1), (2025, 2)]:
        txns += _busy_month_txns("bank", y, m)
    # (2024, 10) and (2025, 3) deliberately left with zero TestBank txns.

    # SavingsQuarterly: one interest credit every quarter -- quiet cadence,
    # quiet cadence.
    for (y, m, d) in [(2024, 4, 10), (2024, 7, 10), (2024, 10, 10), (2025, 1, 10)]:
        txns.append(_txn_xml(f"{y:04d}-{m:02d}-{d:02d}", ["savings", "expense"],
                              desc="quarterly interest"))

    # TestCard: monthly except September (its own interior gap), to prove
    # a LIABILITY/CREDIT account is in scope and labelled correctly.
    for (y, m) in [(2024, 5), (2024, 6), (2024, 7), (2024, 8),
                   (2024, 10), (2024, 11), (2024, 12), (2025, 1), (2025, 2)]:
        txns += _busy_month_txns("card", y, m, n=4)

    return _book_xml(_ALL_ACCOUNTS, txns)


def _build_fy2526_adjacent_book() -> str:
    """Only what's needed for the FY-boundary check: a TestBank transaction
    dated 2025-03-28 -- i.e. filed into the FY2025-26 book even though the
    date falls in FY2024-25's own last month, the "wrong side of the
    rollover" scenario the boundary check exists to catch."""
    txns = [_txn_xml("2025-03-28", ["bank", "expense"], desc="misfiled into next book")]
    # Give the book at least one txn safely inside its own FY too.
    txns.append(_txn_xml("2025-04-15", ["bank", "expense"], desc="normal FY25-26 txn"))
    return _book_xml(_ALL_ACCOUNTS, txns)


@pytest.fixture()
def fy2425_book(tmp_path) -> Path:
    p = tmp_path / "TestPersonCoverage2425.gnucash"
    _write_gz(p, _build_fy2425_book())
    return p


@pytest.fixture()
def fy2526_book(tmp_path) -> Path:
    p = tmp_path / "TestPersonCoverage2526.gnucash"
    _write_gz(p, _build_fy2526_adjacent_book())
    return p


def _entities_path(tmp_path: Path, fy2425: Path, fy2526: Path | None = None) -> Path:
    books = {"2024-25": str(fy2425)}
    if fy2526 is not None:
        books["2025-26"] = str(fy2526)
    entities = {
        "TestPersonCoverage": configs.EntityProfile(
            key="TestPersonCoverage", name="Test Person Coverage", pan=PAN,
            status="Individual", books=books,
        ),
    }
    out = tmp_path / "entities.yaml"
    out.write_text(configs.dump_entities(entities), encoding="utf-8")
    return out


def _run_scan(books, tmp_path, entities_path=None, include_other="no"):
    out_path = tmp_path / "out.xlsx"
    entities_path = entities_path if entities_path is not None else (tmp_path / "no-such-entities.yaml")
    reply = cov.run(
        books=books if isinstance(books, str) else "\n".join(str(b) for b in books),
        output_path=str(out_path),
        entities_path=str(entities_path),
        include_other=include_other,
    )
    return reply, out_path


def _scan(books, tmp_path, entities_path=None, include_other=False, today=None):
    ep = entities_path if entities_path is not None else (tmp_path / "no-such-entities.yaml")
    return cov.scan_books([str(b) for b in books], ep, include_other, today=today)


def _result(scan, account_path):
    return next(r for r in scan.results if r.account_path == account_path)


def _sheet_rows(wb, name):
    return [[c.value for c in row] for row in wb[name].iter_rows()]


def _all_user_text(reply, out_path):
    """The reply plus every cell of the workbook, as one string."""
    wb = load_workbook(out_path)
    cells = [str(v) for ws in wb.worksheets for row in ws.iter_rows(values_only=True)
             for v in row if v is not None]
    return reply + "\n" + "\n".join(cells)


# ---------------------------------------------------------------------------
# A second synthetic book for the scope / window tests (FY 2025-26).
# ---------------------------------------------------------------------------

_EXTRA = {
    "invest": ("Investments", "ASSET", "assets", None),
    "late": ("LateBank", "BANK", "bank_parent", None),
    "taxpay": ("Tax Payable", "LIABILITY", "liab", None),
    "capital": ("Capital", "EQUITY", "equity", None),
}
ACC.update(_EXTRA)
_SCOPE_ACCOUNTS = ["root", "assets", "bank_parent", "bank", "savings", "late",
                   "invest", "liab", "card", "taxpay", "equity", "capital",
                   "ob", "expense"]

_FY2526_MONTHS = [(2025, m) for m in range(4, 13)] + [(2026, m) for m in (1, 2, 3)]


def _build_scope_book() -> str:
    txns = []
    # Old history from 2009 on the main bank: must never leak into the check.
    for y in (2009, 2010, 2015):
        txns.append(_txn_xml(f"{y}-06-10", ["bank", "expense"], desc="old"))
    # TestBank: every month of FY 2025-26 except Aug 2025.
    for (y, m) in _FY2526_MONTHS:
        if (y, m) != (2025, 8):
            txns += _busy_month_txns("bank", y, m, n=2)
    # SavingsQuarterly: a complete bank account, nothing missing.
    for (y, m) in _FY2526_MONTHS:
        txns += _busy_month_txns("savings", y, m, n=2)
    # LateBank: starts in Aug 2025, nothing missing after that.
    for (y, m) in _FY2526_MONTHS:
        if (y, m) >= (2025, 8):
            txns += _busy_month_txns("late", y, m, n=2)
    # TestCard: stops after Dec 2025 (trailing gap Jan-Mar 2026).
    for (y, m) in _FY2526_MONTHS:
        if (y, m) <= (2025, 12):
            txns += _busy_month_txns("card", y, m, n=2)
    # Non-statement accounts with one entry only.
    for aid in ("invest", "taxpay", "capital"):
        txns.append(_txn_xml("2025-04-10", [aid, "expense"], desc="single entry"))
    return _book_xml(_SCOPE_ACCOUNTS, txns)


@pytest.fixture()
def scope_book(tmp_path) -> Path:
    p = tmp_path / "TestScope2526.gnucash"
    _write_gz(p, _build_scope_book())
    return p


@pytest.fixture()
def unregistered_book(tmp_path) -> Path:
    """Same content, but a filename that carries no financial year."""
    p = tmp_path / "TestScopeUnregistered.gnucash"
    _write_gz(p, _build_scope_book())
    return p


# ---------------------------------------------------------------------------
# Core scenarios against the FY2024-25 book (no registered entity, so the
# FY-boundary check never fires and the March trailing gap is un-suppressed).
# ---------------------------------------------------------------------------

def test_interior_zero_month_is_reported(fy2425_book, tmp_path):
    scan = _scan([fy2425_book], tmp_path)
    r = _result(scan, "Assets:Bank:TestBank")
    assert "2024-10" in r.gap_months
    assert "Oct 2024" in r.months_text


def test_month_before_first_transaction_not_reported(fy2425_book, tmp_path):
    scan = _scan([fy2425_book], tmp_path)
    r = _result(scan, "Assets:Bank:TestBank")
    reported = set(r.gap_months) | set(r.trailing_months)
    assert not any(m < "2024-04" for m in reported)
    # April itself is not a gap: the opening-balance entry is a transaction.
    assert "2024-04" not in reported


def test_trailing_gap_is_listed_first_and_worded_since(fy2425_book, tmp_path):
    reply, out_path = _run_scan([fy2425_book], tmp_path)
    r = _result(_scan([fy2425_book], tmp_path), "Assets:Bank:TestBank")
    assert r.trailing_months == ["2025-03"]
    assert "2024-10" in r.gap_months and "2024-10" not in r.trailing_months
    assert r.months_text.startswith("Mar 2025")
    line = next(ln for ln in reply.splitlines() if "Assets:Bank:TestBank" in ln)
    assert "no transactions since feb 2025" in line.lower()
    assert "probably not imported" in line


def test_quiet_account_gets_the_check_before_importing_wording(fy2425_book, tmp_path):
    scan = _scan([fy2425_book], tmp_path)
    quiet = _result(scan, "Assets:Bank:SavingsQuarterly")
    busy = _result(scan, "Assets:Bank:TestBank")
    assert quiet.quiet and not busy.quiet
    assert "this account is quiet, so these may be months with no activity - check before importing" \
        in quiet.meaning.lower()
    assert "quiet" not in busy.meaning.lower()


def test_credit_card_account_is_in_scope(fy2425_book, tmp_path):
    scan = _scan([fy2425_book], tmp_path)
    card = _result(scan, "Liabilities:TestCard")
    assert card.is_core
    assert "2024-09" in card.gap_months


def test_opening_line_is_plain_and_exact(fy2425_book, tmp_path):
    reply, _out = _run_scan([fy2425_book], tmp_path)
    assert reply.splitlines()[0] == (
        "3 bank and card accounts checked for FY 2024-25 (Apr 2024 - Mar 2025); "
        "3 have months with no transactions."
    )


def test_workbook_has_the_two_plain_sheets(fy2425_book, tmp_path):
    _reply, out_path = _run_scan([fy2425_book], tmp_path)
    wb = load_workbook(out_path)
    assert wb.sheetnames == ["Missing months", "Accounts checked"]
    rows = _sheet_rows(wb, "Missing months")
    assert rows[0][0].startswith("3 bank and card accounts checked for FY 2024-25")
    assert rows[2][:3] == ["Account", "Months with no transactions", "What it means"]
    names = {r[0] for r in rows[3:]}
    assert {"Assets:Bank:TestBank", "Assets:Bank:SavingsQuarterly", "Liabilities:TestCard"} <= names
    checked = _sheet_rows(wb, "Accounts checked")
    assert {r[0] for r in checked[1:]} == names


# ---------------------------------------------------------------------------
# COV-02 negative tests.
# ---------------------------------------------------------------------------

def test_default_scope_never_lists_asset_liability_or_equity(scope_book, tmp_path):
    reply, out_path = _run_scan([scope_book], tmp_path)
    scan = _scan([scope_book], tmp_path)
    assert all(r.is_core for r in scan.results)
    wb = load_workbook(out_path)
    for sheet in ("Missing months", "Accounts checked"):
        names = {r[0] for r in _sheet_rows(wb, sheet)}
        for bad in ("Assets:Investments", "Liabilities:Tax Payable", "Equity:Capital"):
            assert bad not in names
    for bad in ("Investments", "Tax Payable", "Capital", "Other accounts"):
        assert bad not in reply


def test_opt_in_puts_asset_and_liability_only_in_other_accounts_section(scope_book, tmp_path):
    reply, out_path = _run_scan([scope_book], tmp_path, include_other="yes")
    scan = _scan([scope_book], tmp_path, include_other=True)
    core_titles = {r.account_path for r in scan.results if r.is_core}
    other_titles = {r.account_path for r in scan.results if not r.is_core}
    assert other_titles == {"Assets:Investments", "Liabilities:Tax Payable"}
    assert "Equity:Capital" not in core_titles | other_titles
    assert not ({"Assets:Investments", "Liabilities:Tax Payable"} & core_titles)
    # In the reply the other accounts sit below the "Other accounts" label.
    head, _sep, tail = reply.partition("Other accounts")
    assert _sep
    assert "Investments" not in head and "Tax Payable" not in head
    assert "Investments" in tail and "Tax Payable" in tail
    # Same in the workbook: the labelled row comes before them.
    rows = _sheet_rows(load_workbook(out_path), "Missing months")
    label_at = next(i for i, r in enumerate(rows) if str(r[0]).startswith("Other accounts"))
    for i, r in enumerate(rows):
        if r[0] in ("Assets:Investments", "Liabilities:Tax Payable"):
            assert i > label_at
        if r[0] in ("Assets:Bank:TestBank", "Liabilities:TestCard"):
            assert i < label_at


def test_no_month_before_the_year_or_after_its_end_with_old_history(scope_book, tmp_path):
    scan = _scan([scope_book], tmp_path, include_other=True)
    bank = _result(scan, "Assets:Bank:TestBank")
    assert bank.fy_key == "2025-26"
    assert bank.gap_months == ["2025-08"]      # not Apr 2009 ... Mar 2025
    assert bank.trailing_months == []
    for r in scan.results:
        for m in list(r.gap_months) + list(r.trailing_months):
            assert "2025-04" <= m <= "2026-03", (r.account_path, m)


def test_months_after_today_are_never_reported(scope_book, tmp_path):
    scan = _scan([scope_book], tmp_path, today=date(2025, 8, 15))
    for r in scan.results:
        for m in list(r.gap_months) + list(r.trailing_months):
            assert m <= "2025-08", (r.account_path, m)
    assert "checked up to Aug 2025" in scan.opening_line()


def test_complete_account_has_no_gap_line_but_is_listed_as_checked(scope_book, tmp_path):
    reply, out_path = _run_scan([scope_book], tmp_path)
    assert not any("SavingsQuarterly" in ln for ln in reply.splitlines())
    wb = load_workbook(out_path)
    assert "Assets:Bank:SavingsQuarterly" not in {r[0] for r in _sheet_rows(wb, "Missing months")}
    checked = {r[0]: r for r in _sheet_rows(wb, "Accounts checked")}
    assert "Assets:Bank:SavingsQuarterly" in checked
    assert checked["Assets:Bank:SavingsQuarterly"][5] == "12 of 12"


def test_account_starting_mid_year_is_not_missing_earlier_months(scope_book, tmp_path):
    scan = _scan([scope_book], tmp_path)
    late = _result(scan, "Assets:Bank:LateBank")
    assert not late.has_gaps
    assert late.months_checked == 8


def test_credit_card_trailing_gap_uses_since_wording(scope_book, tmp_path):
    reply, _out = _run_scan([scope_book], tmp_path)
    line = next(ln for ln in reply.splitlines() if "Liabilities:TestCard" in ln)
    assert "no transactions since dec 2025 - the latest statement(s) probably not imported" \
        in line.lower()
    assert line.index("Jan 2026") < len(line)


def test_user_text_never_uses_grading_words(fy2425_book, scope_book, tmp_path):
    for include in ("no", "yes"):
        reply, out_path = _run_scan([fy2425_book, scope_book], tmp_path, include_other=include)
        text = _all_user_text(reply, out_path)
        for banned in ("HIGH", "LOW", "median", "Trailing", "TRAILING", "Confidence"):
            assert banned not in text, banned
        assert " high " not in text.lower() and "median" not in text.lower()


def test_unregistered_book_uses_latest_transaction_year_and_says_so(unregistered_book, tmp_path):
    scan = _scan([unregistered_book], tmp_path)
    assert scan.summaries[0].inferred
    assert scan.summaries[0].fy_key == "2025-26"
    reply, _out = _run_scan([unregistered_book], tmp_path)
    first = reply.splitlines()[0]
    assert "FY 2025-26" in first
    assert "not registered to a financial year" in first
    assert "latest transaction" in first
    # And a book whose year IS known does not carry the sentence.
    reply2, _o = _run_scan([tmp_path / "TestScope2526.gnucash"], tmp_path) \
        if (tmp_path / "TestScope2526.gnucash").exists() else (reply, None)
    if (tmp_path / "TestScope2526.gnucash").exists():
        assert "not registered" not in reply2.splitlines()[0]


# ---------------------------------------------------------------------------
# FY-boundary suppression: needs the entity registered with BOTH the
# FY2024-25 book and the adjacent FY2025-26 book.
# ---------------------------------------------------------------------------

def test_fy_boundary_gap_suppressed_via_adjacent_book(fy2425_book, fy2526_book, tmp_path):
    entities_path = _entities_path(tmp_path, fy2425_book, fy2526_book)
    scan = _scan([fy2425_book], tmp_path, entities_path=entities_path)
    r = _result(scan, "Assets:Bank:TestBank")
    # March would be a trailing boundary gap, but the adjacent FY2025-26 book
    # has a TestBank transaction dated 2025-03-28 -> suppressed.
    assert "2025-03" not in r.gap_months and "2025-03" not in r.trailing_months
    assert "2024-10" in r.gap_months
    assert r.suppressed == 1
    reply = scan.reply_text("x.xlsx")
    assert "neighbouring year's book" in reply


def test_fy_boundary_gap_reported_without_adjacent_evidence(fy2425_book, tmp_path):
    """Same entity registered, but with NO FY2025-26 book at all -- nothing
    to consult, so March must be reported (not silently dropped)."""
    entities_path = _entities_path(tmp_path, fy2425_book, fy2526=None)
    scan = _scan([fy2425_book], tmp_path, entities_path=entities_path)
    r = _result(scan, "Assets:Bank:TestBank")
    assert "2025-03" in r.trailing_months
    assert r.suppressed == 0


# ---------------------------------------------------------------------------
# Regression guard for ledger item COV-01: the frozen PortableApps build
# never puts a skill's own folder on sys.path, so agent.py's excel_writer
# import must resolve as a package import, not a bare-module lookup that
# only works when COVERAGE_DIR happens to be on sys.path. Proves the
# ABSENCE of the old failure (ModuleNotFoundError), not just the happy path.
# ---------------------------------------------------------------------------

def test_run_works_without_own_dir_on_syspath(fy2425_book, tmp_path):
    """Strip the skill's own directory (and any bare 'excel_writer' module)
    out of the import machinery, reimport agent.py fresh, and confirm
    run() still produces a workbook instead of raising ModuleNotFoundError.
    This is exactly the condition of the shipped, frozen app -- COVERAGE_DIR
    is never on sys.path there."""
    import importlib

    coverage_dir_str = str(COVERAGE_DIR)
    assert coverage_dir_str not in sys.path, (
        "COVERAGE_DIR must not be on sys.path for this test to be a real "
        "regression guard -- the module-level sys.path setup above should "
        "already keep it off."
    )

    # Defensively evict anything a previous test/run could have cached, so
    # this test proves the import works cold, exactly as the frozen app's
    # first launch of the skill would see it.
    for mod_name in (
        "excel_writer",
        "agents.skill_gnucash_coverage.excel_writer",
        "agents.skill_gnucash_coverage.agent",
    ):
        sys.modules.pop(mod_name, None)

    fresh_cov = importlib.import_module("agents.skill_gnucash_coverage.agent")

    out_path = tmp_path / "cold_import_out.xlsx"
    entities_path = tmp_path / "no-such-entities.yaml"
    try:
        summary = fresh_cov.run(
            books=str(fy2425_book),
            output_path=str(out_path),
            entities_path=str(entities_path),
        )
    except ModuleNotFoundError as exc:
        pytest.fail(
            f"run() raised ModuleNotFoundError with COVERAGE_DIR off "
            f"sys.path -- the exact frozen-app crash this test guards "
            f"against: {exc}"
        )

    assert "ERROR" not in summary
    assert out_path.exists(), "run() did not actually produce a workbook"
    wb = load_workbook(out_path)
    assert "Missing months" in wb.sheetnames
    assert "Accounts checked" in wb.sheetnames
