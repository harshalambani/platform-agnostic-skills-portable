"""
tests/test_account_number_matching.py — Enhancement 5: account-number-first
GnuCash account resolution in skill_gnucash_pipeline.

Previously ``_get_gnucash_account_balance`` matched purely by bank-name
substring and took the first hit — silently wrong whenever a book has more
than one account for the same bank (e.g. two BoB accounts). This verifies
the fix: when statement metadata carries an account number, it is
normalised to digits and matched against digits embedded in the GnuCash
account name (e.g. "BOB - 100000000001"); a name-only match still works as
a fallback but is reported via "match_warning" so it's visible in the run
log instead of being a silent guess.

Uses a minimal synthetic gzip-compressed .gnucash book (same construction
style as tests/test_gnucash_accounts.py) with no transactions, since these
tests only exercise account *resolution*, not balance summation.
"""
from __future__ import annotations

import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_pipeline.agent import _get_gnucash_account_balance  # noqa: E402

_NS_DECL = (
    'xmlns:gnc="http://www.gnucash.org/XML/gnc" '
    'xmlns:act="http://www.gnucash.org/XML/act"'
)


def _account_xml(name: str, aid: str, atype: str, parent: str | None) -> str:
    parts = [
        '  <gnc:account version="2.0.0">',
        f"    <act:name>{name}</act:name>",
        f'    <act:id type="guid">{aid}</act:id>',
        f"    <act:type>{atype}</act:type>",
    ]
    if parent is not None:
        parts.append(f'    <act:parent type="guid">{parent}</act:parent>')
    parts.append("  </gnc:account>")
    return "\n".join(parts)


def _book_xml(accounts: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<gnc-v2 {_NS_DECL}>\n"
        '<gnc:book version="2.0.0">\n'
        + "\n".join(accounts)
        + "\n</gnc:book>\n</gnc-v2>\n"
    )


def _write_book(tmp_path, accounts) -> str:
    p = tmp_path / "book.gnucash"
    p.write_bytes(gzip.compress(_book_xml(accounts).encode("utf-8")))
    return str(p)


def test_two_same_bank_accounts_resolved_by_number(tmp_path):
    """Two BoB accounts in the book; the statement's account number picks
    the right one instead of always landing on the first match."""
    accounts = [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Assets", "asset", "ASSET", "root"),
        _account_xml("BoB - Savings 100000000001", "bob1", "BANK", "asset"),
        _account_xml("BoB - Current 100000000002", "bob2", "BANK", "asset"),
    ]
    book = _write_book(tmp_path, accounts)

    result = _get_gnucash_account_balance(book, "BoB", account_number="100000000002")
    assert result["found"] is True
    assert result["account_name"].endswith("BoB - Current 100000000002")
    assert result["match_warning"] is None


def test_number_vs_name_conflict_prefers_number_and_warns_on_fallback(tmp_path):
    """If the account number matches no candidate, fall back to the first
    name match but report the mismatch via match_warning instead of
    silently guessing."""
    accounts = [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Assets", "asset", "ASSET", "root"),
        _account_xml("BoB - Savings 100000000001", "bob1", "BANK", "asset"),
    ]
    book = _write_book(tmp_path, accounts)

    result = _get_gnucash_account_balance(book, "BoB", account_number="999999999999")
    assert result["found"] is True
    assert result["account_name"].endswith("BoB - Savings 100000000001")
    assert result["match_warning"] is not None
    assert "999999999999" in result["match_warning"]


def test_matching_number_wins_even_when_name_match_would_pick_a_different_account(tmp_path):
    """The account-number match must be preferred even though iteration
    order would otherwise select a different (also name-matching) account
    first."""
    accounts = [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Assets", "asset", "ASSET", "root"),
        _account_xml("BoB - Old Closed 100000000000", "bob_old", "BANK", "asset"),
        _account_xml("BoB - Active 100000000003", "bob_active", "BANK", "asset"),
    ]
    book = _write_book(tmp_path, accounts)

    result = _get_gnucash_account_balance(book, "BoB", account_number="100000000003")
    assert result["account_name"].endswith("BoB - Active 100000000003")
    assert result["match_warning"] is None


def test_no_account_number_falls_back_to_name_match_no_warning_for_single_candidate(tmp_path):
    accounts = [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Assets", "asset", "ASSET", "root"),
        _account_xml("HDFC Bank", "hdfc", "BANK", "asset"),
    ]
    book = _write_book(tmp_path, accounts)

    result = _get_gnucash_account_balance(book, "HDFC")
    assert result["found"] is True
    assert result["account_name"].endswith("HDFC Bank")
    assert result["match_warning"] is None


def test_no_account_number_multiple_candidates_stops_and_asks(tmp_path):
    # IMP-08: was "warn and use the first" -- now it must NOT pick one.
    accounts = [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Assets", "asset", "ASSET", "root"),
        _account_xml("BoB - Savings 111", "bob1", "BANK", "asset"),
        _account_xml("BoB - Current 222", "bob2", "BANK", "asset"),
    ]
    book = _write_book(tmp_path, accounts)

    result = _get_gnucash_account_balance(book, "BoB")
    assert result["found"] is False
    assert result["ambiguous"] is True
    assert result["account_name"] == ""
    assert "no account number" in result["match_warning"].lower()
    assert len(result["candidates"]) == 2


def test_account_not_found_still_reports_match_warning_key(tmp_path):
    accounts = [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Assets", "asset", "ASSET", "root"),
    ]
    book = _write_book(tmp_path, accounts)

    result = _get_gnucash_account_balance(book, "NoSuchBank")
    assert result["found"] is False
    assert result["match_warning"] is None
