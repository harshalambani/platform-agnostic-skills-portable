"""
TDS-15 -- 26AS journal: where the NET interest (c - a) is booked, the Review
screen's Leave out, and the pre-flight on income-account balances.

Defect: Category A posts Dr TDS (a), Dr <generic Interest on FD> (c - a),
Cr <matched interest account> (c). If the real books booked the maturity wholly
to the FD asset (or never booked bond / EPF interest), the generic income
account is driven to a DEBIT balance on import.

Synthetic accounts, deductors and books only. Each section carries negative
tests (the wrong behaviour does NOT occur).
"""
from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_tds_journal_fixes import (
    GENERIC_FD, STD, acc, ded, m, read_csv, write_26as, write_book,
)
from ui.tabs import tds_journal_review as tjr

ZEN_INC = "Income:Interest Income:Interest on Zenith Bank - FD"
ZEN_FD = "Assets:Investments:Zenith Bank FD"
ZEN_FD2 = "Assets:Investments:Zenith Bank FD Second"
EPF_ASSET = "Assets:Investments:EPF"


def chart(*extra):
    base = [acc(ZEN_INC), acc(GENERIC_FD),
            acc("Expense:TDS on Interest", "EXPENSE"),
            acc("Expense:TDS on Dividend", "EXPENSE"),
            acc("Liabilities:Suspense", "LIABILITY")]
    return base + list(extra)


def asset(path, blocked=False):
    return acc(path, "ASSET", blocked=blocked)


def zenith(tan=""):
    d = ded(1, "ZENITH BANK", "194A", 10000.0, 1000.0)
    d.tan = tan
    return d


def accounts_of(j):
    return {s.account for s in j.splits}


@pytest.fixture(autouse=True)
def _allow_tmp_as_known_folder(tmp_path, monkeypatch):
    from ui import _safe_paths
    monkeypatch.setattr(_safe_paths, "known_folders", lambda: [tmp_path.resolve()])


# ---------------------------------------------------------------------------
# (a) the net-interest account: learned > unique match > generic
# ---------------------------------------------------------------------------

def test_unique_matching_asset_is_the_default_and_generic_is_not_debited():
    j = m.build_journals([zenith()], chart(asset(ZEN_FD)))[0]
    debits = {s.account: s.debit for s in j.splits if s.debit}
    assert debits[ZEN_FD] == 9000.0
    assert j.net_interest_source == "unique match"
    assert GENERIC_FD not in accounts_of(j)              # NEGATIVE: not the generic account
    assert j.balanced and not j.net_interest_choice_needed


def test_learned_choice_beats_the_unique_match_and_the_generic():
    other = "Assets:Investments:Some Other Holding"
    key = m.tds_learnings.deductor_key(m.tds_learnings.DOMAIN_NETINT, "", "ZENITH BANK")
    j = m.build_journals([zenith()], chart(asset(ZEN_FD), asset(other)),
                         learnings={key: other})[0]
    assert j.net_interest_account == other and j.net_interest_source == "learned"
    assert GENERIC_FD not in accounts_of(j) and ZEN_FD not in accounts_of(j)


def test_no_category_a_row_debits_generic_when_a_learned_asset_exists():
    key = m.tds_learnings.deductor_key(m.tds_learnings.DOMAIN_NETINT, "", "ZENITH BANK")
    j = m.build_journals([zenith()], chart(asset(ZEN_FD), asset(ZEN_FD2)),
                         learnings={key: ZEN_FD2})[0]
    assert GENERIC_FD not in accounts_of(j)
    assert j.net_interest_account == ZEN_FD2 and not j.net_interest_choice_needed


def test_ambiguous_match_is_not_guessed_falls_back_and_asks():
    j = m.build_journals([zenith()], chart(asset(ZEN_FD), asset(ZEN_FD2)))[0]
    assert j.net_interest_choice_needed is True
    assert j.net_interest_account == GENERIC_FD                  # fallback, not a pick
    assert ZEN_FD not in accounts_of(j) and ZEN_FD2 not in accounts_of(j)   # NEGATIVE
    assert j.needs_review is True
    assert "NET INTEREST ACCOUNT NEEDS A CHOICE" in j.credit_basis
    assert ZEN_FD in j.credit_basis and ZEN_FD2 in j.credit_basis
    assert j.balanced


def test_no_candidate_falls_back_to_generic_exactly_as_before():
    j = m.build_journals([zenith()], chart())[0]
    assert j.net_interest_source == "generic" and not j.net_interest_choice_needed
    assert j.net_interest_account == GENERIC_FD
    assert [(s.account, s.debit, s.credit) for s in j.splits] == [
        (m.ACC_TDS_INTEREST, 1000.0, 0), (GENERIC_FD, 9000.0, 0), (ZEN_INC, 0, 10000.0)]


def test_an_unrelated_asset_is_not_a_candidate_a_bare_FD_or_Bank_is_not_enough():
    accts = chart(asset("Assets:Investments:Other Bank FD"), asset("Assets:Current Assets:Cash"))
    assert m.find_net_interest_candidates("ZENITH BANK", accts) == []        # NEGATIVE
    j = m.build_journals([zenith()], accts)[0]
    assert j.net_interest_source == "generic"


def test_a_hidden_or_placeholder_asset_is_never_defaulted_or_offered():
    accts = chart(asset(ZEN_FD, blocked=True))
    assert m.find_net_interest_candidates("ZENITH BANK", accts) == []
    j = m.build_journals([zenith()], accts)[0]
    assert ZEN_FD not in accounts_of(j) and j.net_interest_source == "generic"
    # two candidates of which one is blocked is NOT ambiguous: only one is usable
    accts2 = chart(asset(ZEN_FD), asset(ZEN_FD2, blocked=True))
    assert m.find_net_interest_candidates("ZENITH BANK", accts2) == [ZEN_FD]


def test_a_learned_account_that_is_now_hidden_is_not_used_and_says_so():
    key = m.tds_learnings.deductor_key(m.tds_learnings.DOMAIN_NETINT, "", "ZENITH BANK")
    j = m.build_journals([zenith()], chart(asset(ZEN_FD, blocked=True)),
                         learnings={key: ZEN_FD})[0]
    assert ZEN_FD not in accounts_of(j)
    assert j.net_interest_source == "generic"
    assert "not a usable account" in j.credit_basis


def test_a_learned_choice_for_one_deductor_does_not_leak_to_another():
    key = m.tds_learnings.deductor_key(m.tds_learnings.DOMAIN_NETINT, "", "SOME OTHER PAYER")
    j = m.build_journals([zenith()], chart(), learnings={key: ZEN_FD})[0]
    assert ZEN_FD not in accounts_of(j)


def test_epf_deductor_matches_a_single_epf_asset_through_the_alias_table():
    d = ded(2, "EMPLOYEES PROVIDENT FUND ORGANISATION", "192A", 8000.0, 800.0)
    inc = acc("Income:Interest Income:Interest on EPF Taxable")
    accts = chart(inc, asset(EPF_ASSET))
    assert m.find_net_interest_candidates(d.name, accts) == [EPF_ASSET]


def test_dividend_category_b_output_is_unchanged():
    d = ded(3, "ZENITH BANK", "194", 5000.0, 500.0)
    base = m.build_journals([d], chart(acc("Income:Dividend - Shares:Dividend - Zenith Bank")))[0]
    withasset = m.build_journals(
        [d], chart(acc("Income:Dividend - Shares:Dividend - Zenith Bank"), asset(ZEN_FD)))[0]
    assert base.category == "B"
    assert [(s.account, s.debit, s.credit) for s in base.splits] == \
        [(s.account, s.debit, s.credit) for s in withasset.splits]
    assert withasset.net_interest_source == ""                       # NEGATIVE: not a Cat A concept


def test_s194t_stays_out_of_the_journal():
    d = ded(4, "ZENITH PARTNERS LLP", "194T", 100000.0, 10000.0)
    js = m.build_journals([d], chart(asset(ZEN_FD)), partner_comp_configured=True)
    assert js[0].category == "C" and js[0].excluded_from_journal is True
    rows = m.build_csv_rows(js, "2526")
    assert rows == []                                                # NEGATIVE: nothing posted


def test_category_g_15g_journals_are_untouched():
    d = ded(5, "ZENITH BANK", "194A", 10000.0, 0.0)
    js = m.build_15g_journals([d], chart(asset(ZEN_FD)))
    assert js and js[0].category == "G"
    assert ZEN_FD not in accounts_of(js[0])                          # NEGATIVE


# ---------------------------------------------------------------------------
# (c) the balance reader and the pre-flight
# ---------------------------------------------------------------------------

def jrows(*splits, txn="2526-TDSJ01"):
    return [{"Transaction ID": txn, "Description": "d", "Account": a, "Amount": str(v)}
            for a, v in splits]


def inc_accounts():
    return [acc(GENERIC_FD), acc(ZEN_INC), asset(ZEN_FD),
            acc("Expense:TDS on Interest", "EXPENSE")]


def test_preflight_blocks_when_an_income_account_would_go_to_a_debit_balance():
    rows = jrows((m.ACC_TDS_INTEREST, 1000), (GENERIC_FD, 9000), (ZEN_INC, -10000))
    issues = m.income_debit_preflight(rows, {GENERIC_FD: -2000.0}, inc_accounts())
    assert [i["account"] for i in issues] == [GENERIC_FD]
    assert issues[0]["post_credit"] == -7000.0 and issues[0]["book_credit"] == 2000.0
    assert issues[0]["rows"][0][0] == "2526-TDSJ01"
    text = "\n".join(m.format_preflight(issues))
    assert text.startswith("RED FLAG") and GENERIC_FD in text and "2526-TDSJ01" in text


def test_preflight_does_not_block_a_clean_run():
    rows = jrows((m.ACC_TDS_INTEREST, 1000), (GENERIC_FD, 9000), (ZEN_INC, -10000))
    assert m.income_debit_preflight(rows, {GENERIC_FD: -20000.0}, inc_accounts()) == []
    # exactly zero afterwards is not a debit balance
    assert m.income_debit_preflight(rows, {GENERIC_FD: -9000.0}, inc_accounts()) == []


def test_preflight_a_repointed_row_that_debits_an_asset_does_not_block():
    rows = jrows((m.ACC_TDS_INTEREST, 1000), (ZEN_FD, 9000), (ZEN_INC, -10000))
    assert m.income_debit_preflight(rows, {}, inc_accounts()) == []        # NEGATIVE


def test_preflight_credits_to_income_are_never_flagged():
    rows = jrows((m.ACC_TDS_INTEREST, 1000), (ZEN_FD, 9000), (ZEN_INC, -10000))
    assert ZEN_INC not in {i["account"] for i in
                           m.income_debit_preflight(rows, {ZEN_INC: 0.0}, inc_accounts())}


def test_preflight_an_income_account_not_yet_in_the_book_counts_as_zero_balance():
    rows = jrows((m.ACC_TDS_INTEREST, 1000), ("Income:Interest Income:New", 9000), (ZEN_INC, -10000))
    issues = m.income_debit_preflight(rows, {}, inc_accounts())
    assert [i["account"] for i in issues] == ["Income:Interest Income:New"]


def book_with_txns(path, specs, txns):
    """write_book + posted transactions: txns = [[(account path, value), ...], ...]."""
    p = write_book(path, specs)
    text = p.read_text(encoding="utf-8")
    root = ET.fromstring(text)
    by_id, ids = {}, {}
    for a in root.iter("{http://www.gnucash.org/XML/gnc}account"):
        aid = a.find("{http://www.gnucash.org/XML/act}id").text
        nm = a.find("{http://www.gnucash.org/XML/act}name").text
        par = a.find("{http://www.gnucash.org/XML/act}parent")
        by_id[aid] = (nm, par.text if par is not None else None)
    for aid in by_id:
        parts, cur = [], aid
        while cur in by_id:
            parts.append(by_id[cur][0])
            cur = by_id[cur][1]
        ids[":".join(reversed(parts[:-1]))] = aid
    body = ""
    for t in txns:
        body += "<gnc:transaction><trn:splits>"
        for acct, val in t:
            body += ('<trn:split><split:account type="guid">%s</split:account>'
                     '<split:value>%d/100</split:value></trn:split>' % (ids[acct], round(val * 100)))
        body += "</trn:splits></gnc:transaction>"
    text = text.replace("<gnc-v2 ", '<gnc-v2 xmlns:trn="http://www.gnucash.org/XML/trn" '
                        'xmlns:split="http://www.gnucash.org/XML/split" ', 1)
    text = text.replace("</gnc:book>", body + "</gnc:book>")
    p.write_text(text, encoding="utf-8")
    return p


BOOK_SPECS = STD + [(ZEN_INC, "INCOME", ()), (GENERIC_FD, "INCOME", ()),
                    ("Assets:Bank", "BANK", ())]


def test_load_account_balances_reads_the_book_and_never_writes(tmp_path):
    book = book_with_txns(tmp_path / "b.gnucash", BOOK_SPECS,
                          [[("Assets:Bank", 500.0), (GENERIC_FD, -500.0)],
                           [("Assets:Bank", 100.0), (GENERIC_FD, -100.0)]])
    before = book.read_bytes()
    bal = m.load_account_balances(book)
    assert bal[GENERIC_FD] == -600.0 and bal["Assets:Bank"] == 600.0
    assert book.read_bytes() == before


def test_run_raises_a_red_flag_when_generic_interest_on_fd_would_go_negative(tmp_path):
    book = book_with_txns(tmp_path / "b.gnucash", BOOK_SPECS,
                          [[("Assets:Bank", 2000.0), (GENERIC_FD, -2000.0)]])
    x = write_26as(tmp_path / "x.xlsx", {"Part I": [(1, "ZENITH BANK", "194A", 10000.0, 1000.0)]})
    stats = m.run(x, book, tmp_path / "out.csv")
    assert [i["account"] for i in stats["preflight_issues"]] == [GENERIC_FD]
    assert stats["preflight_error"] == ""


def test_run_is_clean_when_the_net_interest_goes_to_a_unique_asset(tmp_path):
    specs = BOOK_SPECS + [(ZEN_FD, "ASSET", ())]
    book = book_with_txns(tmp_path / "b.gnucash", specs,
                          [[("Assets:Bank", 2000.0), (GENERIC_FD, -2000.0)]])
    x = write_26as(tmp_path / "x.xlsx", {"Part I": [(1, "ZENITH BANK", "194A", 10000.0, 1000.0)]})
    stats = m.run(x, book, tmp_path / "out.csv")
    assert stats["preflight_issues"] == []                           # NEGATIVE: no false block
    assert GENERIC_FD not in {r["Account"] for r in read_csv(tmp_path / "out.csv")}


def test_main_prints_the_red_flag_before_the_summary_line(tmp_path, capsys):
    book = book_with_txns(tmp_path / "b.gnucash", BOOK_SPECS, [])
    x = write_26as(tmp_path / "x.xlsx", {"Part I": [(1, "ZENITH BANK", "194A", 10000.0, 1000.0)]})
    m.main(["build_tds_journals.py", str(x), str(book), str(tmp_path / "out.csv")])
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("RED FLAG")


def test_unreadable_book_reports_not_checked_never_clean(tmp_path):
    book = book_with_txns(tmp_path / "b.gnucash", BOOK_SPECS, [])
    x = write_26as(tmp_path / "x.xlsx", {"Part I": [(1, "ZENITH BANK", "194A", 10000.0, 1000.0)]})
    m.run(x, book, tmp_path / "out.csv")
    issues, err = m.preflight_for_csv(tmp_path / "out.csv", tmp_path / "missing.gnucash")
    assert issues == [] and "could not be read" in err


def test_skill_summary_leads_with_the_red_flag_and_the_reply_withholds_download(tmp_path):
    from agents.skill_26as_journal import tools as tl
    book = book_with_txns(tmp_path / "b.gnucash", BOOK_SPECS, [])
    x = write_26as(tmp_path / "x.xlsx", {"Part I": [(1, "ZENITH BANK", "194A", 10000.0, 1000.0)]})
    m.run(x, book, tmp_path / "out.csv")
    text = tl.final_summary(str(tmp_path / "out.csv"), str(book))
    assert text.startswith("**RED FLAG")
    issues, _e = tl.preflight_issues(str(tmp_path / "out.csv"), str(book))
    assert issues
    from agents.outputs import ReplyWithOutputs
    assert ReplyWithOutputs("t", (), withhold_primary=True).withhold_primary is True
    assert ReplyWithOutputs("t").withhold_primary is False            # NEGATIVE: default offers


def test_clean_summary_has_no_red_flag(tmp_path):
    from agents.skill_26as_journal import tools as tl
    specs = BOOK_SPECS + [(ZEN_FD, "ASSET", ())]
    book = book_with_txns(tmp_path / "b.gnucash", specs, [])
    x = write_26as(tmp_path / "x.xlsx", {"Part I": [(1, "ZENITH BANK", "194A", 10000.0, 1000.0)]})
    m.run(x, book, tmp_path / "out.csv")
    assert "RED FLAG" not in tl.final_summary(str(tmp_path / "out.csv"), str(book))


# ---------------------------------------------------------------------------
# Review screen: net-interest edit, Leave out, gating, learning
# ---------------------------------------------------------------------------

def _review_setup(tmp_path, specs, txns=(), rows=None):
    book = book_with_txns(tmp_path / "b.gnucash", specs, list(txns))
    rows = rows or [(1, "ZENITH BANK", "194A", 10000.0, 1000.0)]
    x = write_26as(tmp_path / "x.xlsx", {"Part I": rows})
    out = tmp_path / "2526-tds-journals.csv"
    m.run(x, book, out)
    review = tmp_path / "2526-tds-journals-review.csv"
    assert review.is_file()
    return book, out, review


def _payload(review, book, rows=(), excluded=None, changes=()):
    p = {"context": {"review_path": str(review), "gnucash_path": str(book)},
         "changes": list(changes), "all_rows": list(rows)}
    if excluded is not None:
        p["excluded"] = list(excluded)
        p["excluded_dirty"] = True
    return json.dumps(p)


def _a_row(review, **extra):
    r = next(x for x in csv.DictReader(review.open(encoding="utf-8")) if x["Category"] == "A")
    r.update(extra)
    return r


def test_review_csv_carries_the_net_account_columns(tmp_path):
    specs = BOOK_SPECS + [(ZEN_FD, "ASSET", ())]
    _book, _out, review = _review_setup(tmp_path, specs)
    r = _a_row(review)
    assert r["Net Interest Account"] == ZEN_FD and r["Net Interest Source"] == "unique match"
    assert r["Left Out"] == ""


def test_review_net_edit_repoints_the_split_and_clears_the_choice_flag(tmp_path):
    specs = BOOK_SPECS + [(ZEN_FD, "ASSET", ()), (ZEN_FD2, "ASSET", ())]
    book, out, review = _review_setup(tmp_path, specs)
    r = _a_row(review)
    assert r["Net Interest Choice Needed"] == "yes" and r["Needs Review"] == "yes"
    r["Net Interest Edit"] = ZEN_FD2
    status, dl, _p = tjr._save_changes(_payload(review, book, rows=[r]))
    accs = {x["Account"] for x in read_csv(out)}
    assert ZEN_FD2 in accs and GENERIC_FD not in accs
    r2 = _a_row(review)
    assert r2["Net Interest Account"] == ZEN_FD2 and r2["Needs Review"] == ""
    assert dl["interactive"] is True


def test_review_net_edit_to_a_hidden_or_unknown_account_is_refused(tmp_path):
    specs = BOOK_SPECS + [(ZEN_FD, "ASSET", ()), ("Assets:Investments:Hidden FD", "ASSET", ("hidden",))]
    book, out, review = _review_setup(tmp_path, specs)
    before = out.read_text(encoding="utf-8")
    for bad in ("Assets:Investments:Hidden FD", "Assets:Nope:Nothing"):
        r = _a_row(review)
        r["Net Interest Edit"] = bad
        status, _dl, _p = tjr._save_changes(_payload(review, book, rows=[r]))
        assert "not a postable account" in status
        assert out.read_text(encoding="utf-8") == before             # NEGATIVE: nothing written


def test_review_net_edit_is_remembered_for_the_deductor(tmp_path):
    specs = BOOK_SPECS + [(ZEN_FD, "ASSET", ()), (ZEN_FD2, "ASSET", ())]
    book, _out, review = _review_setup(tmp_path, specs)
    r = _a_row(review)
    r["Net Interest Edit"] = ZEN_FD2
    tjr._save_changes(_payload(review, book, rows=[r]))
    learned = m.tds_learnings.load_learnings(str(book))
    key = m.tds_learnings.deductor_key(m.tds_learnings.DOMAIN_NETINT, "", "ZENITH BANK")
    assert learned[key] == ZEN_FD2
    # the next build picks it up by itself (no candidate guessing)
    x = write_26as(tmp_path / "y.xlsx", {"Part I": [(1, "ZENITH BANK", "194A", 10000.0, 1000.0)]})
    out2 = tmp_path / "again.csv"
    m.run(x, book, out2)
    assert ZEN_FD2 in {z["Account"] for z in read_csv(out2)}


def test_review_leaving_out_a_row_removes_it_from_both_downloads(tmp_path):
    specs = BOOK_SPECS + [(ZEN_FD, "ASSET", ())]
    book, out, review = _review_setup(tmp_path, specs)
    r = _a_row(review)
    status, dl, part = tjr._save_changes(_payload(review, book, rows=[], excluded=[r]))
    assert read_csv(out) == []                                       # full journal: row gone
    assert not (tmp_path / "2526-tds-journals-partI.csv").exists()   # NEGATIVE: not in Part I either
    assert _a_row_or_none(review)["Left Out"] == "yes"
    assert dl["value"] is not None or dl["interactive"] in (True, False)


def _a_row_or_none(review):
    return next(x for x in csv.DictReader(review.open(encoding="utf-8")) if x["Category"] == "A")


def test_review_left_out_row_is_in_neither_file_when_part_ii_exists(tmp_path):
    specs = BOOK_SPECS + [(ZEN_FD, "ASSET", ())]
    book, out, review = _review_setup(tmp_path, specs)
    # a 15G/15H (Part II) row sits in the same journal so a Part I file exists
    x = write_26as(tmp_path / "x2.xlsx", {
        "Part I": [(1, "ZENITH BANK", "194A", 10000.0, 1000.0)],
        "Part II": [(1, "ZENITH BANK", "194A", 5000.0, 0.0)]})
    out2 = tmp_path / "2526-tds-journals.csv"
    m.run(x, book, out2)
    r = _a_row(review)
    tjr._save_changes(_payload(review, book, rows=[], excluded=[r]))
    for f in (out2, tmp_path / "2526-tds-journals-partI.csv"):
        if f.exists():
            assert "TDSJ01" not in f.read_text(encoding="utf-8")


def test_review_putting_a_left_out_row_back_restores_its_splits(tmp_path):
    specs = BOOK_SPECS + [(ZEN_FD, "ASSET", ())]
    book, out, review = _review_setup(tmp_path, specs)
    original = read_csv(out)
    r = _a_row(review)
    tjr._save_changes(_payload(review, book, rows=[], excluded=[r]))
    assert read_csv(out) == []
    r = _a_row(review)
    tjr._save_changes(_payload(review, book, rows=[r], excluded=[]))
    assert sorted(x["Account"] for x in read_csv(out)) == sorted(x["Account"] for x in original)
    assert _a_row(review)["Left Out"] == ""
    assert m.tds_learnings  # module reachable; learnings unaffected by leave-out


def test_review_save_withholds_both_downloads_on_a_red_flag_and_offers_them_when_cleared(tmp_path):
    book, out, review = _review_setup(
        tmp_path, BOOK_SPECS + [(ZEN_FD, "ASSET", ()), (ZEN_FD2, "ASSET", ())],
        txns=[[("Assets:Bank", 100.0), (GENERIC_FD, -100.0)]])
    r = _a_row(review)
    assert r["Net Interest Account"] == GENERIC_FD                    # ambiguous -> generic
    # a trivial save that keeps the generic account: RED FLAG, no download
    r["Net Interest Edit"] = GENERIC_FD
    status, dl, part = tjr._save_changes(_payload(review, book, rows=[r], excluded=[]))
    assert status.lstrip("*").startswith("RED FLAG") and GENERIC_FD in status
    assert dl["interactive"] is False and dl["value"] is None
    # re-point to the asset: clean, download offered
    r = _a_row(review)
    r["Net Interest Edit"] = ZEN_FD
    status, dl, _p = tjr._save_changes(_payload(review, book, rows=[r], excluded=[]))
    assert "RED FLAG" not in status and dl["interactive"] is True


def test_review_save_without_a_book_says_not_checked(tmp_path):
    book, out, review = _review_setup(tmp_path, BOOK_SPECS + [(ZEN_FD, "ASSET", ())])
    r = _a_row(review)
    p = json.loads(_payload(review, book, rows=[r], excluded=[r]))
    p["context"]["gnucash_path"] = ""
    status, _dl, _p = tjr._save_changes(json.dumps(p))
    assert "NOT CHECKED" in status


def test_review_net_edit_on_a_dividend_row_is_refused(tmp_path):
    specs = BOOK_SPECS + [("Income:Dividend - Shares:Dividend - Zenith Bank", "INCOME", ())]
    book, out, review = _review_setup(
        tmp_path, specs, rows=[(1, "ZENITH BANK", "194", 5000.0, 500.0)])
    r = next(x for x in csv.DictReader(review.open(encoding="utf-8")))
    assert r["Category"] == "B"
    r["Net Interest Edit"] = GENERIC_FD
    before = out.read_text(encoding="utf-8")
    status, _dl, _p = tjr._save_changes(_payload(review, book, rows=[r]))
    assert "Category A" in status
    assert out.read_text(encoding="utf-8") == before
