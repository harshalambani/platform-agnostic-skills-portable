"""ITR-09: a GnuCash journal CSV for the broker report's foreign dividends.
Synthetic data only; invented companies, round amounts."""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import configs                                         # noqa: E402
import foreign_dividend_journal as fdj                 # noqa: E402
import parse_foreign as pf                             # noqa: E402
from agents.gnucash_accounts import TargetGuard        # noqa: E402
from tests.gnc_book_fixture import account_xml, txn_xml, write_book   # noqa: E402

CASH, TAX, INC = "Assets:Broker Cash", "Assets:Foreign Tax Withheld", "Income:Foreign Dividends"
ACCTS = {"foreign_dividend_cash_account": CASH, "foreign_dividend_tax_account": TAX,
         "foreign_dividend_income_account": INC}


def pay(company, when, gross, tax, broker="SynBroker"):
    return pf.DividendPayment(company=company, date_received=when, fx_rate=85.0, amount_usd=None,
                              amount_inr=gross, tax_usd=None, tax_inr=tax, broker=broker)


PAYMENTS = [
    pay("SynCo Beta Inc", "2025-06-26", 2200.0, 380.0),
    pay("SynCo Alpha Ltd", "2025-07-10", 700.0, 0.0),
    pay("SynCo Alpha Ltd", "2025-10-10", 650.0, 0.0),
]


def test_one_balanced_entry_per_payment_at_the_report_figures():
    r = fdj.build(PAYMENTS, ACCTS, fy_label="2025-26")
    assert not r.stopped and not r.problems and len(r.entries) == 3
    e = r.entries[0]
    assert dict(e.splits) == {CASH: 182000, TAX: 38000, INC: -220000}
    for e in r.entries:
        assert sum(c for _a, c in e.splits) == 0                       # balanced
        assert e.net + e.tax == e.gross                                # net + tax == gross
    rows = r.rows
    assert round(sum(float(x["Amount"]) for x in rows), 2) == 0.0
    assert {x["Transaction ID"] for x in rows} == {"2025-FDIV01", "2025-FDIV02", "2025-FDIV03"}


def test_entry_totals_tie_to_the_report_dividend_and_tax_totals():
    r = fdj.build(PAYMENTS, ACCTS, fy_label="2025-26", report_total=3550.0)
    assert r.gross_total == sum(p.amount_inr for p in PAYMENTS) == 3550.0
    assert r.tax_total == sum(p.tax_inr for p in PAYMENTS) == 380.0
    assert not r.notes
    off = fdj.build(PAYMENTS, ACCTS, fy_label="2025-26", report_total=9999.0)
    assert any("add up" in n for n in off.notes)                       # a mismatch is said, not hidden


def test_zero_tax_payment_has_no_tax_split():
    r = fdj.build(PAYMENTS[1:2], ACCTS)
    assert [a for a, _c in r.entries[0].splits] == [CASH, INC]


def test_a_look_alike_payment_is_kept_and_flagged_never_withheld():                # NEGATIVE
    booked = [fdj.BookTxnLite(date(2025, 6, 26), "synthetic booked dividend", frozenset({220000, 182000, 38000}))]
    r = fdj.build(PAYMENTS, ACCTS, booked)
    assert len(r.entries) == 3 and len(r.possibly_booked) == 1           # IMP-15 4b: written AND flagged
    assert any("Beta" in e.description for e in r.entries)               # NOT withheld
    text = chr(10).join(r.summary_lines())
    assert "check before import" in text and "SynCo Beta Inc" in text
    assert "0 left out" in text                                          # nothing was left out
    # matching is by date AND amount: the same amount on another day is not a look-alike
    other_day = [fdj.BookTxnLite(date(2025, 6, 27), "x", frozenset({220000}))]
    r2 = fdj.build(PAYMENTS, ACCTS, other_day)
    assert len(r2.entries) == 3 and r2.possibly_booked == []
    wrong_amt = [fdj.BookTxnLite(date(2025, 6, 26), "x", frozenset({123456}))]
    r3 = fdj.build(PAYMENTS, ACCTS, wrong_amt)
    assert len(r3.entries) == 3 and r3.possibly_booked == []


def test_one_book_entry_flags_only_one_payment():                                   # NEGATIVE
    twins = [pay("SynCo A", "2025-08-01", 500.0, 0.0), pay("SynCo B", "2025-08-01", 500.0, 0.0)]
    booked = [fdj.BookTxnLite(date(2025, 8, 1), "one booked", frozenset({50000}))]
    r = fdj.build(twins, ACCTS, booked)
    assert len(r.entries) == 2 and len(r.possibly_booked) == 1           # both written, one flagged


def test_several_equally_good_book_entries_are_a_tie_none_is_picked():             # NEGATIVE
    one = [pay("SynCo A", "2025-08-01", 500.0, 0.0)]
    booked = [fdj.BookTxnLite(date(2025, 8, 1), "first", frozenset({50000})),
              fdj.BookTxnLite(date(2025, 8, 1), "second", frozenset({50000}))]
    r = fdj.build(one, ACCTS, booked)
    assert len(r.entries) == 1 and len(r.possibly_booked) == 1
    assert r.possibly_booked[0][2] == ""                                  # no description picked
    assert "none was picked" in chr(10).join(r.summary_lines())


def test_a_run_with_no_accounts_set_emits_nothing_and_says_why():                 # NEGATIVE
    r = fdj.build(PAYMENTS, {})
    assert r.stopped and not r.entries and not r.rows
    assert "Foreign dividend cash account" in r.stopped and "Nothing was written" in r.stopped
    partial = dict(ACCTS, foreign_dividend_tax_account="")
    r2 = fdj.build(PAYMENTS, partial)
    assert r2.stopped and not r2.rows and "tax account" in r2.stopped
    assert "NOT written" in r2.summary_lines()[0]


def test_two_roles_on_one_account_stop_the_run():                                   # NEGATIVE
    r = fdj.build(PAYMENTS, dict(ACCTS, foreign_dividend_tax_account=CASH))
    assert r.stopped and not r.rows


def _guard(tmp_path):
    accs = [
        account_xml("root", "Root Account", "ROOT", None),
        account_xml("as", "Assets", "ASSET", "root", ["placeholder"]),
        account_xml("cash", "Broker Cash", "BANK", "as"),
        account_xml("taxa", "Foreign Tax Withheld", "ASSET", "as"),
        account_xml("hid", "Old Tax", "ASSET", "as", ["hidden"]),
        account_xml("inc", "Income", "INCOME", "root", ["placeholder"]),
        account_xml("div", "Foreign Dividends", "INCOME", "inc"),
        account_xml("eq", "Equity", "EQUITY", "root"),
    ]
    txns = [txn_xml("synthetic dividend", "2025-06-26", [("cash", 182000), ("taxa", 38000), ("div", -220000)])
            .replace("<trn:description>", '<trn:id type="guid">t1</trn:id><trn:description>', 1)]
    parts = txns[0].split("<trn:split>")
    txns = [parts[0] + "".join(f'<trn:split><split:id type="guid">s{i}</split:id>{p}'
                               for i, p in enumerate(parts[1:]))]
    book = write_book(tmp_path / "Syn.gnucash", accs, txns)
    return TargetGuard.from_book(book), book


def test_the_journal_never_targets_a_hidden_or_placeholder_account(tmp_path):         # NEGATIVE
    guard, _book = _guard(tmp_path)
    ok = fdj.build(PAYMENTS, ACCTS, guard=guard)
    assert not ok.stopped                                              # the real accounts pass the guard
    for bad in ("Assets:Old Tax", "Assets", "Income"):
        for key in ACCTS:
            r = fdj.build(PAYMENTS, dict(ACCTS, **{key: bad}), guard=guard)
            assert r.stopped and not r.rows, (key, bad)
    unknown = fdj.build(PAYMENTS, dict(ACCTS, foreign_dividend_cash_account="Assets:Nowhere"), guard=guard)
    assert unknown.stopped and "not an account" in unknown.stopped


def test_dedup_reads_the_real_book(tmp_path):
    import parse_gnucash as pg
    _g, book_file = _guard(tmp_path)
    txns = fdj.book_txns_from_book(pg.parse_book(book_file))
    r = fdj.build(PAYMENTS, ACCTS, txns)
    assert len(r.possibly_booked) == 1 and len(r.entries) == 3


def test_unreadable_or_invalid_payments_are_left_out_and_listed_never_zeroed():           # NEGATIVE
    bad = [pay("SynCo X", "not a date", 100.0, 0.0), pay("SynCo Y", "2025-06-01", None, 0.0),
           pay("SynCo Z", "2025-06-02", 100.0, None), pay("SynCo W", "2025-06-03", 100.0, 150.0)]
    r = fdj.build(bad, ACCTS)
    assert not r.entries and len(r.problems) == 4
    assert "never read as 0" in r.problems[2]


def test_csv_is_written_in_the_multi_split_layout(tmp_path):
    r = fdj.build(PAYMENTS, ACCTS, fy_label="2025-26")
    out = tmp_path / "j.csv"
    fdj.write_csv(r.rows, out)
    got = list(csv.DictReader(out.open(encoding="utf-8")))
    assert list(got[0]) == fdj.HEADERS and len(got) == 7               # 3 + 2 + 2 splits
    assert got[0]["Transaction ID"] == got[1]["Transaction ID"] == got[2]["Transaction ID"]
    assert all(x["Currency"] == "INR" for x in got)


def test_the_workbook_hook_writes_the_file_and_stops_loudly_without_accounts(tmp_path):
    div = pf.DividendData(payments=list(PAYMENTS), total=3550.0, financial_year="2025-26")
    report = type("R", (), {"dividend": div})()
    ent = configs.EntityProfile(key="SYN-P", name="Syn", pan="AAAAA0000A", status="Individual",
                                **ACCTS)
    out = str(tmp_path / "wb.xlsx")
    lines = fdj.run_for_workbook(ent, report, None, None, out)
    assert "NOT CHECKED" in lines[0] and lines[0].startswith("RED FLAG")   # no book given: loud
    assert any("3 entry" in x for x in lines) and (tmp_path / "wb.xlsx-SYN-P-foreign-dividends-journal.csv").is_file()
    bare = configs.EntityProfile(key="SYN-Q", name="Syn", pan="AAAAA0000A", status="Individual")
    lines2 = fdj.run_for_workbook(bare, report, None, None, str(tmp_path / "wb2.xlsx"))
    assert "NOT written" in lines2[0]
    assert not list(tmp_path.glob("wb2*"))                              # nothing emitted
    assert fdj.run_for_workbook(ent, type("R", (), {"dividend": pf.DividendData()})(), None, None, out) == []


def test_an_unreadable_book_writes_every_payment_and_says_not_checked(tmp_path):   # NEGATIVE
    class Broken:
        @property
        def transactions(self):
            raise OSError("cannot read")
    div = pf.DividendData(payments=list(PAYMENTS), total=3550.0, financial_year="2025-26")
    report = type("R", (), {"dividend": div})()
    ent = configs.EntityProfile(key="SYN-U", name="Syn", pan="AAAAA0000A", status="Individual", **ACCTS)
    lines = fdj.run_for_workbook(ent, report, Broken(), None, str(tmp_path / "wb.xlsx"))
    assert lines[0].startswith("RED FLAG") and "NOT CHECKED" in lines[0]
    assert any("3 entry" in x for x in lines)                              # all three written
    assert (tmp_path / "wb.xlsx-SYN-U-foreign-dividends-journal.csv").is_file()


def test_entity_keys_survive_a_save_and_a_reload(tmp_path):
    from ui.tabs import itr_entities as ui_mod
    root = tmp_path / "Data"
    p = root / "itr" / "entities.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump({"SYN-P": {"name": "Syn", "pan": "AAAAA0000A", "status": "Individual",
                                           "residency": "Resident", "default_regime": "new", **ACCTS}}),
                 encoding="utf-8")
    with patch("ui._config.data_root_dir", return_value=root):
        e = ui_mod._load_entities()["SYN-P"]
        # an older caller that does not know the fields keeps them
        ui_mod._save_entity("SYN-P", "SYN-P", e.name, e.pan, e.status, e.residency, "", "", "", "", "", "", "",
                            e.default_regime, "", False, "", "", "")
        again = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-P"]
        assert all(again[k] == v for k, v in ACCTS.items())
        # a value replaces, a blank clears
        ui_mod._save_entity("SYN-P", "SYN-P", e.name, e.pan, e.status, e.residency, "", "", "", "", "", "", "",
                            e.default_regime, "", False, "", "", "",
                            foreign_dividend_cash_account="Assets:Other Cash", foreign_dividend_tax_account="")
    after = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-P"]
    assert after["foreign_dividend_cash_account"] == "Assets:Other Cash"
    assert "foreign_dividend_tax_account" not in after
    assert after["foreign_dividend_income_account"] == INC
    vals = ui_mod._entity_to_new_form("SYN-P", {"SYN-P": configs.load_entities(p)["SYN-P"]})
    assert vals[-5:-2] == ("Assets:Other Cash", "", INC)
