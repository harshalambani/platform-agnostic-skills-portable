"""
tests/test_gnucash_accounts.py — the shared placeholder/hidden-aware account
reader (agents.gnucash_accounts) and its use across the account-mapping skills.

Covers:
  * slot parsing for every special-type flag (placeholder, hidden, tax-related,
    auto-interest-transfer, opening-balance), incl. truthiness + gzip handling;
  * postable vs special path helpers;
  * a drift-guard tying the 26AS builder's self-contained flag-key copy to the
    shared constant so the two never diverge;
  * the review-tab picker excludes special accounts;
  * the 26AS matcher never offers a placeholder/hidden account as a candidate
    and routes a placeholder-only match to Suspense (the reported bug).

Run with:
    cd src && python -m pytest ../tests/test_gnucash_accounts.py -v
"""
from __future__ import annotations

import gzip
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import gnucash_accounts as ga  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic .gnucash book builder
# ---------------------------------------------------------------------------

_NS_DECL = (
    'xmlns:gnc="http://www.gnucash.org/XML/gnc" '
    'xmlns:act="http://www.gnucash.org/XML/act" '
    'xmlns:slot="http://www.gnucash.org/XML/slot"'
)


def _account_xml(name: str, aid: str, atype: str, parent: str | None,
                 slots: list[tuple[str, str]] | None = None) -> str:
    parts = [
        "  <gnc:account version=\"2.0.0\">",
        f"    <act:name>{name}</act:name>",
        f"    <act:id type=\"guid\">{aid}</act:id>",
        f"    <act:type>{atype}</act:type>",
    ]
    if parent is not None:
        parts.append(f"    <act:parent type=\"guid\">{parent}</act:parent>")
    if slots:
        parts.append("    <act:slots>")
        for key, value in slots:
            parts.append(
                "      <slot>"
                f"<slot:key>{key}</slot:key>"
                f"<slot:value type=\"string\">{value}</slot:value>"
                "</slot>"
            )
        parts.append("    </act:slots>")
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


def _sample_accounts() -> list[str]:
    return [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Income", "inc", "INCOME", "root",
                     slots=[("placeholder", "true")]),
        _account_xml("Interest Income", "int", "INCOME", "inc",
                     slots=[("placeholder", "true")]),          # header/parent
        _account_xml("Interest on HDFC - FD", "hdfc", "INCOME", "int"),  # normal
        _account_xml("Interest on Old Bank", "old", "INCOME", "int",
                     slots=[("hidden", "true")]),               # retired
        _account_xml("Assets", "asset", "ASSET", "root",
                     slots=[("placeholder", "true")]),
        _account_xml("HDFC Bank", "bank", "BANK", "asset"),     # normal
        _account_xml("Tax Deducted", "tax", "INCOME", "inc",
                     slots=[("tax-related", "1")]),             # tax-related (int)
        _account_xml("Auto Interest", "auto", "INCOME", "inc",
                     slots=[("auto-interest-transfer", "true")]),
        _account_xml("Opening Balances", "ob", "EQUITY", "root",
                     slots=[("equity-type", "opening-balance")]),
        _account_xml("Not Special", "ns", "INCOME", "inc",
                     slots=[("placeholder", "false")]),         # explicit false
    ]


@pytest.fixture()
def book(tmp_path) -> Path:
    p = tmp_path / "book.gnucash"
    p.write_text(_book_xml(_sample_accounts()), encoding="utf-8")
    return p


@pytest.fixture()
def gzbook(tmp_path) -> Path:
    p = tmp_path / "book_gz.gnucash"
    p.write_bytes(gzip.compress(_book_xml(_sample_accounts()).encode("utf-8")))
    return p


# ---------------------------------------------------------------------------
# load_accounts + flag parsing
# ---------------------------------------------------------------------------

def test_load_accounts_resolves_flags(book):
    by_path = {a.path: a for a in ga.load_accounts(book)}
    # placeholders
    assert "placeholder" in by_path["Income"].special_flags
    assert "placeholder" in by_path["Income:Interest Income"].special_flags
    # hidden
    assert "hidden" in by_path["Income:Interest Income:Interest on Old Bank"].special_flags
    # tax-related (integer truthy) + auto-interest-transfer
    assert "tax-related" in by_path["Income:Tax Deducted"].special_flags
    assert "auto-interest-transfer" in by_path["Income:Auto Interest"].special_flags
    # opening balance via equity-type
    assert "opening-balance" in by_path["Opening Balances"].special_flags
    # normal leaf carries no flag
    assert by_path["Income:Interest Income:Interest on HDFC - FD"].special_flags == frozenset()
    # explicit 'false' does not count
    assert by_path["Income:Not Special"].special_flags == frozenset()


def test_gzip_and_plain_parse_identically(book, gzbook):
    assert (sorted(a.path for a in ga.load_accounts(book))
            == sorted(a.path for a in ga.load_accounts(gzbook)))


def test_unreadable_file_returns_empty(tmp_path):
    bad = tmp_path / "nope.gnucash"
    bad.write_text("not xml at all", encoding="utf-8")
    assert ga.load_accounts(bad) == []
    assert ga.read_postable_paths(bad) == set()
    assert ga.read_special_paths(bad) == set()


# ---------------------------------------------------------------------------
# postable / special helpers
# ---------------------------------------------------------------------------

def test_postable_paths_excludes_all_specials(book):
    postable = ga.read_postable_paths(book)
    assert "Income:Interest Income:Interest on HDFC - FD" in postable
    assert "Assets:HDFC Bank" in postable
    assert "Income:Not Special" in postable
    for excluded in (
        "Income",
        "Income:Interest Income",
        "Income:Interest Income:Interest on Old Bank",  # hidden
        "Assets",
        "Income:Tax Deducted",
        "Income:Auto Interest",
        "Opening Balances",
    ):
        assert excluded not in postable, excluded


def test_special_paths_are_exactly_the_flagged(book):
    assert ga.read_special_paths(book) == {
        "Income",
        "Income:Interest Income",
        "Income:Interest Income:Interest on Old Bank",
        "Assets",
        "Income:Tax Deducted",
        "Income:Auto Interest",
        "Opening Balances",
    }


def test_root_is_not_postable(book):
    postable = ga.postable_accounts(ga.load_accounts(book))
    assert all(a.type != "ROOT" for a in postable)


# ---------------------------------------------------------------------------
# Drift guard: the 26AS builder keeps its OWN copy of the flag keys
# ---------------------------------------------------------------------------

def _load_script(rel_parts: tuple[str, ...], mod_name: str):
    script = SRC.joinpath(*rel_parts)
    spec = importlib.util.spec_from_file_location(mod_name, script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_builder():
    return _load_script(
        ("agents", "skill_26as_journal", "scripts", "build_tds_journals.py"),
        "build_tds_journals_drift",
    )


def _load_krc_builder():
    return _load_script(
        ("agents", "skill_krc_gnucash", "scripts", "build_krc_gnucash.py"),
        "build_krc_gnucash_drift",
    )


def test_builder_flag_keys_match_shared_constant():
    """The subprocess builders can't import `agents` in a frozen child, so each
    mirrors the boolean flag keys. Guard every copy against drift from
    agents.gnucash_accounts.BOOL_FLAG_KEYS."""
    tds = _load_builder()
    krc = _load_krc_builder()
    assert tuple(tds.SPECIAL_BOOL_FLAG_KEYS) == tuple(ga.BOOL_FLAG_KEYS)
    assert tuple(krc.SPECIAL_BOOL_FLAG_KEYS) == tuple(ga.BOOL_FLAG_KEYS)
    assert tds._OPENING_BALANCE_VALUE == "opening-balance"


def test_builder_load_accounts_sets_special_from_slots(book):
    """The builder's self-contained parser must flag the same accounts."""
    builder = _load_builder()
    by_path = {a.path: a for a in builder.load_accounts(book)}
    assert by_path["Income:Interest Income"].special is True
    assert by_path["Income:Interest Income:Interest on Old Bank"].special is True
    assert by_path["Opening Balances"].special is True
    assert by_path["Income:Interest Income:Interest on HDFC - FD"].special is False
    assert by_path["Income:Not Special"].special is False


# ---------------------------------------------------------------------------
# KRC broker skill: securities never match a placeholder/hidden STOCK account,
# but FIFO holdings replay still sees them.
# ---------------------------------------------------------------------------

def _krc_stock_book() -> list[str]:
    return [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Assets", "assets", "ASSET", "root",
                     slots=[("placeholder", "true")]),
        _account_xml("Stocks", "stocks", "STOCK", "assets",
                     slots=[("placeholder", "true")]),        # header
        _account_xml("Reliance Industries", "rel", "STOCK", "stocks"),  # normal
        _account_xml("Delisted Co", "del", "STOCK", "stocks",
                     slots=[("hidden", "true")]),             # retired holding
    ]


def test_krc_match_candidates_exclude_special_stock(tmp_path):
    krc = _load_krc_builder()
    p = tmp_path / "stocks.gnucash"
    p.write_text(_book_xml(_krc_stock_book()), encoding="utf-8")
    acc, paths, stock_guids, holdings = krc.load_book(str(p))
    # FIFO replay keeps ALL STOCK accounts (a hidden holding's lots still count).
    assert {paths[g] for g in stock_guids} == {
        "Assets:Stocks",
        "Assets:Stocks:Reliance Industries",
        "Assets:Stocks:Delisted Co",
    }
    # Match candidates exclude the placeholder header and the hidden holding.
    candidates = [paths[g] for g in stock_guids if not acc[g]["special"]]
    assert candidates == ["Assets:Stocks:Reliance Industries"]
    # A normal security still matches; the hidden one does not.
    assert krc.match_security("Reliance Industries", candidates, {})[0] == \
        "Assets:Stocks:Reliance Industries"
    assert krc.match_security("Delisted Co", candidates, {})[0] != \
        "Assets:Stocks:Delisted Co"


# ---------------------------------------------------------------------------
# Review-tab picker excludes special accounts
# ---------------------------------------------------------------------------

def test_review_picker_excludes_special_accounts(book):
    pytest.importorskip("gradio")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from ui.tabs.gnucash_review import _extract_account_tree
    paths = _extract_account_tree(str(book))
    assert "Income:Interest Income:Interest on HDFC - FD" in paths
    assert "Income:Interest Income" not in paths                 # placeholder
    assert "Income:Interest Income:Interest on Old Bank" not in paths  # hidden
    assert "Opening Balances" not in paths                       # equity-type
    # single-level groups are still filtered out (historical behaviour)
    assert "Income" not in paths
