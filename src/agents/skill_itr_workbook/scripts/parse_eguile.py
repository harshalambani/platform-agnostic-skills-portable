"""
parse_eguile.py -- GnuCash "Balance Sheet (eguile)" HTML export -> account tree.

Per the plan (2026-07-12-itr-workbook-skill-plan.md, section 1.1):
  - bs4 tolerant parsing (the report is not well-formed HTML: unclosed tags).
    Never regex-over-string.
  - Per account row: GUID from the gnc-register:acct-guid=<32hex> anchor,
    name, indent-depth, section, balance.
  - Numbers: "Rs" (Unicode Rupee sign) + non-breaking spaces, Indian lakh
    grouping, negatives inside span.negative with a leading minus.
  - Security cells: "<qty>. <SYMBOL> Rs <value>" triple.
  - Subtotal rows ("Total <name>") captured separately from leaf rows.
  - The trailing Exchange Rates table (rational-fraction prices) is never
    parsed -- it lives outside table.accounts and is simply never visited.
  - Missing "Imbalance Amount" row => hard fail ("file truncated -- re-export").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, Tag

TRUNCATED_MESSAGE = "file truncated — re-export"

_GUID_RE = re.compile(r"acct-guid=([0-9a-fA-F]{32})")
_QTY_SYMBOL_RE = re.compile(r"^([\d,]+(?:\.\d+)?\.?)\s+(.+)$")

# Section markers that open/close without an account GUID.
_SECTION_OPENERS = {
    "Assets Accounts": "Assets",
    "Liability Accounts": "Liability",
    "Trading Accounts": "Trading",
    "Equity Accounts": "Equity",
    "Retained Earnings": "RetainedEarnings",
}


@dataclass
class AccountNode:
    """One row of the account tree (leaf, branch header, or closed subtotal)."""
    guid: str | None
    name: str
    depth: int
    section: str
    path: str
    balance: float | None = None   # value shown directly on this row, if any
    total: float | None = None     # closing total (== balance for leaves)
    qty: float | None = None
    symbol: str | None = None
    children: list["AccountNode"] = field(default_factory=list)

    def child_sum(self) -> float:
        return sum(c.total for c in self.children if c.total is not None)

    def to_dict(self) -> dict:
        return {
            "guid": self.guid,
            "name": self.name,
            "depth": self.depth,
            "section": self.section,
            "path": self.path,
            "balance": self.balance,
            "total": self.total,
            "qty": self.qty,
            "symbol": self.symbol,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class ParsedBalanceSheet:
    section_roots: dict[str, list[AccountNode]]
    section_totals: dict[str, float]
    imbalance: float

    @property
    def income_total(self) -> float | None:
        for node in self.section_roots.get("RetainedEarnings", []):
            if node.name == "Income":
                return node.total
        return None

    @property
    def expense_total(self) -> float | None:
        for node in self.section_roots.get("RetainedEarnings", []):
            if node.name == "Expense":
                return node.total
        return None

    def all_nodes(self):
        """Yield every node in the tree, depth-first."""
        def _walk(nodes):
            for n in nodes:
                yield n
                yield from _walk(n.children)
        for roots in self.section_roots.values():
            yield from _walk(roots)

    def to_dict(self) -> dict:
        return {
            "section_totals": self.section_totals,
            "imbalance": self.imbalance,
            "sections": {
                sect: [n.to_dict() for n in roots]
                for sect, roots in self.section_roots.items()
            },
        }


def parse_amount(raw: str | None) -> float | None:
    """'Rs  1,58,156.35' / '-4,99,919.22' / '&nbsp;' -> float or None."""
    if raw is None:
        return None
    text = raw.replace("\xa0", " ").replace("₹", " ").strip()
    if not text:
        return None
    negative = False
    if text.startswith("-"):
        negative = True
        text = text[1:].strip()
    text = text.replace(",", "").replace(" ", "")
    if not text:
        return None
    value = float(text)
    return -value if negative else value


def _split_qty_symbol(text: str) -> tuple[float, str]:
    """'2,400. 531225.BS' -> (2400.0, '531225.BS'); '9.43509528 BABA' -> (9.43509528, 'BABA')."""
    m = _QTY_SYMBOL_RE.match(text)
    if not m:
        raise ValueError(f"could not split quantity/symbol from {text!r}")
    qty_text = m.group(1).rstrip(".").replace(",", "")
    return float(qty_text), m.group(2).strip()


def _cell_class(td: Tag) -> str | None:
    classes = td.get("class") or []
    return classes[0] if classes else None


def _find_name_td(tr: Tag) -> Tag | None:
    for td in tr.find_all("td"):
        cls = _cell_class(td)
        if cls in ("accname", "accnametotal"):
            return td
    return None


def _find_balance_td(tr: Tag) -> Tag | None:
    for td in tr.find_all("td"):
        if _cell_class(td) in ("balance", "balancetotal", "ruledtotal"):
            return td
    return None


def _extract_value(balance_td: Tag | None) -> tuple[float | None, float | None, str | None]:
    """Returns (value, qty, symbol). qty/symbol are None for non-security cells."""
    if balance_td is None:
        return None, None, None
    foreign = balance_td.find("span", class_="foreign")
    if foreign is not None:
        qty_symbol_text = foreign.get_text().replace("\xa0", " ").strip()
        qty, symbol = _split_qty_symbol(qty_symbol_text)
        full_text = balance_td.get_text().replace("\xa0", " ")
        rupee_pos = full_text.find("₹")
        value = parse_amount(full_text[rupee_pos:]) if rupee_pos != -1 else None
        return value, qty, symbol
    return parse_amount(balance_td.get_text()), None, None


def parse_html(html_text: str) -> ParsedBalanceSheet:
    """Parse the eguile Balance Sheet HTML text into a ParsedBalanceSheet."""
    soup = BeautifulSoup(html_text, "html.parser")
    table = soup.find("table", class_="accounts")
    if table is None:
        raise ValueError(TRUNCATED_MESSAGE)

    section_roots: dict[str, list[AccountNode]] = {}
    section_totals: dict[str, float] = {}
    imbalance: float | None = None

    current_section: str | None = None      # Assets / Liability / Trading / Equity / RetainedEarnings
    current_leaf_section: str | None = None  # what gets stamped onto AccountNode.section
    stack: list[AccountNode] = []           # open ancestor chain, indexed by depth
    roots: list[AccountNode] = []           # depth-0 nodes of the current section

    for tr in table.find_all("tr"):
        name_td = _find_name_td(tr)
        if name_td is None:
            continue  # spacer row

        depth = len(tr.find_all("td", class_="indent"))
        anchor = name_td.find("a")
        text = name_td.get_text(" ", strip=True)
        is_total_text = text.startswith("Total ")
        balance_td = _find_balance_td(tr)

        if anchor is None:
            # Section marker row: opener, closer, or the terminal Imbalance row.
            if text == "Imbalance Amount":
                value, _, _ = _extract_value(balance_td)
                imbalance = value if value is not None else 0.0
                break
            if not is_total_text and text in _SECTION_OPENERS:
                current_section = _SECTION_OPENERS[text]
                current_leaf_section = current_section
                stack = []
                roots = []
                section_roots.setdefault(current_section, roots)
                continue
            if is_total_text:
                value, _, _ = _extract_value(balance_td)
                value = value if value is not None else 0.0
                key = text[len("Total "):]
                section_totals[key] = value
                # A section has exactly one root account (e.g. "Assets" under
                # "Assets Accounts"); the report never prints a redundant
                # "Total <a>Assets</a>" row for it, so the section-closer IS
                # that root's total whenever it hasn't been closed already.
                for root in roots:
                    if root.total is None:
                        root.total = value
                continue
            # Unrecognized non-anchor marker row; ignore.
            continue

        guid_match = _GUID_RE.search(anchor.get("href", ""))
        guid = guid_match.group(1) if guid_match else None
        name = anchor.get_text(strip=True)
        value, qty, symbol = _extract_value(balance_td)

        if is_total_text:
            # Closes the node opened earlier at this same depth/guid.
            if depth < len(stack) and stack[depth].guid == guid:
                stack[depth].total = value if value is not None else 0.0
            stack = stack[:depth]
            continue

        parent_path = stack[depth - 1].path if depth > 0 and depth - 1 < len(stack) else ""
        path = f"{parent_path}/{name}" if parent_path else name

        if depth == 0 and current_section == "RetainedEarnings":
            if name == "Income":
                current_leaf_section = "RetainedEarnings-Income"
            elif name == "Expense":
                current_leaf_section = "RetainedEarnings-Expense"

        node = AccountNode(
            guid=guid, name=name, depth=depth, section=current_leaf_section or "Unknown",
            path=path, balance=value, total=value, qty=qty, symbol=symbol,
        )

        if depth == 0:
            roots.append(node)
        elif depth - 1 < len(stack):
            stack[depth - 1].children.append(node)
        stack = stack[:depth] + [node]

    if imbalance is None:
        raise ValueError(TRUNCATED_MESSAGE)

    return ParsedBalanceSheet(
        section_roots=section_roots,
        section_totals=section_totals,
        imbalance=imbalance,
    )


def parse_file(path: str | Path) -> ParsedBalanceSheet:
    html_text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_html(html_text)


# ---------------------------------------------------------------------------
# Verification -- the three identity checks from plan section 1.1.
# ---------------------------------------------------------------------------

def verify(tree: ParsedBalanceSheet, tolerance: float = 0.01) -> list[str]:
    """Run the built-in identity checks; return a list of failure messages
    (empty list == all green)."""
    failures: list[str] = []

    if abs(tree.imbalance) > tolerance:
        failures.append(f"Imbalance Amount is {tree.imbalance:.2f}, expected 0.00")

    total_assets = tree.section_totals.get("Assets Accounts")
    etl = tree.section_totals.get("Equity, Trading, and Liabilities")
    total_re = tree.section_totals.get("Retained Earnings")
    if total_assets is None or etl is None or total_re is None:
        failures.append("Missing one of the top-level section totals "
                         "(Assets Accounts / Equity, Trading, and Liabilities / Retained Earnings)")
    elif abs(total_assets - (etl + total_re)) > tolerance:
        failures.append(
            f"Total Assets ({total_assets:.2f}) != Equity+Trading+Liabilities "
            f"({etl:.2f}) + Retained Earnings ({total_re:.2f}) "
            f"[diff {total_assets - (etl + total_re):.2f}]"
        )

    for node in tree.all_nodes():
        if not node.children:
            continue
        expected = node.child_sum()
        actual = node.total if node.total is not None else 0.0
        if abs(actual - expected) > tolerance:
            failures.append(
                f"Total {node.name} ({actual:.2f}) != sum of children ({expected:.2f}) "
                f"at {node.path}"
            )

    return failures


def assert_valid(tree: ParsedBalanceSheet) -> None:
    failures = verify(tree)
    if failures:
        raise ValueError("Validation failed:\n" + "\n".join(failures))
