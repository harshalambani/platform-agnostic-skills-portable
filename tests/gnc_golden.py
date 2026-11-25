"""Helper for tests/test_gnc_switch_readers.py (GNC-01 PR 2).

``collect(book)`` runs every switched reader on one book and returns a plain,
JSON-able dict, so the same function can be run against main (old readers) and
this branch (new readers) and the outputs compared byte for byte. The caller
must have put src/ (and the scripts dirs) on sys.path first. Nothing here
touches a real book.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path


def _norm(obj):
    return json.loads(json.dumps(obj, default=_default, sort_keys=True))


def _default(o):
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {f.name: getattr(o, f.name) for f in dataclasses.fields(o)
                if f.name != "root"}
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=repr)
    if isinstance(o, Path):
        return o.name
    return repr(o)


def _scrub(out: dict, book) -> dict:
    """Replace the book's own path so gzip and plain copies compare equal."""
    text = json.dumps(out, sort_keys=True)
    p = str(book)
    for form in (json.dumps(p)[1:-1], p):
        text = text.replace(form, "BOOK")
    text = text.replace(Path(book).stem, "BOOKSTEM")
    return json.loads(text)


def collect_group1(book) -> dict:
    from agents import gnucash_accounts as ga
    from agents.skill_gnucash_coverage import agent as cov
    from agents.skill_26as_journal import tools as t26
    from agents.skill_year_end_close import closing_check as cc

    book = Path(book)
    out = {}
    accts = ga.load_accounts(book)
    out["gnucash_accounts:load_accounts"] = _norm(accts)
    years, bal = ga.read_account_activity(book)
    out["gnucash_accounts:read_account_activity"] = _norm(
        {"years": {k: sorted(v) for k, v in years.items()},
         "bal": {k: str(v) for k, v in bal.items()}})
    guard = ga.TargetGuard.from_book(book)
    out["gnucash_accounts:TargetGuard.from_book"] = _norm(
        {"known": guard.known,
         "reasons": {a.path: guard.blocked_target_reason(a.path) for a in accts}})
    root = cov._read_root(book)
    ob = frozenset(a.id for a in accts if "opening-balance" in a.special_flags)
    out["skill_gnucash_coverage.agent:_read_root+_collect_account_dates"] = _norm(
        cov._collect_account_dates(root, ob))
    res = cc.check_closed(book, 2026)
    out["skill_year_end_close.closing_check:check_closed"] = _norm(res)
    out["skill_26as_journal.tools:_existing_account_paths"] = _norm(
        t26._existing_account_paths(str(book)))
    return _scrub(out, book)


def collect_group2(book) -> dict:
    import reconcile_intercompany as ric
    import parse_gnucash as pg
    import build_tds_journals as btj
    from agents.skill_gnucash_pipeline import agent as pipe
    from agents.skill_gnucash_reconciler import agent as rec
    from agents.skill_gnucash_xml_extractor import agent as ext

    book = Path(book)
    out = {}
    b = ric.load_book(str(book))
    out["skill_gnucash_intercompany.scripts.reconcile_intercompany:load_book"] = _norm(
        {"owner": b.owner, "fy": b.fy_from_name, "accounts": b.accounts})
    out["skill_gnucash_pipeline.agent:_get_gnucash_account_balance"] = _norm(
        pipe._get_gnucash_account_balance(str(book), "Demo Bank"))
    out["skill_gnucash_reconciler.agent:parse_gnucash_for_reconcile"] = _norm(
        rec.parse_gnucash_for_reconcile(str(book)))
    out["skill_gnucash_reconciler.agent:parse_gnucash_for_reconcile(filter)"] = _norm(
        rec.parse_gnucash_for_reconcile(str(book), "Assets:Bank:Demo Bank Savings"))
    ext_out = ext.parse_gnucash_file(str(book))
    ext_out.get("metadata", {}).pop("extraction_date", None)
    ext_out.pop("extraction_date", None)
    out["skill_gnucash_xml_extractor.agent:parse_gnucash_file"] = _norm(ext_out)
    out["skill_itr_workbook.scripts.parse_gnucash:parse_book"] = _norm(pg.parse_book(book))
    out["skill_26as_journal.scripts.build_tds_journals:load_accounts"] = _norm(
        btj.load_accounts(book))
    out["skill_26as_journal.scripts.build_tds_journals:load_account_balances"] = _norm(
        btj.load_account_balances(book))
    out["skill_26as_journal.scripts.build_tds_journals:load_book_journals_by_num"] = _norm(
        btj.load_book_journals_by_num(book))
    out["skill_26as_journal.scripts.build_tds_journals:load_book_txns"] = _norm(
        btj.load_book_txns(book))
    out["skill_26as_journal.scripts.build_tds_journals:monthly_debits_for_account"] = _norm(
        btj.monthly_debits_for_account(book, "Assets:Bank:Demo Bank Savings"))
    return _scrub(out, book)


def collect_group4(book) -> dict:
    """KRC regex readers. The script is loaded by path (it runs as its own
    process in the app), the same way tests/test_krc_gnucash_flags_charges.py
    loads it."""
    import importlib.util
    pkg = importlib.util.find_spec("agents").submodule_search_locations[0]
    script = Path(pkg) / "skill_krc_gnucash" / "scripts" / "build_krc_gnucash.py"
    spec = importlib.util.spec_from_file_location("build_krc_gnucash_golden", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    book = Path(book)
    acc, paths, stock_guids, holdings = mod.load_book(str(book))
    out = {
        "skill_krc_gnucash.scripts.build_krc_gnucash:load_book": _norm(
            {"acc": acc, "paths": paths, "stock_guids": stock_guids,
             "holdings": holdings}),
        "skill_krc_gnucash.scripts.build_krc_gnucash:load_book_txns": _norm(
            mod.load_book_txns(str(book), paths)),
    }
    return _scrub(out, book)
