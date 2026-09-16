"""
tests/test_gnucash_dedup_account_scope.py -- regression guard for IMP-01:
dedup was not scoped to the target account.

Previously skill_gnucash_pipeline.agent.run() called
parse_gnucash_for_reconcile(gnucash_file) with no account_filter, so the
duplicate-check index in reconcile() covered every split in the whole book,
keyed only by (date, amount). A same-date/same-amount posting in a
completely unrelated account (e.g. an Expense entry, or a clearing-account
leg) would then make a genuine, never-before-imported statement row read as
"Match" and get silently dropped -- never imported, never even reaching
contra detection.

This builds a synthetic HDFC CSV statement (via the existing
tests/skill_hdfc/hdfc_fixture_gen.py fixture) and a synthetic .gnucash book
with:
  - the target HDFC bank account, with NO transactions of its own;
  - an unrelated Expense account carrying one transaction with the exact
    same date and amount as the statement's first row (a NEFT salary credit
    of 50000.00 on 2025-04-01).

On unfixed code, the whole-book dedup index matches the Expense-account
transaction by (date, amount) alone and marks the salary-credit row a
duplicate, so it never appears in the mapped output CSV. Fixed, the dedup
index is scoped to the HDFC account (which has no transactions), so the row
must survive as "New" and appear in the output.
"""
from __future__ import annotations

import csv
import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT / "skill_hdfc") not in sys.path:
    sys.path.insert(0, str(ROOT / "skill_hdfc"))

REPO_ROOT = ROOT.parent
SRC = REPO_ROOT / "src"
for _p in (SRC, SRC / "agents"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import hdfc_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_gnucash_pipeline.agent import run as pipeline_run  # noqa: E402

_NS_DECL = (
    'xmlns:gnc="http://www.gnucash.org/XML/gnc" '
    'xmlns:act="http://www.gnucash.org/XML/act" '
    'xmlns:trn="http://www.gnucash.org/XML/trn" '
    'xmlns:split="http://www.gnucash.org/XML/split" '
    'xmlns:ts="http://www.gnucash.org/XML/ts"'
)

# Matches hdfc_fixture_gen.SYN_TRANSACTIONS row 0: NEFT SALARY CREDIT,
# value date 01/04/25 -> canonical Date 2025-04-01, Deposit 50000.00.
_UNRELATED_TXN_DATE = "2025-04-01 00:00:00 +0000"
_UNRELATED_TXN_AMOUNT = "5000000/100"  # 50000.00


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


def _transaction_xml(desc: str, date_posted: str, acct_id: str, value: str) -> str:
    return (
        '  <gnc:transaction version="2.0.0">\n'
        f"    <trn:description>{desc}</trn:description>\n"
        "    <trn:date-posted>\n"
        f"      <ts:date>{date_posted}</ts:date>\n"
        "    </trn:date-posted>\n"
        "    <trn:splits>\n"
        "      <trn:split>\n"
        f"        <split:value>{value}</split:value>\n"
        f'        <split:account type="guid">{acct_id}</split:account>\n'
        "      </trn:split>\n"
        "    </trn:splits>\n"
        "  </gnc:transaction>"
    )


def _book_xml(accounts: list[str], transactions: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<gnc-v2 {_NS_DECL}>\n"
        '<gnc:book version="2.0.0">\n'
        + "\n".join(accounts)
        + "\n"
        + "\n".join(transactions)
        + "\n</gnc:book>\n</gnc-v2>\n"
    )


def _write_book(tmp_path: Path, accounts: list[str], transactions: list[str]) -> str:
    p = tmp_path / "book.gnucash"
    p.write_bytes(gzip.compress(_book_xml(accounts, transactions).encode("utf-8")))
    return str(p)


def _build_book_with_unrelated_same_date_amount_posting(tmp_path: Path) -> str:
    """HDFC target account with zero transactions of its own; an unrelated
    Expense account carries a posting with the same date+amount as the
    statement's first (NEFT salary credit) row."""
    accounts = [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Assets", "asset", "ASSET", "root"),
        _account_xml("HDFC Bank - SYN0001", "hdfc", "BANK", "asset"),
        _account_xml("Expenses", "exp_top", "EXPENSE", "root"),
        _account_xml("Rent", "exp_rent", "EXPENSE", "exp_top"),
    ]
    transactions = [
        _transaction_xml(
            "Unrelated rent posting (decoy)",
            _UNRELATED_TXN_DATE,
            "exp_rent",
            _UNRELATED_TXN_AMOUNT,
        ),
    ]
    return _write_book(tmp_path, accounts, transactions)


def _read_output_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_unrelated_account_same_date_amount_does_not_dedupe_statement_row(tmp_path):
    """The core IMP-01 regression: a same-date/same-amount posting in an
    unrelated (Expense) account must never cause a genuine statement row to
    be treated as already-in-GnuCash and dropped."""
    csv_path = tmp_path / "syn_hdfc.csv"
    csv_path.write_text(fixture_gen.build_csv_text(), encoding="utf-8")
    gnucash_file = _build_book_with_unrelated_same_date_amount_posting(tmp_path)
    out_path = tmp_path / "out.csv"

    result = pipeline_run(
        bank="HDFC",
        statement_files=str(csv_path),
        gnucash_file=gnucash_file,
        output_path=str(out_path),
        config_path=None,
    )

    # Must not hit the "all transactions already in GnuCash" short-circuit.
    assert "already in GnuCash" not in result or "New" in result

    assert out_path.is_file(), f"pipeline did not produce an output CSV:\n{result}"
    rows = _read_output_rows(out_path)
    dates = [r.get("Date") for r in rows]
    deposits = [r.get("Deposit") for r in rows]
    assert "2025-04-01" in dates, (
        f"the NEFT salary-credit row (2025-04-01, 50000.00) was dropped as a "
        f"duplicate of an unrelated Expense posting -- dedup is not scoped to "
        f"the target account.\nrows={rows}\nlog={result}"
    )
    assert "50000.00" in deposits
