"""
tests/test_gnucash_balance_account_scope.py -- regression guard for IMP-05:
the second, later ``_get_gnucash_account_balance`` resolution site in
skill_gnucash_pipeline.agent.run() (used to scope dedup, and for the CSV
Account column / contra-detection eligibility) must resolve the SAME
account as the earlier opening-balance reconciliation -- by the statement's
account number, never by a bare first-name match on the bank name.

Background: PR #256 (IMP-01) moved this resolution earlier and made it
reused for both dedup-scoping and the later "Bank account: ..." log line
-- collapsing what used to be two separate _get_gnucash_account_balance()
calls into one. But that single remaining call was still invoked as
``_get_gnucash_account_balance(gnucash_file, bank)`` -- WITHOUT the
statement's account number -- while the opening-balance reconciliation a
few lines above it (_reconcile_opening_balance) already resolves the
account WITH the account number. Whenever a book has more than one account
sharing the same bank name (e.g. several Bank of Baroda accounts,
distinguished only by account number), those two resolutions could
disagree: opening-balance reconciliation would correctly report against
the statement's real account, while dedup / the CSV Account column / contra
detection would silently scope to a *different* account picked by name
order alone.

This file verifies the fix: the later resolution now also receives the
statement's account number, so it agrees with the earlier one, and when an
account number is given but matches none of several same-name candidates,
the resolver refuses to guess (reports unresolved) rather than silently
picking one.

Fixtures: a synthetic Bank of Baroda statement PDF (tests/skill_bob's
bob_fixture_gen, which already supports overriding the embedded account
number via ``build_pdf_for(..., account_number=...)``) against a synthetic
gzipped .gnucash XML book built here in tmp_path, following the same
construction style as tests/test_gnucash_dedup_account_scope.py and
tests/test_account_number_matching.py.
"""
from __future__ import annotations

import csv
import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT / "skill_bob") not in sys.path:
    sys.path.insert(0, str(ROOT / "skill_bob"))

REPO_ROOT = ROOT.parent
SRC = REPO_ROOT / "src"
for _p in (SRC, SRC / "agents"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import bob_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_gnucash_pipeline.agent import run as pipeline_run  # noqa: E402

_NS_DECL = (
    'xmlns:gnc="http://www.gnucash.org/XML/gnc" '
    'xmlns:act="http://www.gnucash.org/XML/act" '
    'xmlns:trn="http://www.gnucash.org/XML/trn" '
    'xmlns:split="http://www.gnucash.org/XML/split" '
    'xmlns:ts="http://www.gnucash.org/XML/ts"'
)

# bob_fixture_gen.SYN_TRANSACTIONS row 0: NEFT SALARY CREDIT, 01-04-2025,
# deposit 50,000.00 -> canonical Date 2025-04-01, Deposit 50000.00.
_DECOY_TXN_DATE = "2025-04-01 00:00:00 +0000"
_DECOY_TXN_AMOUNT = "5000000/100"  # 50000.00, matches the statement's first row

_ACCOUNT_1 = "1111111111"  # the statement's real account
_ACCOUNT_2 = "2222222222"  # a different account sharing the same bank name
_ACCOUNT_NONE = "9999999999"  # matches neither


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


def _two_bob_accounts_book(tmp_path: Path) -> str:
    """Two Bank of Baroda accounts sharing the same bank name, differing
    only by embedded account number:
      - "...2222222222" is declared FIRST (so a bare name-match, which just
        takes the first candidate in document order, would wrongly land
        here) and carries a seed balance of 500000.00 PLUS a decoy posting
        with the exact same date+amount as the statement's first row.
      - "...1111111111" is declared SECOND and is the statement's real
        account (per its embedded "A/C Number"), seeded to exactly the
        statement's opening balance (100000.00) and otherwise empty.
    """
    accounts = [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Assets", "asset", "ASSET", "root"),
        _account_xml(f"Bank of Baroda - {_ACCOUNT_2}", "bob2", "BANK", "asset"),
        _account_xml(f"Bank of Baroda - {_ACCOUNT_1}", "bob1", "BANK", "asset"),
    ]
    transactions = [
        # Seed account 2's balance far away from the statement's, and give
        # it a same-date/same-amount decoy that would wrongly dedupe the
        # statement's first row if dedup were (mis)scoped to this account.
        _transaction_xml("Seed balance (account 2)", "2025-01-01 00:00:00 +0000", "bob2", "50000000/100"),
        _transaction_xml("Decoy salary credit (account 2)", _DECOY_TXN_DATE, "bob2", _DECOY_TXN_AMOUNT),
        # Seed account 1's balance to match the statement's opening balance
        # exactly, so a correct resolution reconciles cleanly.
        _transaction_xml("Seed balance (account 1)", "2025-01-01 00:00:00 +0000", "bob1", "10000000/100"),
    ]
    return _write_book(tmp_path, accounts, transactions)


def _statement_pdf(tmp_path: Path, account_number: str) -> Path:
    pdf_path = tmp_path / "syn_bob_stmt.pdf"
    pdf_path.write_bytes(
        fixture_gen.build_pdf_for(
            fixture_gen.SYN_TRANSACTIONS,
            fixture_gen.SYN_PERIOD_FROM,
            fixture_gen.SYN_PERIOD_TO,
            account_number=account_number,
        )
    )
    return pdf_path


def _read_output_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── The fixed-path test ────────────────────────────────────────────────────

def test_balance_and_dedup_use_the_statement_account_not_a_name_only_match(tmp_path):
    """Core IMP-05 regression: with two same-bank-name accounts, the balance
    and dedup scoping used by the pipeline must be the one matching the
    statement's own account number (2), never the other, same-name account
    that merely happens to sort/declare first (1)."""
    pdf_path = _statement_pdf(tmp_path, _ACCOUNT_1)
    gnucash_file = _two_bob_accounts_book(tmp_path)
    out_path = tmp_path / "out.csv"

    result = pipeline_run(
        bank="Bank of Baroda",
        statement_files=str(pdf_path),
        gnucash_file=gnucash_file,
        output_path=str(out_path),
        config_path=None,
    )

    # The resolved account must be the statement's own account (1), not (2).
    assert f"Bank of Baroda - {_ACCOUNT_1}" in result, (
        f"pipeline did not resolve the statement's own account "
        f"({_ACCOUNT_1}).\nlog={result}"
    )
    assert f"Bank account: `Assets:Bank of Baroda - {_ACCOUNT_2}`" not in result, (
        f"pipeline scoped the balance/dedup lookup to the WRONG account "
        f"({_ACCOUNT_2}) instead of the statement's own account "
        f"({_ACCOUNT_1}).\nlog={result}"
    )

    # Opening balance must reconcile cleanly against account 1 (seeded to
    # exactly the statement's opening balance) -- not flag a mismatch, which
    # would happen if account 2 (seeded to a wildly different balance) had
    # been used instead.
    assert "OPENING BALANCE MISMATCH" not in result, (
        f"opening balance was reconciled against the wrong account.\n"
        f"log={result}"
    )

    # Dedup must be scoped to account 1 (which has no transactions of its
    # own), so the statement's first row must survive as new -- not be
    # scoped to account 2's decoy posting (same date+amount) and dropped as
    # a false duplicate.
    assert out_path.is_file(), f"pipeline did not produce an output CSV:\n{result}"
    rows = _read_output_rows(out_path)
    dates = [r.get("Date") for r in rows]
    assert "2025-04-01" in dates, (
        f"the statement's first row (2025-04-01, 50000.00) was dropped -- "
        f"dedup was scoped to the WRONG account's decoy posting instead of "
        f"the statement's own account.\nrows={rows}\nlog={result}"
    )


# ── Negative tests ──────────────────────────────────────────────────────────
# These assert the WRONG behaviour does NOT occur.

def test_balance_is_not_silently_taken_from_the_other_same_name_account(tmp_path):
    """Direct negative check on the resolved balance itself: the reported
    account must not be account 2's, and account 2's seeded balance
    (500000.00) must never appear as if it were the statement's account
    balance."""
    pdf_path = _statement_pdf(tmp_path, _ACCOUNT_1)
    gnucash_file = _two_bob_accounts_book(tmp_path)
    out_path = tmp_path / "out.csv"

    result = pipeline_run(
        bank="Bank of Baroda",
        statement_files=str(pdf_path),
        gnucash_file=gnucash_file,
        output_path=str(out_path),
        config_path=None,
    )

    assert "500000.00" not in result, (
        f"account 2's balance (500000.00) leaked into the pipeline output -- "
        f"the balance lookup used the wrong account.\nlog={result}"
    )


def test_account_number_matching_nothing_does_not_silently_fall_back(tmp_path):
    """When the statement's account number matches NEITHER of several
    same-bank-name candidates, the pipeline must say so and skip dedup --
    never silently fall back to one of them by name alone. If it fell back
    to account 2, the decoy posting there would wrongly dedupe away the
    statement's first row, exactly like the original defect."""
    pdf_path = _statement_pdf(tmp_path, _ACCOUNT_NONE)
    gnucash_file = _two_bob_accounts_book(tmp_path)
    out_path = tmp_path / "out.csv"

    result = pipeline_run(
        bank="Bank of Baroda",
        statement_files=str(pdf_path),
        gnucash_file=gnucash_file,
        output_path=str(out_path),
        config_path=None,
    )

    assert "Duplicate check skipped" in result, (
        f"an account number matching no candidate must skip dedup rather "
        f"than silently scoping to a guessed account.\nlog={result}"
    )
    assert "not resolving to any one of them automatically" in result or (
        f"Could not match account number '{_ACCOUNT_NONE}'" in result
    ), (
        f"the ambiguity must be reported plainly in the log, not silently "
        f"swallowed.\nlog={result}"
    )
    assert f"Bank account: `Assets:Bank of Baroda - {_ACCOUNT_2}`" not in result
    assert f"Bank account: `Assets:Bank of Baroda - {_ACCOUNT_1}`" not in result
    assert "500000.00" not in result, (
        "account 2's balance must not leak in even as a guessed fallback."
    )

    assert out_path.is_file(), f"pipeline did not produce an output CSV:\n{result}"
    rows = _read_output_rows(out_path)
    dates = [r.get("Date") for r in rows]
    assert "2025-04-01" in dates, (
        f"with dedup skipped, the statement's first row must survive, not "
        f"be silently dropped via a guessed account's decoy posting.\n"
        f"rows={rows}\nlog={result}"
    )


# ── Guard: the single-account case that already worked ─────────────────────

def test_single_matching_account_still_resolves_and_dedupes_normally(tmp_path):
    """Regression guard: with only ONE Bank of Baroda account in the book,
    resolution and dedup scoping must keep working exactly as before this
    fix -- the account-number/ambiguity handling must not regress the
    common single-account case."""
    accounts = [
        _account_xml("Root Account", "root", "ROOT", None),
        _account_xml("Assets", "asset", "ASSET", "root"),
        _account_xml(f"Bank of Baroda - {_ACCOUNT_1}", "bob1", "BANK", "asset"),
    ]
    transactions = [
        _transaction_xml("Seed balance", "2025-01-01 00:00:00 +0000", "bob1", "10000000/100"),
    ]
    gnucash_file = _write_book(tmp_path, accounts, transactions)
    pdf_path = _statement_pdf(tmp_path, _ACCOUNT_1)
    out_path = tmp_path / "out.csv"

    result = pipeline_run(
        bank="Bank of Baroda",
        statement_files=str(pdf_path),
        gnucash_file=gnucash_file,
        output_path=str(out_path),
        config_path=None,
    )

    assert f"Bank account: `Assets:Bank of Baroda - {_ACCOUNT_1}`" in result, (
        f"the single existing account must still resolve normally.\n"
        f"log={result}"
    )
    assert "OPENING BALANCE MISMATCH" not in result
    assert "Duplicate check skipped" not in result

    assert out_path.is_file(), f"pipeline did not produce an output CSV:\n{result}"
    rows = _read_output_rows(out_path)
    dates = [r.get("Date") for r in rows]
    assert "2025-04-01" in dates
