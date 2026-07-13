"""
fixture_gen.py -- synthetic eguile HTML fixture generator for the ITR
Workbook skill's tests (plan section 8).

Entities SYN-IND and SYN-HUF. NO real family data appears here: all names,
GUIDs and amounts are made up for testing only. The generated HTML
deliberately replicates the real DOM's quirks: gnc-register:acct-guid
anchors, td.indent depth cells, a security-cell balance (qty/symbol/value
triple), a trailing Exchange Rates table with a rational-fraction price, an
Imbalance Amount row, and a couple of unclosed/malformed tags (the real
reports are not well-formed HTML).
"""
from __future__ import annotations

import hashlib


def _guid(seed: str) -> str:
    """Deterministic fake 32-hex GUID from a seed string (not a real account)."""
    return hashlib.md5(seed.encode()).hexdigest()


def _indent_cells(depth: int) -> str:
    return "".join(
        '<td min-width="32" class="indent">&nbsp;&nbsp;</td>' for _ in range(depth)
    )


def _fmt(value: float) -> str:
    """Indian lakh grouping, matching the eguile report's number format."""
    negative = value < 0
    value = abs(value)
    whole = int(value)
    frac = round((value - whole) * 100)
    s = str(whole)
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        s = ",".join(groups) + "," + last3
    out = f"{s}.{frac:02d}"
    return f"-{out}" if negative else out


def _account_row(depth: int, guid: str, name: str, value: float | None,
                  colspan: int, negative_class: bool = False, unclosed: bool = False) -> str:
    if value is None:
        balance_html = '<td class="balance" align="right">&nbsp;</td>'
    else:
        amt = f"₹&nbsp;&nbsp;{_fmt(value)}"
        span = f'<span style="white-space:nowrap;">{amt}</span>'
        if negative_class:
            span = f'<span class="negative">{span}</span>'
        balance_html = f'<td class="balance" align="right">{span}</td>'
    name_td_open = "<td colspan=\"%d\" class=\"accname\">" % colspan
    # Replicates the real reports' occasional unclosed <td> -- the anchor's
    # cell is left open, relying on the tolerant parser to auto-close it when
    # the next tag opens (bs4's html.parser builder does this correctly).
    name_td_close = "" if unclosed else "</td>"
    return f"""
<tr valign="bottom">
{_indent_cells(depth)}
{name_td_open}
<a href="gnc-register:acct-guid={guid}">{name}</a>{name_td_close}
<td class="empty"></td>
{balance_html}
<td class="empty"></td>
</tr>
"""


def _total_row(depth: int, guid: str, name: str, value: float, colspan: int,
               negative_class: bool = False, section_style: bool = False) -> str:
    amt = f"₹&nbsp;&nbsp;{_fmt(value)}"
    span = f'<span style="white-space:nowrap;">{amt}</span>'
    if negative_class:
        span = f'<span class="negative">{span}</span>'
    if section_style:
        return f"""
<tr valign="bottom">
<td colspan="4" class="accnametotal">
<b>Total <a href="gnc-register:acct-guid={guid}">{name}</a></b></td>
<td class="empty"></td><td class="empty"></td><td class="overruled">&nbsp;</td>
<td class="balancetotal" align="right"><b>{span}</b></td>
</tr>
"""
    return f"""
<tr valign="bottom">
{_indent_cells(depth)}
<td colspan="{colspan}" class="accname">
Total <a href="gnc-register:acct-guid={guid}">{name}</a></td>
<td class="empty"></td><td class="overruled">&nbsp;</td>
<td class="balance" align="right">{span}</td>
</tr>
"""


def _security_row(depth: int, guid: str, name: str, qty: str, symbol: str,
                   value: float, colspan: int) -> str:
    amt = f"₹&nbsp;&nbsp;{_fmt(value)}"
    return f"""
<tr valign="bottom">
{_indent_cells(depth)}
<td colspan="{colspan}" class="accname">
<a href="gnc-register:acct-guid={guid}">{name}</a></td>
<td class="empty"></td>
<td class="balance" align="right"><span class="foreign"><span style="white-space:nowrap;">{qty}&nbsp;{symbol}</span></span>&nbsp;<span style="white-space:nowrap;">{amt}</span></td>
<td class="empty"></td>
</tr>
"""


def _section_header(label: str) -> str:
    return f"""
<tr valign="bottom">
<td colspan="4" class="accnametotal">
<b>{label}</b></td>
<td class="empty"></td><td class="empty"></td>
<td class="balancetotal" align="right"><b>&nbsp;</b></td>
</tr>
"""


def _section_total(label: str, value: float) -> str:
    amt = f"₹&nbsp;&nbsp;{_fmt(value)}"
    span = f'<span style="white-space:nowrap;">{amt}</span>'
    if value < 0:
        span = f'<span class="negative">{span}</span>'
    return f"""
<tr valign="bottom">
<td colspan="4" class="accnametotal">
<b>{label}</b></td>
<td class="empty"></td><td class="empty"></td><td class="empty"></td>
<td class="ruledtotal" align="right"><b>{span}</b></td>
</tr>
"""


def _spacer() -> str:
    return '<tr valign="center"><td colspan="8">&nbsp;</td></tr>'


def _exchange_rates_table(entity: str) -> str:
    return f"""
<p><strong>Exchange Rates</strong> used for this report
<table border="0">
<tr>
  <td align="right">1. {entity}SCRIP</td>
  <td>=</td>
  <td align="right">₹ 115 + 93/220</td>
</tr>
</table>
"""


def _wrap(title: str, body: str, truncated: bool = False) -> str:
    tail = "" if truncated else "</table>\n</table>\n"
    exchange = "" if truncated else _exchange_rates_table("SYN")
    return f"""<!-- The HTML starts here... -->
<html dir='auto'>
<head>
<meta http-equiv="content-type" content="text-html; charset=utf-8">
<title>{title}</title>
</head>
<body>
<h3>{title}</h3>
<h2>Balance Sheet (eguile) 31-03-2025</h2>

<table border="0" class="outer"><tr valign="top"><td valign="top">
<table border="0" class="accounts" align="left">
{body}
{tail}
{exchange}
</body>
</html>
"""


def build_syn_ind_html(*, truncated: bool = False, nonzero_imbalance: bool = False) -> str:
    """SYN-IND: salary-adjacent business + CG + OS + one security holding +
    one unmapped-looking leaf, replicating the real DOM quirks."""
    g = lambda s: _guid(f"SYN-IND:{s}")  # noqa: E731

    body = []
    body.append(_section_header("Assets Accounts"))
    body.append(_account_row(0, g("Assets"), "Assets", None, 4))
    body.append(_account_row(1, g("Cash and Bank"), "Cash and Bank", None, 3))
    body.append(_account_row(2, g("Cash"), "Cash", 25000.00, 2, unclosed=True))
    # Bumped +475000.00 (B4), then +1300.00 (B6, RULE-1 refund fixture) to
    # absorb the RE-side changes and keep Total Assets == ETL + RE.
    body.append(_account_row(2, g("HDFC Bank"), "HDFC Bank - SYN001", 891300.50, 2))
    body.append(_total_row(1, g("Cash and Bank"), "Cash and Bank", 916300.50, 3))
    body.append(_account_row(1, g("Investments"), "Investments", None, 3))
    body.append(_security_row(2, g("SynthCorp"), "SynthCorp Shares", "1,000.", "SYNCORP.NS", 150000.00, 2))
    body.append(_total_row(1, g("Investments"), "Investments", 150000.00, 3))
    body.append(_account_row(1, g("Misc Holding"), "Misc Holding (unmapped)", 5000.00, 3))
    body.append(_section_total("Total Assets Accounts", 1071300.50))
    body.append(_spacer())

    body.append(_section_header("Liability Accounts"))
    body.append(_section_total("Total Liability Accounts", 0.00))
    body.append(_spacer())

    body.append(_section_header("Trading Accounts"))
    body.append(_section_total("Total Trading Accounts", 0.00))
    body.append(_spacer())

    body.append(_section_header("Equity Accounts"))
    body.append(_account_row(0, g("Equity"), "Equity", None, 4))
    body.append(_account_row(1, g("Capital Account"), "Capital Account", 400000.50, 3))
    body.append(_section_total("Total Equity Accounts", 400000.50))
    body.append(_spacer())
    body.append(_section_total("Total Equity, Trading, and Liabilities", 400000.50))
    body.append(_spacer())

    body.append(_section_header("Retained Earnings"))
    body.append(_account_row(0, g("Income"), "Income", None, 4))
    body.append(_account_row(1, g("Business Remuneration"), "Business Remuneration", 300000.00, 3))
    body.append(_account_row(1, g("Bank Interest"), "Bank Interest", 20000.00, 3))
    # B4: Salary income -- SALARY_GROSS, reconciles with the synthetic Form16
    # fixture's 17(1) (build_syn_ind_form16_lines) for the Book<->Form16
    # cross-check.
    body.append(_account_row(1, g("Salary"), "Salary", 500000.00, 3))
    # B6 (RULE-1 golden): refund principal (excluded, not income) + refund
    # interest (taxable) -- both booked to Income per D16/OQ-2 (two separate
    # accounts from entry, so RULE-1 is structurally enforced).
    body.append(_account_row(1, g("IT Refund Principal"), "IT Refund Principal", 1000.00, 3))
    body.append(_account_row(1, g("IT Refund Interest"), "IT Refund Interest", 300.00, 3))
    body.append(_total_row(0, g("Income"), "Income", 821300.00, 4, section_style=True))
    body.append(_account_row(0, g("Expense"), "Expense", None, 4))
    body.append(_account_row(1, g("Business Expenses"), "Business Expenses", -120000.00, 3, negative_class=True))
    body.append(_account_row(1, g("TDS on Interest"), "TDS on Interest", -5000.00, 3, negative_class=True))
    # B4: TDS on salary -- TAXPAID_TDS_SALARY, reconciles with the synthetic
    # Form16 fixture's net tax payable (item 21).
    body.append(_account_row(1, g("TDS on Salary"), "TDS on Salary", -25000.00, 3, negative_class=True))
    body.append(_total_row(0, g("Expense"), "Expense", -150000.00, 4, negative_class=True, section_style=True))
    re_total = 821300.00 - 150000.00
    # total_assets(1,071,300.50) == etl(400000.50) + re_total should hold:
    # 400000.50 + 671300.00 = 1,071,300.50
    body.append(_section_total("Total Retained Earnings", re_total))
    body.append(_spacer())

    if not truncated:
        imbalance = 999.99 if nonzero_imbalance else 0.00
        body.append(_section_total("Imbalance Amount", imbalance))

    return _wrap("SYN-IND Balance Sheet (eguile) 31-03-2025", "\n".join(body), truncated=truncated)


def build_syn_huf_html(*, truncated: bool = False, nonzero_imbalance: bool = False) -> str:
    """SYN-HUF: Other Sources + Capital Gains only, no salary seam."""
    g = lambda s: _guid(f"SYN-HUF:{s}")  # noqa: E731

    body = []
    body.append(_section_header("Assets Accounts"))
    body.append(_account_row(0, g("Assets"), "Assets", None, 4))
    body.append(_account_row(1, g("Cash and Bank"), "Cash and Bank", None, 3))
    body.append(_account_row(2, g("Cash"), "Cash", 10000.00, 2))
    body.append(_total_row(1, g("Cash and Bank"), "Cash and Bank", 10000.00, 3))
    body.append(_section_total("Total Assets Accounts", 10000.00))
    body.append(_spacer())

    body.append(_section_header("Liability Accounts"))
    body.append(_section_total("Total Liability Accounts", 0.00))
    body.append(_spacer())

    body.append(_section_header("Trading Accounts"))
    body.append(_section_total("Total Trading Accounts", 0.00))
    body.append(_spacer())

    body.append(_section_header("Equity Accounts"))
    body.append(_account_row(0, g("Equity"), "Equity", None, 4))
    body.append(_account_row(1, g("Capital Account"), "Capital Account", 8500.00, 3))
    body.append(_section_total("Total Equity Accounts", 8500.00))
    body.append(_spacer())
    body.append(_section_total("Total Equity, Trading, and Liabilities", 8500.00))
    body.append(_spacer())

    body.append(_section_header("Retained Earnings"))
    body.append(_account_row(0, g("Income"), "Income", None, 4))
    body.append(_account_row(1, g("Interest Income"), "Interest Income", 2000.00, 3))
    body.append(_account_row(1, g("Long Term Capital Gain"), "Long Term Capital Gain", 1000.00, 3))
    body.append(_total_row(0, g("Income"), "Income", 3000.00, 4, section_style=True))
    body.append(_account_row(0, g("Expense"), "Expense", None, 4))
    body.append(_account_row(1, g("TDS on Interest"), "TDS on Interest", -1500.00, 3, negative_class=True))
    body.append(_total_row(0, g("Expense"), "Expense", -1500.00, 4, negative_class=True, section_style=True))
    re_total = 3000.00 - 1500.00
    body.append(_section_total("Total Retained Earnings", re_total))
    body.append(_spacer())

    if not truncated:
        imbalance = 999.99 if nonzero_imbalance else 0.00
        body.append(_section_total("Imbalance Amount", imbalance))

    return _wrap("SYN-HUF Balance Sheet (eguile) 31-03-2025", "\n".join(body), truncated=truncated)


# ---------------------------------------------------------------------------
# Synthetic .gnucash (gzipped GnuCash-v2 XML) generation (Batch 2, plan
# section 8): a book that RECONCILES with the HTML fixtures above -- every
# account shared with the HTML reuses the SAME _guid() seed, and the FY
# 2024-25 sum of every income/expense leaf equals its HTML total exactly.
# CG-lot and dividend/interest accounts added here are book-only (not part
# of the HTML backbone), which is fine: the cross-check only compares GUIDs
# present in BOTH files, and zero-balance-at-FY-end stock accounts are
# legitimately absent from the HTML (the real export omits zero balances).
# ---------------------------------------------------------------------------

_GNC_NS = (
    'xmlns:gnc="http://www.gnucash.org/XML/gnc" '
    'xmlns:act="http://www.gnucash.org/XML/act" '
    'xmlns:book="http://www.gnucash.org/XML/book" '
    'xmlns:cmdty="http://www.gnucash.org/XML/cmdty" '
    'xmlns:trn="http://www.gnucash.org/XML/trn" '
    'xmlns:split="http://www.gnucash.org/XML/split" '
    'xmlns:ts="http://www.gnucash.org/XML/ts" '
    'xmlns:slot="http://www.gnucash.org/XML/slot"'
)


def _frac(x: float) -> str:
    """Float rupees -> exact 'num/100' GnuCash rational string."""
    return f"{round(x * 100)}/100"


def _gnc_account(guid: str, name: str, typ: str, parent_guid: str | None,
                  commodity_space: str = "CURRENCY", commodity_id: str = "INR") -> str:
    parent_xml = (f'<act:parent type="guid">{parent_guid}</act:parent>' if parent_guid else "")
    return f"""
<gnc:account version="2.0.0">
  <act:name>{name}</act:name>
  <act:id type="guid">{guid}</act:id>
  <act:type>{typ}</act:type>
  <act:commodity>
    <cmdty:space>{commodity_space}</cmdty:space>
    <cmdty:id>{commodity_id}</cmdty:id>
  </act:commodity>
  {parent_xml}
</gnc:account>
"""


def _gnc_split(guid: str, value: float, quantity: float, account_guid: str,
               action: str | None = None) -> str:
    action_xml = f"<split:action>{action}</split:action>" if action else ""
    return f"""
    <trn:split>
      <split:id type="guid">{guid}</split:id>
      {action_xml}
      <split:reconciled-state>n</split:reconciled-state>
      <split:value>{_frac(value)}</split:value>
      <split:quantity>{_frac(quantity)}</split:quantity>
      <split:account type="guid">{account_guid}</split:account>
    </trn:split>
"""


def _gnc_txn(guid: str, iso_date: str, description: str, splits_xml: list[str]) -> str:
    return f"""
<gnc:transaction version="2.0.0">
  <trn:id type="guid">{guid}</trn:id>
  <trn:date-posted>
    <ts:date>{iso_date} 10:59:00 +0000</ts:date>
  </trn:date-posted>
  <trn:date-entered>
    <ts:date>{iso_date} 10:59:00 +0000</ts:date>
  </trn:date-entered>
  <trn:description>{description}</trn:description>
  <trn:splits>
    {"".join(splits_xml)}
  </trn:splits>
</gnc:transaction>
"""


def _gnc_document(accounts_xml: list[str], txns_xml: list[str]) -> str:
    return f"""<?xml version="1.0" encoding="utf-8" ?>
<gnc-v2 {_GNC_NS}>
<gnc:count-data cd:type="book" xmlns:cd="http://www.gnucash.org/XML/cd">1</gnc:count-data>
<gnc:book version="2.0.0">
<book:id type="guid">{_guid("book-id")}</book:id>
{"".join(accounts_xml)}
{"".join(txns_xml)}
</gnc:book>
</gnc-v2>
"""


def build_syn_ind_gnucash() -> str:
    """Synthetic SYN-IND book: reuses the HTML fixture's GUIDs for the shared
    accounts (Business Remuneration/Bank Interest/Business Expenses/TDS on
    Interest all sum to their HTML totals for FY 2024-25), plus book-only
    CG-lot and dividend/interest accounts covering every plan section 6.1/1.2
    shape: a FIFO-consistent two-lot sale (one gain, one loss lot), an
    intra-year STCG sale, an ambiguous cost-match sale, an engineered FIFO
    violation, quarterly dividends across 3 buckets with a 31-03 TDS
    gross-up entry, and bank/NBFC/savings-bank interest accounts."""
    g = lambda s: _guid(f"SYN-IND:{s}")  # noqa: E731

    root = g("Root")
    accounts = [_gnc_account(root, "Root Account", "ROOT", None)]

    def acc(guid, name, typ, parent, space="CURRENCY", cid="INR"):
        accounts.append(_gnc_account(guid, name, typ, parent, space, cid))

    acc(g("Assets"), "Assets", "ASSET", root)
    acc(g("Cash and Bank"), "Cash and Bank", "ASSET", g("Assets"))
    acc(g("Cash"), "Cash", "ASSET", g("Cash and Bank"))
    acc(g("HDFC Bank"), "HDFC Bank - SYN001", "ASSET", g("Cash and Bank"))
    acc(g("Investments"), "Investments", "ASSET", g("Assets"))
    acc(g("SynthCorp"), "SynthCorp Shares", "STOCK", g("Investments"), "NSE", "SYNCORP.NS")
    acc(g("Misc Holding"), "Misc Holding (unmapped)", "ASSET", g("Assets"))
    acc(g("Zerodha"), "Zerodha", "ASSET", g("Assets"))
    acc(g("OldTech"), "OldTech Ltd", "STOCK", g("Investments"), "NSE", "OLDTECH.NS")
    acc(g("QuickFlip"), "QuickFlip Ltd", "STOCK", g("Investments"), "NSE", "QUICKFLIP.NS")
    acc(g("AmbiguousCo"), "AmbiguousCo Ltd", "STOCK", g("Investments"), "NSE", "AMBIG.NS")
    acc(g("ViolatorCo"), "ViolatorCo Ltd", "STOCK", g("Investments"), "NSE", "VIOLATOR.NS")
    acc(g("StraddleCo"), "StraddleCo Ltd", "STOCK", g("Investments"), "NSE", "STRADDLE.NS")

    acc(g("Equity"), "Equity", "EQUITY", root)
    acc(g("Capital Account"), "Capital Account", "EQUITY", g("Equity"))

    acc(g("Income"), "Income", "INCOME", root)
    acc(g("Business Remuneration"), "Business Remuneration", "INCOME", g("Income"))
    acc(g("Bank Interest"), "Bank Interest", "INCOME", g("Income"))
    acc(g("Salary"), "Salary", "INCOME", g("Income"))
    acc(g("IT Refund Principal"), "IT Refund Principal", "INCOME", g("Income"))
    acc(g("IT Refund Interest"), "IT Refund Interest", "INCOME", g("Income"))
    acc(g("LTCG"), "Long Term Capital Gain", "INCOME", g("Income"))
    acc(g("STCG"), "Short Term Capital Gain", "INCOME", g("Income"))
    acc(g("DividendShares"), "Dividend - Shares", "INCOME", g("Income"))
    acc(g("NBFCInterest"), "Interest - NBFC", "INCOME", g("Income"))
    acc(g("SavingsInterest"), "Interest - Savings Bank", "INCOME", g("Income"))
    acc(g("BankFDInterest"), "Interest - Bank FD", "INCOME", g("Income"))

    acc(g("Expense"), "Expense", "EXPENSE", root)
    acc(g("Business Expenses"), "Business Expenses", "EXPENSE", g("Expense"))
    acc(g("TDS on Interest"), "TDS on Interest", "EXPENSE", g("Expense"))
    acc(g("TDS on Salary"), "TDS on Salary", "EXPENSE", g("Expense"))
    acc(g("TDSOnDividend"), "TDS on Dividend", "EXPENSE", g("Expense"))

    txns = []

    def txn(seed, iso_date, description, splits):
        txns.append(_gnc_txn(g(seed), iso_date, description, splits))

    def sp(seed, value, quantity, account_seed, action=None):
        return _gnc_split(g(seed), value, quantity, g(account_seed), action)

    # --- Opening balance (multi-year history: 2015) --------------------
    txn("txn-opening", "2015-04-01", "Opening balance", [
        sp("sp-open-cash", 25000.00, 25000.00, "Cash"),
        sp("sp-open-synth-buy", 100000.00, 1000, "SynthCorp", "Buy"),
        sp("sp-open-misc", 5000.00, 5000.00, "Misc Holding"),
        sp("sp-open-bank", 270000.50, 270000.50, "HDFC Bank"),
        sp("sp-open-capital", -400000.50, -400000.50, "Capital Account"),
    ])

    # --- FY 2024-25 P&L matching the HTML fixture exactly ---------------
    txn("txn-remuneration", "2024-06-15", "Partner remuneration", [
        sp("sp-remun-bank", 300000.00, 300000.00, "HDFC Bank"),
        sp("sp-remun-income", -300000.00, -300000.00, "Business Remuneration"),
    ])
    txn("txn-bank-interest", "2024-07-10", "Bank interest credited", [
        sp("sp-bi-bank", 20000.00, 20000.00, "HDFC Bank"),
        sp("sp-bi-income", -20000.00, -20000.00, "Bank Interest"),
    ])
    txn("txn-biz-expense", "2024-08-01", "Business expenses paid", [
        sp("sp-be-bank", -120000.00, -120000.00, "HDFC Bank"),
        sp("sp-be-expense", 120000.00, 120000.00, "Business Expenses"),
    ])
    txn("txn-tds-interest", "2024-07-10", "TDS on interest", [
        sp("sp-tdsi-bank", -5000.00, -5000.00, "HDFC Bank"),
        sp("sp-tdsi-expense", 5000.00, 5000.00, "TDS on Interest"),
    ])
    # B4: Salary + TDS on salary -- reconciles with the synthetic Form16
    # fixture's 17(1) and net tax payable (Book<->Form16 cross-check).
    txn("txn-salary", "2024-05-01", "Salary credited", [
        sp("sp-salary-bank", 500000.00, 500000.00, "HDFC Bank"),
        sp("sp-salary-income", -500000.00, -500000.00, "Salary"),
    ])
    txn("txn-tds-salary", "2024-05-01", "TDS on salary", [
        sp("sp-tdss-bank", -25000.00, -25000.00, "HDFC Bank"),
        sp("sp-tdss-expense", 25000.00, 25000.00, "TDS on Salary"),
    ])
    # B6 (RULE-1 golden): refund principal + refund interest booked to two
    # separate accounts from entry (D16/OQ-2) -- structurally enforces
    # RULE-1 (principal excluded from GTI, interest taxable).
    txn("txn-refund", "2024-09-01", "IT refund received (principal + interest)", [
        sp("sp-refund-bank", 1300.00, 1300.00, "HDFC Bank"),
        sp("sp-refund-principal", -1000.00, -1000.00, "IT Refund Principal"),
        sp("sp-refund-interest", -300.00, -300.00, "IT Refund Interest"),
    ])

    # --- CG lot #1: OldTech Ltd -- FIFO-consistent two-lot sale ----------
    txn("oldtech-buyA", "2016-06-01", "Bought OldTech lot A", [
        sp("oldtech-buyA-stock", 90000.00, 1000, "OldTech", "Buy"),
        sp("oldtech-buyA-cash", -90000.00, -90000.00, "Zerodha"),
    ])
    txn("oldtech-buyB", "2020-01-15", "Bought OldTech lot B", [
        sp("oldtech-buyB-stock", 200000.00, 1000, "OldTech", "Buy"),
        sp("oldtech-buyB-cash", -200000.00, -200000.00, "Zerodha"),
    ])
    txn("oldtech-sell", "2024-08-15", "Sold 2000 OldTech (2 lots)", [
        sp("oldtech-sell-proceeds", 280000.00, 280000.00, "Zerodha"),
        sp("oldtech-sell-lotA", -90000.00, -1000, "OldTech", "Sell"),
        sp("oldtech-sell-lotB", -200000.00, -1000, "OldTech", "Sell"),
        sp("oldtech-sell-ltcg", 10000.00, 10000.00, "LTCG", "LTCG"),
    ])

    # --- CG lot #2: QuickFlip Ltd -- intra-year STCG ---------------------
    txn("quickflip-buy", "2024-05-01", "Bought QuickFlip", [
        sp("quickflip-buy-stock", 50000.00, 500, "QuickFlip", "Buy"),
        sp("quickflip-buy-cash", -50000.00, -50000.00, "Zerodha"),
    ])
    txn("quickflip-sell", "2024-11-01", "Sold QuickFlip", [
        sp("quickflip-sell-proceeds", 60000.00, 60000.00, "Zerodha"),
        sp("quickflip-sell-stock", -50000.00, -500, "QuickFlip", "Sell"),
        sp("quickflip-sell-stcg", -10000.00, -10000.00, "STCG", "STCG"),
    ])

    # --- CG lot #3: AmbiguousCo -- two identical lots => unattributed ----
    txn("ambig-buyA", "2018-01-01", "Bought AmbiguousCo lot A", [
        sp("ambig-buyA-stock", 10000.00, 100, "AmbiguousCo", "Buy"),
        sp("ambig-buyA-cash", -10000.00, -10000.00, "Zerodha"),
    ])
    txn("ambig-buyB", "2019-01-01", "Bought AmbiguousCo lot B (identical)", [
        sp("ambig-buyB-stock", 10000.00, 100, "AmbiguousCo", "Buy"),
        sp("ambig-buyB-cash", -10000.00, -10000.00, "Zerodha"),
    ])
    txn("ambig-sell", "2024-09-01", "Sold AmbiguousCo", [
        sp("ambig-sell-proceeds", 15000.00, 15000.00, "Zerodha"),
        sp("ambig-sell-stock", -10000.00, -100, "AmbiguousCo", "Sell"),
        sp("ambig-sell-ltcg", -5000.00, -5000.00, "LTCG", "LTCG"),
    ])

    # --- CG lot #4: ViolatorCo -- engineered FIFO violation --------------
    txn("violator-buyA", "2015-01-01", "Bought ViolatorCo lot A (never sold)", [
        sp("violator-buyA-stock", 30000.00, 300, "ViolatorCo", "Buy"),
        sp("violator-buyA-cash", -30000.00, -30000.00, "Zerodha"),
    ])
    txn("violator-buyB", "2019-01-01", "Bought ViolatorCo lot B", [
        sp("violator-buyB-stock", 45000.00, 300, "ViolatorCo", "Buy"),
        sp("violator-buyB-cash", -45000.00, -45000.00, "Zerodha"),
    ])
    txn("violator-sell", "2024-09-15", "Sold ViolatorCo lot B out of FIFO order", [
        sp("violator-sell-proceeds", 60000.00, 60000.00, "Zerodha"),
        sp("violator-sell-stock", -45000.00, -300, "ViolatorCo", "Sell"),
        sp("violator-sell-ltcg", -15000.00, -15000.00, "LTCG", "LTCG"),
    ])

    # --- CG lot #5: StraddleCo -- Tier-3 multi-lot match straddling the
    # LT/ST 12-month boundary (B3 carry-forward patch): buyB (2022, > 12mo
    # before the sale => LT) and buyA (2024, < 12mo before the sale => ST)
    # are consumed together in one merged sell-split; the two rows must
    # come back out split by term, never merged. ---
    txn("straddle-buyB", "2022-01-01", "Bought StraddleCo lot B (old, will be LT)", [
        sp("straddle-buyB-stock", 20000.00, 200, "StraddleCo", "Buy"),
        sp("straddle-buyB-cash", -20000.00, -20000.00, "Zerodha"),
    ])
    txn("straddle-buyA", "2024-01-01", "Bought StraddleCo lot A (recent, will be ST)", [
        sp("straddle-buyA-stock", 10000.00, 100, "StraddleCo", "Buy"),
        sp("straddle-buyA-cash", -10000.00, -10000.00, "Zerodha"),
    ])
    txn("straddle-sell", "2024-09-05", "Sold 300 StraddleCo (2 lots, straddles LT/ST)", [
        sp("straddle-sell-proceeds", 45000.00, 45000.00, "Zerodha"),
        sp("straddle-sell-stock", -30000.00, -300, "StraddleCo", "Sell"),
        sp("straddle-sell-ltcg", -10000.00, -10000.00, "LTCG", "LTCG"),
        sp("straddle-sell-stcg", -5000.00, -5000.00, "STCG", "STCG"),
    ])

    # --- Quarterly dividends (>=3 buckets) + a 31-03 TDS gross-up entry --
    txn("div-q1", "2024-05-20", "Dividend received", [
        sp("div-q1-bank", 2000.00, 2000.00, "HDFC Bank"),
        sp("div-q1-income", -2000.00, -2000.00, "DividendShares"),
    ])
    txn("div-q2", "2024-08-10", "Dividend received", [
        sp("div-q2-bank", 3000.00, 3000.00, "HDFC Bank"),
        sp("div-q2-income", -3000.00, -3000.00, "DividendShares"),
    ])
    txn("div-q3", "2024-11-20", "Dividend received", [
        sp("div-q3-bank", 2500.00, 2500.00, "HDFC Bank"),
        sp("div-q3-income", -2500.00, -2500.00, "DividendShares"),
    ])
    txn("div-grossup", "2025-03-31", "Dividend with TDS gross-up", [
        sp("div-gu-income", -1000.00, -1000.00, "DividendShares"),
        sp("div-gu-tds", 100.00, 100.00, "TDSOnDividend"),
        sp("div-gu-bank", 900.00, 900.00, "HDFC Bank"),
    ])

    # --- Bank / NBFC / savings-bank interest -----------------------------
    txn("bankfd-interest", "2024-09-30", "Bank FD interest", [
        sp("bankfd-bank", 8000.00, 8000.00, "HDFC Bank"),
        sp("bankfd-income", -8000.00, -8000.00, "BankFDInterest"),
    ])
    txn("nbfc-interest", "2024-10-15", "NBFC interest", [
        sp("nbfc-bank", 6000.00, 6000.00, "HDFC Bank"),
        sp("nbfc-income", -6000.00, -6000.00, "NBFCInterest"),
    ])
    txn("savings-interest", "2024-06-30", "Savings bank interest", [
        sp("savings-bank", 500.00, 500.00, "HDFC Bank"),
        sp("savings-income", -500.00, -500.00, "SavingsInterest"),
    ])

    return _gnc_document(accounts, txns)


def build_syn_huf_gnucash() -> str:
    """Synthetic SYN-HUF book: reuses the HTML fixture's GUIDs (Interest
    Income / Long Term Capital Gain / TDS on Interest all sum to their HTML
    totals for FY 2024-25) plus a single lot sale behind the LTCG leaf so the
    lot-reconciliation invariant has real lot detail to check even for the
    simpler entity."""
    g = lambda s: _guid(f"SYN-HUF:{s}")  # noqa: E731

    root = g("Root")
    accounts = [_gnc_account(root, "Root Account", "ROOT", None)]

    def acc(guid, name, typ, parent, space="CURRENCY", cid="INR"):
        accounts.append(_gnc_account(guid, name, typ, parent, space, cid))

    acc(g("Assets"), "Assets", "ASSET", root)
    acc(g("Cash and Bank"), "Cash and Bank", "ASSET", g("Assets"))
    acc(g("Cash"), "Cash", "ASSET", g("Cash and Bank"))
    acc(g("Zerodha"), "Zerodha", "ASSET", g("Assets"))
    acc(g("HufScrip"), "HufScrip Ltd", "STOCK", g("Assets"), "NSE", "HUFSCRIP.NS")

    acc(g("Equity"), "Equity", "EQUITY", root)
    acc(g("Capital Account"), "Capital Account", "EQUITY", g("Equity"))

    acc(g("Income"), "Income", "INCOME", root)
    acc(g("Interest Income"), "Interest Income", "INCOME", g("Income"))
    acc(g("Long Term Capital Gain"), "Long Term Capital Gain", "INCOME", g("Income"))

    acc(g("Expense"), "Expense", "EXPENSE", root)
    acc(g("TDS on Interest"), "TDS on Interest", "EXPENSE", g("Expense"))

    txns = []

    def txn(seed, iso_date, description, splits):
        txns.append(_gnc_txn(g(seed), iso_date, description, splits))

    def sp(seed, value, quantity, account_seed, action=None):
        return _gnc_split(g(seed), value, quantity, g(account_seed), action)

    txn("txn-opening", "2015-04-01", "Opening balance", [
        sp("sp-open-cash", 10000.00, 10000.00, "Cash"),
        sp("sp-open-scrip-buy", 9000.00, 100, "HufScrip", "Buy"),
        sp("sp-open-capital", -19000.00, -19000.00, "Capital Account"),
    ])
    txn("txn-interest", "2024-08-01", "Interest income", [
        sp("sp-interest-cash", 2000.00, 2000.00, "Cash"),
        sp("sp-interest-income", -2000.00, -2000.00, "Interest Income"),
    ])
    txn("txn-tds", "2024-08-01", "TDS on interest", [
        sp("sp-tds-cash", -1500.00, -1500.00, "Cash"),
        sp("sp-tds-expense", 1500.00, 1500.00, "TDS on Interest"),
    ])
    txn("hufscrip-sell", "2024-06-01", "Sold HufScrip lot", [
        sp("hufscrip-sell-proceeds", 10000.00, 10000.00, "Zerodha"),
        sp("hufscrip-sell-stock", -9000.00, -100, "HufScrip", "Sell"),
        sp("hufscrip-sell-ltcg", -1000.00, -1000.00, "Long Term Capital Gain", "LTCG"),
    ])

    return _gnc_document(accounts, txns)


# ---------------------------------------------------------------------------
# Synthetic Form 16 PDF fixtures (Batch 4, plan section 6.2): TRACES-style
# Part B/Annexure-I text PDFs generated with reportlab. Numbers reconcile with
# the SYN-IND book/HTML's Salary (SALARY_GROSS) and TDS on Salary
# (TAXPAID_TDS_SALARY) leaves above, so the Book<->Form16 cross-checks pass.
# A few fields deliberately put the value on the line AFTER its label (with
# other lines in between) -- the real-world "label/value separation, jumbled
# reading order" hazard observed against the real corpus this batch.
# ---------------------------------------------------------------------------

SYN_IND_FORM16_TAN = "SYNE00001E"
SYN_IND_FORM16_PAN = "AAAAA0000A"        # matches Data/itr/entities.example.yaml SYN-IND
SYN_IND_FORM16_EMPLOYER_PAN = "AAAAA9999A"
SYN_IND_FORM16_CERT = "SYNCERT1"

SYN_IND_FORM16_EXTRA_TAN = "OLDE00002E"
SYN_IND_FORM16_EXTRA_CERT = "OLDCERT2"


def _syn_ind_form16_part_b_pages(*, broken_identity: bool = False) -> list[list[str]]:
    total_1d = "480000.00" if broken_identity else "500000.00"   # (a)+(b)+(c) != (d) when broken
    page1 = [
        "FORM NO. 16",
        "PART B",
        "Certificate under section 203 of the Income-tax Act, 1961",
        f"Certificate No. {SYN_IND_FORM16_CERT}",
        f"TAN of Employer:{SYN_IND_FORM16_TAN} PAN of Employee:{SYN_IND_FORM16_PAN} "
        f"Assessment Year:2025-26",
        "Name and address of the Employer/Specified Bank",
        "SYNTHETIC EMPLOYER PRIVATE LIMITED",
        "PAN of the Deductor TAN of the Deductor PAN of the Employee/Specified senior citizen",
        f"{SYN_IND_FORM16_EMPLOYER_PAN} {SYN_IND_FORM16_TAN} {SYN_IND_FORM16_PAN}",
        "CIT (TDS) Assessment Year Period with the Employer",
        "From To",
        "The Commissioner of Income Tax (TDS)",
        "Example Address Line, Example City - 000000",
        "2025-26",
        "01-Apr-2024 31-Mar-2025",
        "Annexure - I",
        "Details of Salary Paid and any other income and tax deducted",
        "A Whether opting out of taxation u/s 115BAC(1A)?",
        "Yes",
        "1. Gross Salary Rs.",
        "(a) Salary as per provisions contained in section 17(1)",
        "500000.00",
        "Value of perquisites under section 17(2) (as per Form No. 12BA)",
        "(b) 0.00",
        "Profits in lieu of salary under section 17(3)",
        "(c) 0.00",
        f"(d) Total {total_1d}",
        "(e) Reported total amount of salary received from other employer(s) 0.00",
        "2. Less: Allowances to the extent exempt under section 10",
        "(a) Travel concession or assistance under section 10(5) 0.00",
        "(b) Death-cum-retirement gratuity under section 10(10) 0.00",
        "(c) Commuted value of pension under section 10(10A) 0.00",
        "Cash equivalent of leave salary encashment under section 10",
        "(d) 0.00",
        "(10AA)",
        "(e) House rent allowance under section 10(13A) 0.00",
        "(f) Other special allowances under section 10(14) 0.00",
        "(g) Amount of any other exemption under section 10 0.00",
        "(h) Total amount of any other exemption under section 10 0.00",
        "(i) Total amount of exemption claimed under section 10 0.00",
        "3. Total amount of salary received from current employer [1(d)-2(i)]",
        "500000.00",
        "4. Less: Deductions under section 16",
        "(a) Standard deduction under section 16(ia) 50000.00",
        "(b) Entertainment allowance under section 16(ii) 0.00",
        "(c) Tax on employment under section 16(iii) 2500.00",
        "5. Total amount of deductions under section 16 [4(a)+4(b)+4(c)] 52500.00",
        '6. Income chargeable under the head "Salaries" [(3+1(e)-5]',
        "447500.00",
    ]
    page2 = [
        f"Certificate Number:{SYN_IND_FORM16_CERT} TAN of Employer:{SYN_IND_FORM16_TAN} "
        f"PAN of Employee:{SYN_IND_FORM16_PAN} Assessment Year:2025-26",
        "7. Add: Any other income reported by the employee under as per section 192 (2B)",
        "Income (or admissible loss) from house property reported by",
        "(a) 0.00",
        "employee offered for TDS",
        "(b) Income under the head Other Sources offered for TDS 0.00",
        "8. Total amount of other income reported by the employee",
        "[7(a)+7(b)] 0.00",
        "9. Gross total income (6+8)",
        "447500.00",
        "10. Deductions under Chapter VI-A Gross Amount Deductible Amount",
        "11. Aggregate of deductible amount under Chapter VI-A 0.00",
        "12. Total taxable income (9-11) 447500.00",
        "13. Tax on total income 25000.00",
        "14. Rebate under section 87A, if applicable 0.00",
        "15. Surcharge, wherever applicable 0.00",
        "16. Health and education cess 0.00",
        "17. Tax payable (13+15+16-14) 25000.00",
        "18. Less: Relief under section 89 (attach details) 0.00",
        "21. Net tax payable (17-18-19-20) 25000.00",
        "Verification",
        "I, SYNTHETIC AUTHORISED SIGNATORY, do hereby certify that the information",
        "given above is true, complete and correct.",
    ]
    return [page1, page2]


def _syn_ind_form16_extra_certificate_pages() -> list[list[str]]:
    """A second, unrelated certificate/TAN (a prior employer's Part A-only
    pages, no Part B/Annexure-I) -- exercises the two-certificate grouping +
    extra_certificates flagging in parse_form16.py."""
    page1 = [
        "FORM NO. 16",
        "PART A",
        f"Certificate No. {SYN_IND_FORM16_EXTRA_CERT}",
        f"TAN of Employer:{SYN_IND_FORM16_EXTRA_TAN} PAN of Employee:{SYN_IND_FORM16_PAN} "
        f"Assessment Year:2025-26",
        "Name and address of the Employer/Specified Bank",
        "PREVIOUS SYNTHETIC EMPLOYER LIMITED",
        "Summary of amount paid/credited and tax deducted at source",
        "Q1 100000.00 25000.00 25000.00",
    ]
    page2 = [
        f"TAN of Employer:{SYN_IND_FORM16_EXTRA_TAN} PAN of Employee:{SYN_IND_FORM16_PAN} "
        f"Assessment Year:2025-26",
        "II. DETAILS OF TAX DEDUCTED AND DEPOSITED IN THE CENTRAL GOVERNMENT ACCOUNT THROUGH CHALLAN",
        "Total (Rs.) 25000.00",
    ]
    return [page1, page2]


def _render_text_pdf(pages: list[list[str]], password: str | None = None) -> bytes:
    """Render `pages` (list of pages, each a list of text lines) to PDF bytes
    with reportlab -- a real text layer (not a raster image), so pdfplumber
    can extract it. `password` (when given) encrypts the PDF with reportlab's
    own StandardEncryption, matching the TRACES convention of password==PAN."""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    encrypt = StandardEncryption(password, canPrint=1, canModify=0) if password else None
    c = canvas.Canvas(buf, pagesize=A4, encrypt=encrypt)
    _, height = A4
    for page_lines in pages:
        c.setFont("Helvetica", 8)
        y = height - 50
        for line in page_lines:
            c.drawString(40, y, line)
            y -= 12
        c.showPage()
    c.save()
    return buf.getvalue()


def build_syn_ind_form16_pdf(
    *, encrypted: bool = False, two_certificates: bool = False, broken_identity: bool = False,
) -> bytes:
    """Synthetic SYN-IND Form 16 (TRACES Part B/Annexure-I). 17(1) and net
    tax payable reconcile with the SYN-IND book/HTML's Salary/TDS-on-Salary
    leaves. encrypted=True password-protects it with SYN_IND_FORM16_PAN
    (matching Data/itr/entities.example.yaml); two_certificates=True prepends
    an unrelated second certificate's pages; broken_identity=True makes
    1(d)'s printed total disagree with 17(1)+17(2)+17(3)."""
    pages = []
    if two_certificates:
        pages.extend(_syn_ind_form16_extra_certificate_pages())
    pages.extend(_syn_ind_form16_part_b_pages(broken_identity=broken_identity))
    password = SYN_IND_FORM16_PAN if encrypted else None
    return _render_text_pdf(pages, password=password)


if __name__ == "__main__":
    import gzip
    import pathlib
    out_dir = pathlib.Path(__file__).parent / "fixtures"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "syn_ind.html").write_text(build_syn_ind_html(), encoding="utf-8")
    (out_dir / "syn_huf.html").write_text(build_syn_huf_html(), encoding="utf-8")
    (out_dir / "syn_ind_truncated.html").write_text(build_syn_ind_html(truncated=True), encoding="utf-8")
    (out_dir / "syn_ind_imbalanced.html").write_text(build_syn_ind_html(nonzero_imbalance=True), encoding="utf-8")
    with gzip.open(out_dir / "syn_ind.gnucash", "wt", encoding="utf-8") as f:
        f.write(build_syn_ind_gnucash())
    with gzip.open(out_dir / "syn_huf.gnucash", "wt", encoding="utf-8") as f:
        f.write(build_syn_huf_gnucash())
    (out_dir / "syn_ind_form16.pdf").write_bytes(build_syn_ind_form16_pdf())
    (out_dir / "syn_ind_form16_encrypted.pdf").write_bytes(build_syn_ind_form16_pdf(encrypted=True))
    (out_dir / "syn_ind_form16_two_certs.pdf").write_bytes(build_syn_ind_form16_pdf(two_certificates=True))
    (out_dir / "syn_ind_form16_broken_identity.pdf").write_bytes(build_syn_ind_form16_pdf(broken_identity=True))
    print("Wrote fixtures to", out_dir)
