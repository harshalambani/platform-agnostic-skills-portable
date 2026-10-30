"""
IMP-17: the KRC contract-note journal checks the book before writing.
Same Num + same amounts -> left out and listed; same Num, other amounts -> RED
FLAG and left out; no Num match but same date/amounts/accounts -> kept and
flagged as possible; unreadable book -> NOT CHECKED. Synthetic data only.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_krc_gnucash_flags_charges as base  # noqa: E402

bk = base.bk
BROKER = base.BROKER
STK = "Assets:Investments:Stocks:ALPHA STEEL"


def _split(acct, val, qty=None):
    v = f"{int(round(val * 100))}/100"
    q = f"{int(round(qty * 100))}/100" if qty is not None else v
    return (f"<trn:split><split:account type=\"guid\">{acct}</split:account>"
            f"<split:value>{v}</split:value><split:quantity>{q}</split:quantity></trn:split>")


def _txn(num, day, splits):
    return (f"<gnc:transaction version=\"2.0.0\"><trn:num>{num}</trn:num>"
            f"<trn:date-posted><ts:date>{day} 00:00:00 +0000</ts:date></trn:date-posted>"
            f"<trn:splits>{''.join(splits)}</trn:splits></gnc:transaction>")


def _book(path: Path, txns=()):
    accts = [
        base._acct("Root Account", "root", "ROOT", None),
        base._acct("Assets", "as", "ASSET", "root", [("placeholder", "true")]),
        base._acct("Broker", "br", "BANK", "as"),
        base._acct("Investments", "inv", "ASSET", "as", [("placeholder", "true")]),
        base._acct("Stocks", "stk", "ASSET", "inv", [("placeholder", "true")]),
        base._acct("ALPHA STEEL", "s1", "STOCK", "stk"),
        base._acct("Expenses", "ex", "EXPENSE", "root", [("placeholder", "true")]),
        base._acct("Demat Charges", "ex1", "EXPENSE", "ex"),
    ]
    path.write_text(f'<?xml version="1.0"?><gnc-v2 {base._NS} '
                    'xmlns:trn="http://www.gnucash.org/XML/trn" '
                    'xmlns:split="http://www.gnucash.org/XML/split" '
                    'xmlns:ts="http://www.gnucash.org/XML/ts"><gnc:book version="2.0.0">'
                    + "".join(accts) + "".join(txns) + "</gnc:book></gnc-v2>", encoding="utf-8")
    return path


def _buy_txn(num, day, qty, net):
    return _txn(num, day, [_split("s1", net, qty), _split("br", -net)])


def _run(tmp_path, bills, ledger, txns, extra=(), outname="out"):
    book = _book(tmp_path / "b.gnucash", txns)
    x = base._bills_xlsx(tmp_path / "bills.xlsx", bills, ledger)
    out = tmp_path / outname
    r = subprocess.run([sys.executable, str(base.SCRIPT), str(x), str(book), str(out),
                        str(base._cfg(tmp_path)), *extra], capture_output=True, text=True)
    return r, out


def _ids(out, name):
    p = out / name
    if not p.is_file():
        return set()
    with open(p, newline="", encoding="utf-8") as f:
        return {r["Transaction ID"] for r in csv.DictReader(f)}


def _review(out):
    p = out / "Review.csv"
    if not p.is_file():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_note_already_in_book_is_left_out_and_listed(tmp_path):
    r, out = _run(tmp_path, [base._buy("C1", "ALPHA STEEL", 10, 600.0)], [],
                  [_buy_txn("KRC-PC1", "2025-06-01", 10, 600.0)])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _ids(out, "Purchase.csv") == set()
    assert not (out / "Purchase.csv").exists()
    assert "Already in the book (left out): 1" in r.stdout and "KRC-PC1" in r.stdout
    assert any(x["Reason"].startswith("Already in the book (left out)") for x in _review(out))
    info = json.loads((out / "run_info.json").read_text())
    assert [i["id"] for i in info["already_in_book"]] == ["KRC-PC1"]


def test_different_note_same_amount_other_date_is_still_offered(tmp_path):
    r, out = _run(tmp_path, [base._buy("C2", "ALPHA STEEL", 10, 600.0)], [],
                  [_buy_txn("KRC-PC1", "2025-05-01", 10, 600.0)])
    assert _ids(out, "Purchase.csv") == {"KRC-PC2"}
    assert "Already in the book" not in r.stdout and "Possibly" not in r.stdout


def test_renumbered_hand_copy_is_kept_and_flagged_possible(tmp_path):
    r, out = _run(tmp_path, [base._buy("C1", "ALPHA STEEL", 10, 600.0)], [],
                  [_buy_txn("MY-1", "2025-06-01", 10, 600.0)])
    assert _ids(out, "Purchase.csv") == {"KRC-PC1"}          # not dropped
    assert "Possibly already in the book - check before importing" in r.stdout
    assert r.returncode == 2
    assert any(x["Reason"].startswith("Possibly already in the book") for x in _review(out))
    assert json.loads((out / "run_info.json").read_text())["possibly_in_book"]


def test_same_num_other_amount_is_red_flag_and_left_out(tmp_path):
    r, out = _run(tmp_path, [base._buy("C1", "ALPHA STEEL", 10, 600.0)], [],
                  [_buy_txn("KRC-PC1", "2025-06-01", 10, 650.0)])
    assert r.stdout.splitlines()[0].startswith("RED FLAG - IN BOOK, DIFFERENT AMOUNTS")
    assert _ids(out, "Purchase.csv") == set()
    assert r.returncode == 2
    assert json.loads((out / "run_info.json").read_text())["red_flags"][0].startswith(
        "RED FLAG - IN BOOK, DIFFERENT AMOUNTS")


def test_unreadable_book_check_says_not_checked(tmp_path, monkeypatch, capsys):
    book = _book(tmp_path / "b.gnucash", [_buy_txn("KRC-PC1", "2025-06-01", 10, 600.0)])
    x = base._bills_xlsx(tmp_path / "bills.xlsx", [base._buy("C1", "ALPHA STEEL", 10, 600.0)], [])

    def boom(*a, **k):
        raise ValueError("synthetic unreadable")
    monkeypatch.setattr(bk, "load_book_txns", boom)
    monkeypatch.setattr(sys, "argv", ["x", str(x), str(book), str(tmp_path / "o"),
                                      str(base._cfg(tmp_path))])
    rc = bk.main()
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("RED FLAG - NOT CHECKED")
    assert "synthetic unreadable" in out and rc == 2


def test_malformed_book_txn_is_not_checked_not_silent(tmp_path):
    bad = _txn("KRC-PC1", "2025-06-01", [_split("zz", 5.0)])      # unknown account
    r, out = _run(tmp_path, [base._buy("C1", "ALPHA STEEL", 10, 600.0)], [], [bad])
    assert "NOT CHECKED" in r.stdout.splitlines()[0]


def test_one_book_txn_accounts_for_one_bill_only(tmp_path):
    bills = [base._buy("C1", "ALPHA STEEL", 10, 600.0), base._buy("C2", "ALPHA STEEL", 10, 600.0)]
    r, out = _run(tmp_path, bills, [], [_buy_txn("MY-1", "2025-06-01", 10, 600.0)])
    assert r.stdout.count("FLAG - Possibly already in the book") == 1
    assert _ids(out, "Purchase.csv") == {"KRC-PC1", "KRC-PC2"}


def test_num_match_is_not_stolen_by_an_earlier_lookalike(tmp_path):
    bills = [base._buy("C1", "ALPHA STEEL", 10, 600.0), base._buy("C2", "ALPHA STEEL", 10, 600.0)]
    r, out = _run(tmp_path, bills, [], [_buy_txn("KRC-PC2", "2025-06-01", 10, 600.0)])
    assert _ids(out, "Purchase.csv") == {"KRC-PC1"}
    assert "Possibly" not in r.stdout


def test_sale_and_charge_already_in_book_are_left_out(tmp_path):
    lot = _buy_txn("H1", "2024-01-01", 20, 200.0)
    sale = _txn("KRC-SC9", "2025-07-01", [_split("br", 300.0), _split("s1", -100.0, -10)])
    chg = _txn("KRC-D20250603-5000", "2025-06-03", [_split("ex1", 50.0), _split("br", -50.0)])
    ledger = [("2025-06-03", "DP charges synthetic", 50.0, None, 0.0, "Cr", "Demat Charge")]
    r, out = _run(tmp_path, [base._sell("C9", "ALPHA STEEL", 10, 300.0)], ledger,
                  [lot, sale, chg], extra=("--demat-account", "Expenses:Demat Charges"))
    assert not (out / "Sale.csv").exists() and not (out / "Charges.csv").exists()
    assert "insufficient" not in r.stdout          # not re-sold from the lots
    assert "KRC-D20250603-5000" in r.stdout and "KRC-SC9" in r.stdout


def test_charge_same_num_other_amount_is_red_flag(tmp_path):
    chg = _txn("KRC-D20250603-5000", "2025-06-03", [_split("ex1", 40.0), _split("br", -40.0)])
    ledger = [("2025-06-03", "DP charges synthetic", 50.0, None, 0.0, "Cr", "Demat Charge")]
    r, out = _run(tmp_path, [], ledger, [chg], extra=("--demat-account", "Expenses:Demat Charges"))
    assert r.stdout.splitlines()[0].startswith("RED FLAG - IN BOOK, DIFFERENT AMOUNTS")
    assert not (out / "Charges.csv").exists()


# ---------------- FIFO trap ----------------
def _build(tmp_path, bills, txns, name="f.gnucash"):
    book = _book(tmp_path / name, txns)
    acc, paths, stock_guids, holdings = bk.load_book(str(book))
    bk._stock_paths = [paths[g] for g in stock_guids]
    idx = bk.BookIndex(bk.load_book_txns(str(book), paths), {bk.bill_id(b) for b in bills})
    cfg = {"accounts": {"broker": BROKER, "slbs_income": "Income:SLBS",
                        "ltcg": "Income:LTCG", "stcg": "Income:STCG"},
           "long_term_threshold_months": 12}
    notes = {}
    res = bk.build_entries(bills, cfg, paths, holdings, notes, idx)
    return res, holdings, notes


def _bill(cn, day, qty, net, direction):
    return {"cn_no": cn, "type": "TRADE", "date": day, "settlement": "S",
            "security": "ALPHA STEEL", "quantity": qty, "net": net, "direction": direction}


def _stock_cost(sale):
    return [a for a in sale["splits"] if a[0] == STK][0][1]


def test_fifo_cost_of_new_sale_same_with_or_without_rerun_purchase(tmp_path):
    txns = [_buy_txn("H1", "2024-01-01", 10, 100.0),                # 10 @ 10
            _buy_txn("KRC-PC1", "2025-01-01", 10, 200.0)]           # 10 @ 20 (booked by KRC)
    sale = _bill("C5", "01/08/2025", 15, 450.0, "Receivable")
    buy = _bill("C1", "01/01/2025", 10, 200.0, "Payable")
    (_, _, sales_a, _), h_a, _n = _build(tmp_path, [sale], txns, "a.gnucash")
    (_, _, sales_b, _), h_b, nb = _build(tmp_path, [buy, sale], txns, "b.gnucash")
    assert nb["in_book"] and nb["in_book"][0]["id"] == "KRC-PC1"
    assert _stock_cost(sales_a[0]) == _stock_cost(sales_b[0]) == -200.0   # 10*10 + 5*20
    assert h_a == h_b


def test_rerun_purchase_does_not_double_count_lots(tmp_path):
    txns = [_buy_txn("KRC-PC1", "2025-01-01", 10, 200.0)]
    buy = _bill("C1", "01/01/2025", 10, 200.0, "Payable")
    _, h, _n = _build(tmp_path, [buy], txns)
    assert sum(lot[1] for lot in h[STK]) == Fraction(10)


def test_sale_larger_than_real_holdings_still_insufficient_after_rerun(tmp_path):
    txns = [_buy_txn("KRC-PC1", "2025-01-01", 10, 200.0)]
    buy = _bill("C1", "01/01/2025", 10, 200.0, "Payable")
    sale = _bill("C5", "01/08/2025", 15, 450.0, "Receivable")
    (_, _, sales, review), _h, _n = _build(tmp_path, [buy, sale], txns)
    assert not sales and "insufficient FIFO lots" in review[0][1]


def test_booked_sale_not_sold_again_from_remaining_lots(tmp_path):
    txns = [_buy_txn("H1", "2024-01-01", 10, 100.0), _buy_txn("H2", "2025-01-01", 10, 200.0),
            _txn("KRC-SC5", "2025-08-01", [_split("br", 150.0), _split("s1", -100.0, -10)])]
    sale = _bill("C5", "01/08/2025", 10, 150.0, "Receivable")
    (_, _, sales, review), h, nt = _build(tmp_path, [sale], txns)
    assert not sales and not review and nt["in_book"]
    assert sum(lot[1] for lot in h[STK]) == Fraction(10)
    assert h[STK][0][2] == Fraction(20)
