"""IMP-18: the partner journal CSVs no longer offer journals the book already
holds. Synthetic fixtures only (made-up entity, accounts, amounts)."""
import csv
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import tests.test_skill_partner_comp_recon as T  # noqa: E402
from agents.skill_partner_comp_recon import agent as A  # noqa: E402
from agents.skill_partner_comp_recon.gnucash_tieout import (  # noqa: E402
    ALREADY_POSTED, NOT_POSTED, build_posted_check, partition_by_posted)
from agents.skill_partner_comp_recon.jv_emitter import build_journals  # noqa: E402

FY = "2025-26"
ACCTS = {
    "bank": "Assets:Bank:Current Account",
    "tds_expense": "Expenses:Tax:TDS",
    "remuneration_income": "Income:PGBP:Remuneration",
    "share_of_profit_income": "Income:PGBP:Share of Profit",
    "current_account": "Equity:Partner Current Account",
}
MONTH_DATE = {"2025-04": "2025-04-30", "2025-05": "2025-05-31"}


def _tree():
    accounts, guids = T._gc_tree_with_equity()
    guids["Other Clearing"] = T._gc_guid("Other Clearing")
    accounts.append(T._gc_account_xml(
        guids["Other Clearing"], "Other Clearing", "INCOME", guids["PGBP"]))
    return accounts, guids


def _deposits(guids, months):
    return [T._bank_deposit_txn(f"dep-{m}", MONTH_DATE[m], 480000.0,
                                guids["Current Account"], guids["Other Clearing"])
            for m in months]


def _write_book(tmp_path, name, txns):
    accounts, _ = _tree()
    return T._write_gnucash_book(tmp_path / f"{name}.gnucash", T._gc_document_xml(accounts, txns))


def _txn_from_csv(guids, rows, seed, num=None, nudge=0.0, date=None):
    """A book transaction that mirrors one journal's CSV rows."""
    splits = []
    for i, r in enumerate(rows):
        amt = float(r["Amount"])
        if i == 0:
            amt += nudge
        if i == len(rows) - 1:
            amt -= nudge
        leaf = r["Account"].split(":")[-1]
        splits.append(T._gc_split_xml(T._gc_guid(f"{seed}-{i}"), amt, guids[leaf]))
    return T._gc_txn_xml(T._gc_guid(seed), date or rows[0]["Date"], "posted", splits,
                         num=rows[0]["Transaction ID"] if num is None else num)


def _read(path):
    with Path(path).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _by_id(rows):
    out = {}
    for r in rows:
        out.setdefault(r["Transaction ID"], []).append(r)
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    d = tmp_path / "advices"
    d.mkdir()
    for n in ("a.pdf", "b.pdf"):
        (d / n).write_bytes(b"%PDF-1.4 fake")
    adv = tmp_path / "adv.pdf"
    adv.write_bytes(b"%PDF-1.4 fake")
    ents = tmp_path / "entities.yaml"
    ents.write_text(yaml.safe_dump({"SYN-LLP": {
        "name": "Synthetic Partner One", "pan": "AAAAA0000A", "status": "Individual",
        "partner_comp_accounts": dict(ACCTS)}}), encoding="utf-8")
    state = {"months": ["2025-04"], "llp": None}

    def parse(path, pw):
        month = "2025-04" if str(path).endswith("a.pdf") else "2025-05"
        return {"month": month, "source_name": Path(path).name, "total_paid": 480000.0,
                "remuneration": 200000.0, "share_of_profit_gross": 300000.0,
                "additional_share_of_profit": 0.0, "tds": -20000.0}

    monkeypatch.setattr(A._advisory_parser, "parse", lambda p, pw: {"financial_year": FY})
    monkeypatch.setattr(A._payout_advice_parser, "parse", parse)
    monkeypatch.setattr(A, "_resolve_llp_leg",
                        lambda p, pw: ("L5 stub", state["llp"]) if state["llp"] else ("L5 none", None))
    def go(book, with_accrual=False, tag="x"):
        if len(state["months"]) == 1:
            for f in d.glob("b.pdf"):
                f.rename(d / "b.pdf.off")
        else:
            for f in d.glob("b.pdf.off"):
                f.rename(d / "b.pdf")
        jr = tmp_path / f"j_{tag}.csv"
        ar = tmp_path / f"a_{tag}.csv"
        res = A.run(entity="SYN-LLP", advices_dir=str(d), advisory_path=str(adv),
                    output_path=str(tmp_path / f"o_{tag}.xlsx"), financial_year=FY,
                    entities_path=str(ents), gnucash_path=book, journal_path=str(jr),
                    accrual_journal_path=str(ar) if with_accrual else "",
                    llp_statement="stub.pdf" if with_accrual else "")
        return res, jr, ar, state

    go.state = state
    go.tmp = tmp_path
    return go


def _text(res):
    return str(res)


def _extra(res, key):
    return next(o for o in res.extra_outputs if o["key"] == key)


def _first_run(env):
    """Run against a book holding only the bank deposit(s); returns the
    journal rows by id plus the tree guids."""
    _, guids = _tree()
    env.state["months"] = ["2025-04", "2025-05"]
    book = _write_book(env.tmp, "first", _deposits(guids, ["2025-04", "2025-05"]))
    res, jr, ar, _ = env(book, tag="first")
    assert jr.exists(), _text(res)
    return _by_id(_read(jr)), guids


# ---------------------------------------------------------------- monthly --

def test_posted_month_is_left_out_and_unposted_month_is_offered(env):
    rows, guids = _first_run(env)
    ids = sorted(rows)
    assert len(ids) == 2
    posted, unposted = ids[0], ids[1]
    book = _write_book(env.tmp, "second",
                       _deposits(guids, ["2025-04", "2025-05"])
                       + [_txn_from_csv(guids, rows[posted], "posted-1")])
    res, jr, _, _ = env(book, tag="second")
    got = _by_id(_read(jr))
    assert set(got) == {unposted}                       # posted month left out
    assert posted not in got                            # NEGATIVE
    text = _text(res)
    assert "Already in the book (left out): 1 journal(s)" in text and posted in text
    assert "RED FLAG" not in text
    assert "do not import those" not in text            # the old advice is gone


def test_all_posted_writes_no_csv_and_says_why(env):
    env.state["months"] = ["2025-04"]
    _, guids = _tree()
    book = _write_book(env.tmp, "one", _deposits(guids, ["2025-04"]))
    res, jr, _, _ = env(book, tag="p1")
    rows = _by_id(_read(jr))
    (jid,) = rows
    book2 = _write_book(env.tmp, "one2", _deposits(guids, ["2025-04"])
                        + [_txn_from_csv(guids, rows[jid], "posted-1")])
    res2, jr2, _, _ = env(book2, tag="p2")
    assert not jr2.exists()                             # NEGATIVE: no empty importable CSV
    text = _text(res2)
    assert "Journal CSV: not written" in text and "Already in the book (left out)" in text
    out = _extra(res2, "journal_csv")
    assert out["path"] is None and "left out" in out["note"] and "No journal written" in out["note"]
    assert "RED FLAG" not in text


def test_same_num_different_amount_is_left_out_with_a_red_flag_first(env):
    env.state["months"] = ["2025-04"]
    _, guids = _tree()
    book = _write_book(env.tmp, "m1", _deposits(guids, ["2025-04"]))
    res, jr, _, _ = env(book, tag="m1")
    rows = _by_id(_read(jr))
    (jid,) = rows
    book2 = _write_book(env.tmp, "m2", _deposits(guids, ["2025-04"])
                        + [_txn_from_csv(guids, rows[jid], "posted-1", nudge=125.0)])
    res2, jr2, _, _ = env(book2, tag="m2")
    assert not jr2.exists()                             # left OUT
    first = _text(res2).splitlines()[0]
    assert first.startswith("RED FLAG") and jid in first and "DIFFERENT" in first
    assert "disagree" in first
    assert "Already in the book (left out)" not in _text(res2)  # not called a clean match


def test_fallback_match_stays_in_and_is_flagged_possible(env):
    env.state["months"] = ["2025-04"]
    _, guids = _tree()
    book = _write_book(env.tmp, "f1", _deposits(guids, ["2025-04"]))
    res, jr, _, _ = env(book, tag="f1")
    rows = _by_id(_read(jr))
    (jid,) = rows
    book2 = _write_book(env.tmp, "f2", _deposits(guids, ["2025-04"])
                        + [_txn_from_csv(guids, rows[jid], "posted-1", num="OTHER-NUM")])
    res2, jr2, _, _ = env(book2, tag="f2")
    assert set(_by_id(_read(jr2))) == {jid}             # stays IN
    text = _text(res2)
    assert "Possible already in the book - check before import" in text and jid in text
    assert "Already in the book (left out)" not in text  # NEGATIVE: not claimed definite
    assert "RED FLAG" not in text


def test_unposted_everything_is_offered_without_flags(env):
    env.state["months"] = ["2025-04"]
    _, guids = _tree()
    res, jr, _, _ = env(_write_book(env.tmp, "n", _deposits(guids, ["2025-04"])), tag="n")
    assert len(_by_id(_read(jr))) == 1
    t = _text(res)
    assert "RED FLAG" not in t and "left out" not in t and "Possible already" not in t


def test_book_unreadable_writes_the_file_with_not_checked_red_flag(env):
    env.state["months"] = ["2025-04"]
    _, guids = _tree()
    good = _write_book(env.tmp, "g", _deposits(guids, ["2025-04"]))
    env(good, tag="g")
    # the checks cannot run when the book is unreadable; force that for the
    # posted check only so the rest of the run is unaffected
    import agents.skill_partner_comp_recon.agent as ag
    orig = ag.build_posted_check
    ag.build_posted_check = lambda *a, **k: ([], "Posted-already check: not available (book unreadable).")
    try:
        res, jr, _, _ = env(good, tag="u")
    finally:
        ag.build_posted_check = orig
    assert jr.exists()                                  # file still written
    assert _text(res).splitlines()[0].startswith("RED FLAG: NOT CHECKED against the book")
    assert "not available" in _text(res).splitlines()[0]


def test_structured_input_path_says_it_did_not_check_the_book(tmp_path):
    data = {
        "financial_year": FY, "firm_name": "Synthetic Test LLP",
        "accounts": ACCTS,
        "monthly": [{"month": "2025-04", "total_paid": 480000.0, "remuneration": 200000.0,
                     "share_of_profit_gross": 300000.0, "tds": -20000.0}],
    }
    p = tmp_path / "in.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    jr = tmp_path / "j.csv"
    res = A.run(input_path=str(p), output_path=str(tmp_path / "o.xlsx"), journal_path=str(jr))
    text = _text(res)
    assert "RED FLAG: NOT CHECKED against the book" in text
    assert "ERROR" not in text or jr.exists()


# ------------------------------------------------- posted check, unit level --

def _check_setup(tmp_path, extra=None, nudge=0.0, num_ok=True, seeds=("a",)):
    report = T._gc_tieout_report()
    accounts = T._GC_TIEOUT_ACCOUNTS
    journals = build_journals(report, accounts)
    assert len(journals) == 1
    j = journals[0]
    tree, guids = T._gc_tree()
    sp = T._gc_matching_splits(guids)
    txns = []
    for s in seeds:
        sp_s = [x.replace(T._gc_guid("s-bank"), T._gc_guid(f"s-bank-{s}"))
                 .replace(T._gc_guid("s-tds"), T._gc_guid(f"s-tds-{s}"))
                 .replace(T._gc_guid("s-rem"), T._gc_guid(f"s-rem-{s}"))
                 .replace(T._gc_guid("s-sop"), T._gc_guid(f"s-sop-{s}")) for x in sp]
        txns.append(T._gc_txn_xml(T._gc_guid(f"t-{s}"), j.date, "posted", sp_s,
                                  num=j.txn_id if (num_ok and s == seeds[0]) else ""))
    book = T._write_gnucash_book(tmp_path / "u.gnucash", T._gc_document_xml(tree, txns))
    return report, accounts, journals, book


def test_definite_vs_fallback_kinds(tmp_path):
    report, accounts, journals, book = _check_setup(tmp_path)
    res, _ = build_posted_check(report, accounts, book, FY)
    assert res[0].status == ALREADY_POSTED and res[0].kind == "definite"
    part = partition_by_posted(journals, res)
    assert part["left_out"] == journals and part["write"] == []

    report, accounts, journals, book = _check_setup(tmp_path, num_ok=False)
    res, _ = build_posted_check(report, accounts, book, FY)
    assert res[0].kind == "possible"                    # NEGATIVE: never "definite" without a Num
    part = partition_by_posted(journals, res)
    assert part["write"] == journals and part["possible"] == journals and part["left_out"] == []


def test_one_book_transaction_cannot_clear_two_journals(tmp_path):
    report = T._gc_tieout_report()
    accounts = T._GC_TIEOUT_ACCOUNTS
    (j,) = build_journals(report, accounts)
    twin = type(j)(txn_id=j.txn_id + "-TWIN", date=j.date, description="twin", splits=j.splits)
    tree, guids = T._gc_tree()
    txn = T._gc_txn_xml(T._gc_guid("only"), j.date, "posted", T._gc_matching_splits(guids))
    book = T._write_gnucash_book(tmp_path / "o.gnucash", T._gc_document_xml(tree, [txn]))
    import agents.skill_partner_comp_recon.gnucash_tieout as G
    orig = G._journals_safely
    G._journals_safely = lambda *a, **k: ([j, twin], None)
    try:
        res, _ = build_posted_check(report, accounts, book, FY)
    finally:
        G._journals_safely = orig
    kinds = sorted((r.status, r.kind) for r in res)
    assert (ALREADY_POSTED, "possible") in kinds
    assert (NOT_POSTED, "") in kinds                     # the second journal is NOT cleared


def test_two_equally_good_book_transactions_are_a_tie_not_auto_picked(tmp_path):
    report, accounts, journals, book = _check_setup(tmp_path, num_ok=False, seeds=("a", "b"))
    res, _ = build_posted_check(report, accounts, book, FY)
    assert res[0].kind == "possible_tie"
    part = partition_by_posted(journals, res)
    assert part["write"] == journals and part["left_out"] == []   # both stay, flagged


def test_unchecked_journal_is_kept_and_listed():
    report = T._gc_tieout_report()
    journals = build_journals(report, T._GC_TIEOUT_ACCOUNTS)
    part = partition_by_posted(journals, [])
    assert part["unchecked"] == journals and part["write"] == journals


# ----------------------------------------------------------------- accrual --

def test_accrual_posted_is_not_reoffered_and_unposted_is(env):
    env.state["months"] = ["2025-04"]
    _, guids = _tree()
    env.state["llp"] = {"current_profit_share": 307000.0, "current_opening_balance": 100000.0,
                        "current_closing_balance": None, "capital_closing_balance": None}
    book = _write_book(env.tmp, "ac1", _deposits(guids, ["2025-04"]))
    res, jr, ar, _ = env(book, with_accrual=True, tag="ac1")
    assert ar.exists(), _text(res)                      # unposted accrual IS offered
    arows = _by_id(_read(ar))
    (aid,) = arows
    book2 = _write_book(env.tmp, "ac2", _deposits(guids, ["2025-04"])
                        + [_txn_from_csv(guids, arows[aid], "posted-accr")])
    res2, jr2, ar2, _ = env(book2, with_accrual=True, tag="ac2")
    assert not ar2.exists()                             # NEGATIVE: posted accrual not re-offered
    assert jr2.exists()                                  # the unposted month is unaffected
    t = _text(res2)
    assert "Accrual journal: not written" in t and "Already in the book (left out)" in t
    assert "RED FLAG" not in t
