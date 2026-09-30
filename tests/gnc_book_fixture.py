"""
tests/gnc_book_fixture.py -- a small synthetic .gnucash builder shared by the
IMP-09 / IMP-08 / MAP-1x tests.

Everything here is synthetic. The naming SHAPE copies a real book (a
"Root Account:" root, a placeholder "Assets" header, SEVERAL accounts at one
bank -- three postable HDFC accounts plus one hidden -- and two HSBC
accounts), with masked account numbers like 094XXXX1234 / 013065XXXX. No real
names, numbers or amounts.
"""
from __future__ import annotations

import gzip
from pathlib import Path

_NS_DECL = (
    'xmlns:gnc="http://www.gnucash.org/XML/gnc" '
    'xmlns:act="http://www.gnucash.org/XML/act" '
    'xmlns:trn="http://www.gnucash.org/XML/trn" '
    'xmlns:split="http://www.gnucash.org/XML/split" '
    'xmlns:ts="http://www.gnucash.org/XML/ts" '
    'xmlns:slot="http://www.gnucash.org/XML/slot"'
)


def account_xml(aid, name, atype, parent, flags=()):
    """flags: iterable of ('hidden'|'placeholder'|'tax-related'|...) or
    ('equity-type', 'opening-balance') tuples / bare boolean names."""
    parts = [
        '  <gnc:account version="2.0.0">',
        f"    <act:name>{name}</act:name>",
        f'    <act:id type="guid">{aid}</act:id>',
        f"    <act:type>{atype}</act:type>",
    ]
    if parent is not None:
        parts.append(f'    <act:parent type="guid">{parent}</act:parent>')
    slots = []
    for f in flags:
        if isinstance(f, tuple):
            k, v = f
        else:
            k, v = f, "true"
        slots.append(
            f'<slot><slot:key>{k}</slot:key>'
            f'<slot:value type="string">{v}</slot:value></slot>'
        )
    if slots:
        parts.append("    <act:slots>" + "".join(slots) + "</act:slots>")
    parts.append("  </gnc:account>")
    return "\n".join(parts)


def txn_xml(desc, date, legs, notes=None):
    """legs: list of (account_id, value_in_paise) -- must sum to 0."""
    assert sum(v for _a, v in legs) == 0, legs
    sp = "".join(
        "      <trn:split>\n"
        f"        <split:value>{v}/100</split:value>\n"
        f"        <split:quantity>{v}/100</split:quantity>\n"
        f'        <split:account type="guid">{a}</split:account>\n'
        "      </trn:split>\n"
        for a, v in legs
    )
    memo = ""
    if notes:
        memo = ("    <trn:slots><slot><slot:key>notes</slot:key>"
                f'<slot:value type="string">{notes}</slot:value></slot>'
                "</trn:slots>\n")
    return (
        '  <gnc:transaction version="2.0.0">\n'
        f"    <trn:description>{desc}</trn:description>\n"
        "    <trn:date-posted>\n"
        f"      <ts:date>{date} 00:00:00 +0000</ts:date>\n"
        "    </trn:date-posted>\n"
        + memo
        + "    <trn:splits>\n" + sp + "    </trn:splits>\n"
        "  </gnc:transaction>"
    )


def write_book(path: Path, accounts, transactions=()) -> str:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<gnc-v2 {_NS_DECL}>\n"
        '<gnc:book version="2.0.0">\n'
        + "\n".join(accounts) + "\n" + "\n".join(transactions)
        + "\n</gnc:book>\n</gnc-v2>\n"
    )
    Path(path).write_bytes(gzip.compress(xml.encode("utf-8")))
    return str(path)


# Account ids used by the standard book below.
HDFC1, HDFC2, HDFC_OLD, HDFC4 = "hdfc1", "hdfc2", "hdfc_old", "hdfc4"
HSBC1, HSBC2 = "hsbc1", "hsbc2"

P_HDFC1 = "Assets:Current Assets:Cash and Bank:HDFC Bank - 094XXXX1234"
P_HDFC2 = "Assets:Current Assets:Cash and Bank:HDFC Bank - 094XXXX5678"
P_HDFC_OLD = "Assets:Current Assets:Cash and Bank:HDFC Bank - 094XXXX9012 (old)"
P_HDFC4 = "Assets:Current Assets:Cash and Bank:HDFC Bank - 094XXXX3456"
P_HSBC1 = "Assets:Current Assets:Cash and Bank:HSBC Bank - 013065XXXX-001"
P_HSBC2 = "Assets:Current Assets:Cash and Bank:HSBC Bank - 013065XXXX-002"
P_SUSPENSE = "Assets:Suspense"


def standard_accounts(extra=()):
    """The standard synthetic chart. `extra` is appended."""
    a = [
        account_xml("root", "Root Account", "ROOT", None),
        account_xml("assets", "Assets", "ASSET", "root", ["placeholder"]),
        account_xml("ca", "Current Assets", "ASSET", "assets", ["placeholder"]),
        account_xml("cab", "Cash and Bank", "ASSET", "ca", ["placeholder"]),
        account_xml(HDFC1, "HDFC Bank - 094XXXX1234", "BANK", "cab"),
        account_xml(HDFC2, "HDFC Bank - 094XXXX5678", "BANK", "cab"),
        account_xml(HDFC_OLD, "HDFC Bank - 094XXXX9012 (old)", "BANK", "cab",
                    ["hidden"]),
        account_xml(HDFC4, "HDFC Bank - 094XXXX3456", "BANK", "cab"),
        account_xml(HSBC1, "HSBC Bank - 013065XXXX-001", "BANK", "cab"),
        account_xml(HSBC2, "HSBC Bank - 013065XXXX-002", "BANK", "cab"),
        account_xml("susp", "Suspense", "ASSET", "assets"),
        account_xml("exp", "Expenses", "EXPENSE", "root", ["placeholder"]),
        account_xml("food", "Food", "EXPENSE", "exp"),
        account_xml("dining", "Dining", "EXPENSE", "food"),          # postable
        account_xml("old", "Old", "EXPENSE", "exp", ["hidden"]),
        account_xml("old_dining", "Dining", "EXPENSE", "old", []),   # child of hidden
        account_xml("hid_dining", "Dining", "EXPENSE", "exp", ["hidden"]),  # same leaf, hidden
        account_xml("groc", "Groceries", "EXPENSE", "exp"),
        account_xml("grp", "Household", "EXPENSE", "exp", ["placeholder"]),
        account_xml("fuel", "Fuel", "EXPENSE", "grp"),               # child of placeholder
        account_xml("tax", "GST Paid", "EXPENSE", "exp", ["tax-related"]),
        account_xml("eq", "Equity", "EQUITY", "root", ["placeholder"]),
        account_xml("obe", "Opening Balances", "EQUITY", "eq",
                    [("equity-type", "opening-balance")]),
        account_xml("inc", "Income", "INCOME", "root", ["placeholder"]),
        account_xml("int", "Interest", "INCOME", "inc"),
    ]
    a.extend(extra)
    return a


def canonical_csv(path: Path, rows) -> str:
    """rows: list of (date, description, deposit, withdrawal)."""
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Transaction ID", "Description", "Account",
                    "Deposit", "Withdrawal", "Balance", "Currency"])
        for i, (d, desc, dep, wd) in enumerate(rows, 1):
            w.writerow([d, f"T{i}", desc, "", dep, wd, "", "INR"])
    return str(path)
