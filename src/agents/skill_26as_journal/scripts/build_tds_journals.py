"""
build_tds_journals.py — Form 26AS (Part I) -> GnuCash multi-split journal CSV.

Reads the Part I sub-totals from a Convert-tab 26AS workbook and the user's
GnuCash account tree, and emits one balanced journal transaction per deductor,
in three categories driven by the TDS section:

  A. Interest   (sections 194A, 193)
       Dr  Expense:TDS on Interest                  = Tax Deducted          (a)
       Dr  Income:Interest Income:Interest on FD    = Amount - Tax  (c - a)     [fixed generic]
       Cr  <matched interest income account>        = Amount Paid/Credited  (c)
  B. Dividend   (section 194)
       Dr  Expense:TDS on Dividend                  = Tax Deducted          (a)
       Cr  <matched dividend income account>        = Tax Deducted          (a)
  C. Partnership (section 194T)
       Dr  Expense:TDS on Partnership Payments      = Tax Deducted          (a)   [emitted as-is]
       Cr  <matched remuneration income account>    = Tax Deducted          (a)

Amounts come from the per-deductor sub-totals (header totals) in Part I.
Each transaction is dated 31-March of the current calendar year.

Account matching is DETERMINISTIC (token overlap + acronym + alias table +
single-candidate rule). Anything that cannot be matched with confidence is
routed to Liabilities:Suspense and flagged in the review for LLM/manual
resolution. No network or LLM calls happen here — this module is pure and
testable.

CSV dialect: GnuCash multi-split transaction import. One row per split. The
Date / Transaction ID / Description are REPEATED on every split row of a
transaction — GnuCash groups splits by matching transaction fields / shared
Transaction ID, and does NOT reliably attach blank-date continuation rows
(blank rows import as parse errors). A single signed "Amount" column holds the
split value using GnuCash's convention (Debit = positive, Credit = negative);
each transaction's Amounts sum to zero. Map it to the importer's "Amount"
column type (or "Amount (Negated)" if a build imports the signs reversed).
Transfer Amount / Transfer Account are NOT used: those exist only in two-split
mode (one row = one 2-split transaction), and the Interest journals have three
splits, so the file must be multi-split. Account is the full colon path WITHOUT
the "Root Account:" prefix. Import with the "Multi-split" box ticked, skipping
the 1 header line.

Usage:
  python build_tds_journals.py <26as.xlsx> <book.gnucash> <out.csv>
"""
from __future__ import annotations

import csv
import datetime as _dt
import gzip
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from openpyxl import load_workbook

# -------------------- fixed account names --------------------

ACC_TDS_INTEREST = "Expense:TDS on Interest"
ACC_TDS_DIVIDEND = "Expense:TDS on Dividend"
ACC_TDS_PARTNERSHIP = "Expense:TDS on Partnership Payments"  # may not exist; emitted as-is
ACC_INTEREST_ON_FD = "Income:Interest Income:Interest on FD"  # fixed generic (Category A debit b)
ACC_SUSPENSE = "Liabilities:Suspense"

CURRENCY = "INR"

# Section -> category.
SECTION_CATEGORY = {
    "194A": "A", "193": "A",
    "194": "B",
    "194T": "C",
}

# Alias expansion for matching: maps a token found in an ACCOUNT name to the
# set of tokens it stands for in a DEDUCTOR name (and vice-versa via expansion).
ALIASES = {
    "BOB": {"BANK", "BARODA"},
    "EPF": {"PROVIDENT", "FUND"},
    "PF": {"PROVIDENT", "FUND"},
    "DRL": {"REDDY", "REDDYS", "LABORATORIES"},
    "HDFC": {"HDFC"},
    "ICICI": {"ICICI"},
    "SBM": {"SBM"},
    "PNB": {"PUNJAB", "NATIONAL"},
    "LIC": {"LIFE", "INSURANCE"},
}

# Legal / generic noise tokens stripped from deductor names before matching.
STOPWORDS = {
    "LIMITED", "LTD", "LIMITE", "COMPANY", "CO", "PRIVATE", "PVT", "LLP",
    "INVESTMENT", "INVESTMENTS", "FINANCE", "AND", "THE", "OF", "OFFICE",
    "INDIA", "SERVICES", "SERVICE", "REGIONAL", "COMMISSIONER", "BANDRA",
    "EAST", "WEST", "NORTH", "SOUTH", "INC", "CORPORATION", "CORP",
}

# Account-name decoration stripped before matching (keeps the entity token).
ACC_NOISE = {
    "INTEREST", "ON", "FROM", "FD", "BOND", "DIVIDEND", "SHARES", "REMUNERATION",
    "TAXABLE", "BANK", "PARTNERSHIP", "MF", "INCOME",
}


# -------------------- data classes --------------------

@dataclass
class Deductor:
    sr: int
    name: str
    sections: tuple
    amount_paid: float
    tax_deducted: float
    tds_deposited: float


@dataclass
class Account:
    path: str          # full path WITHOUT "Root Account:" prefix
    leaf: str
    type: str


@dataclass
class Split:
    account: str
    debit: float = 0.0
    credit: float = 0.0


@dataclass
class Journal:
    sr: int
    deductor: str
    category: str
    section_label: str
    splits: list = field(default_factory=list)
    credit_account: str = ""
    credit_confidence: str = ""        # High / Medium / Low / Suspense
    credit_basis: str = ""
    candidates: list = field(default_factory=list)
    needs_review: bool = False

    @property
    def total_debit(self) -> float:
        return round(sum(s.debit for s in self.splits), 2)

    @property
    def total_credit(self) -> float:
        return round(sum(s.credit for s in self.splits), 2)

    @property
    def balanced(self) -> bool:
        return abs(self.total_debit - self.total_credit) < 0.005


# -------------------- 26AS xlsx parsing --------------------

def parse_part_i(xlsx_path: Path) -> tuple[list[Deductor], str]:
    """Return (deductors, financial_year) from a Convert-tab 26AS workbook."""
    wb = load_workbook(xlsx_path, data_only=True)
    if "Part I" not in wb.sheetnames:
        raise ValueError("Workbook has no 'Part I' sheet — is this a 26AS Convert output?")
    ws = wb["Part I"]

    # Financial year lives in the meta strip (row 2).
    fy = ""
    meta = ws.cell(2, 1).value or ""
    m = re.search(r"Financial Year:\s*(\d{4}-\d{2})", str(meta))
    if m:
        fy = m.group(1)

    from collections import OrderedDict
    hdr: "OrderedDict[int, list]" = OrderedDict()
    secs: dict[int, list] = {}
    for r in range(4, ws.max_row + 1):
        a = ws.cell(r, 1).value
        if not isinstance(a, int):
            continue
        sr = a
        if sr not in hdr:
            hdr[sr] = [ws.cell(r, 2).value, ws.cell(r, 4).value,
                       ws.cell(r, 5).value, ws.cell(r, 6).value]
            secs[sr] = []
        sec = ws.cell(r, 8).value
        if sec is not None and str(sec) not in secs[sr]:
            secs[sr].append(str(sec))

    deductors = []
    for sr, (name, amt, tax, tds) in hdr.items():
        deductors.append(Deductor(
            sr=sr, name=str(name).strip(), sections=tuple(secs[sr]),
            amount_paid=float(amt or 0), tax_deducted=float(tax or 0),
            tds_deposited=float(tds or 0),
        ))
    return deductors, fy


# -------------------- gnucash account parsing --------------------

def load_accounts(gnucash_path: Path) -> list[Account]:
    raw = gnucash_path.read_bytes()
    data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    root = ET.fromstring(data)
    ns = {"gnc": "http://www.gnucash.org/XML/gnc",
          "act": "http://www.gnucash.org/XML/act"}
    by_id = {}
    for a in root.iter("{http://www.gnucash.org/XML/gnc}account"):
        name = a.find("act:name", ns).text
        aid = a.find("act:id", ns).text
        typ = a.find("act:type", ns).text
        par = a.find("act:parent", ns)
        by_id[aid] = (name, par.text if par is not None else None, typ)

    def full_path(aid):
        parts = []
        cur = aid
        while cur and cur in by_id:
            n, p, _ = by_id[cur]
            parts.append(n)
            cur = p
        return list(reversed(parts))

    accounts = []
    for aid, (name, _p, typ) in by_id.items():
        parts = full_path(aid)
        # Drop the root account name ("Root Account") from the path.
        if parts and parts[0].lower().startswith("root"):
            parts = parts[1:]
        if not parts:
            continue
        accounts.append(Account(path=":".join(parts), leaf=parts[-1], type=typ))
    return accounts


def find_account(accounts: list[Account], path: str) -> Optional[Account]:
    for a in accounts:
        if a.path == path:
            return a
    return None


# -------------------- matching --------------------

def _tokens(s: str) -> list[str]:
    return [t for t in re.split(r"[^A-Za-z0-9]+", s.upper()) if t]


def _sig_tokens(s: str, drop: set) -> set:
    return {t for t in _tokens(s) if t not in drop and len(t) > 1}


def _acronym(tokens: list[str]) -> str:
    return "".join(t[0] for t in tokens if t)


def _candidates_for(category: str, accounts: list[Account]) -> list[Account]:
    """Income accounts in the relevant subtree (leaves only — has a parent path)."""
    out = []
    for a in accounts:
        if a.type != "INCOME":
            continue
        p = a.path
        if category == "A" and "Interest Income" in p:
            # Never offer the fixed generic debit account as a credit candidate,
            # or a deductor could land a debit and credit on the same account.
            if a.path == ACC_INTEREST_ON_FD:
                continue
            out.append(a)
        elif category == "B" and "Dividend" in p:
            out.append(a)
        elif category == "C" and "Remuneration" in p:
            out.append(a)
    # Keep only leaf-ish accounts (exclude the bare parent like "Income:Dividend - Shares"
    # when it has children) — but harmless to keep; scoring handles it.
    return out


def match_credit_account(deductor: str, category: str,
                         accounts: list[Account]) -> tuple[Optional[str], str, str, list]:
    """Return (account_path|None, confidence, basis, candidate_paths)."""
    cands = _candidates_for(category, accounts)
    cand_paths = [c.path for c in cands]
    if not cands:
        return None, "Suspense", "no income accounts in category subtree", []

    # Single-candidate rule (e.g. Category C has only 'Remuneration from Partnership').
    if len(cands) == 1:
        return cands[0].path, "Medium", "only candidate in category subtree", cand_paths

    d_tokens_list = [t for t in _tokens(deductor) if t not in STOPWORDS and len(t) > 1]
    d_tokens = set(d_tokens_list)
    d_acro = _acronym(d_tokens_list)

    best = None
    best_score = 0.0
    best_basis = ""
    for c in cands:
        c_tokens = _sig_tokens(c.leaf, ACC_NOISE)
        score = 0.0
        hits = []
        # 1) direct shared significant tokens
        shared = d_tokens & c_tokens
        if shared:
            score += 2.0 * len(shared)
            hits.append("token:" + "/".join(sorted(shared)))
        # 2) prefix / substring (CHOLA in CHOLAMANDALAM)
        for ct in c_tokens:
            for dt in d_tokens:
                if ct == dt:
                    continue
                if len(ct) >= 3 and (dt.startswith(ct) or ct.startswith(dt)):
                    score += 1.5
                    hits.append(f"prefix:{ct}~{dt}")
        # 3) acronym: account token equals deductor acronym (BOB, DRL)
        for ct in c_tokens:
            if len(ct) >= 2 and ct == d_acro:
                score += 2.5
                hits.append(f"acronym:{ct}")
        # 4) alias table
        for ct in c_tokens:
            alias = ALIASES.get(ct)
            if alias and (alias & d_tokens):
                score += 2.0
                hits.append(f"alias:{ct}")
        if score > best_score:
            best_score = score
            best = c
            best_basis = ", ".join(hits)

    if best is None or best_score < 1.5:
        return None, "Suspense", "no confident match", cand_paths
    conf = "High" if best_score >= 2.0 else "Medium"
    return best.path, conf, best_basis, cand_paths


# -------------------- journal construction --------------------

def categorize(sections: tuple) -> tuple[Optional[str], str]:
    """Return (category, label). label is the section string for the description."""
    cats = {SECTION_CATEGORY.get(s) for s in sections}
    cats.discard(None)
    label = "/".join(sections)
    if len(cats) == 1:
        return cats.pop(), label
    return None, label  # unknown or mixed -> needs review


def build_journals(deductors: list[Deductor], accounts: list[Account],
                   overrides: Optional[dict] = None) -> list[Journal]:
    """overrides: {deductor_sr (int) -> credit account full path}. Used by the
    LLM-fallback path to resolve deductors the deterministic matcher sent to
    Suspense. An override always wins over the deterministic choice."""
    overrides = overrides or {}
    journals = []
    for d in deductors:
        cat, label = categorize(d.sections)
        j = Journal(sr=d.sr, deductor=d.name, category=cat or "?", section_label=label)
        a = round(d.tax_deducted, 2)
        c = round(d.amount_paid, 2)

        if cat is None:
            # Unknown/mixed section — park the tax on Suspense, flag.
            j.splits = [Split(ACC_SUSPENSE, debit=a), Split(ACC_SUSPENSE, credit=a)]
            j.credit_account = ACC_SUSPENSE
            j.credit_confidence = "Suspense"
            j.credit_basis = f"unhandled section(s): {label}"
            j.needs_review = True
            journals.append(j)
            continue

        acct, conf, basis, cands = match_credit_account(d.name, cat, accounts)
        j.candidates = cands
        if d.sr in overrides and overrides[d.sr]:
            credit_acc = overrides[d.sr]
            j.credit_account = credit_acc
            j.credit_confidence = "Override"
            j.credit_basis = "resolved by override/LLM"
            j.needs_review = False
        else:
            credit_acc = acct or ACC_SUSPENSE
            j.credit_account = credit_acc
            j.credit_confidence = conf
            j.credit_basis = basis
            j.needs_review = acct is None or conf in ("Low", "Suspense")

        if cat == "A":
            j.splits = [
                Split(ACC_TDS_INTEREST, debit=a),
                Split(ACC_INTEREST_ON_FD, debit=round(c - a, 2)),
                Split(credit_acc, credit=c),
            ]
        elif cat == "B":
            j.splits = [
                Split(ACC_TDS_DIVIDEND, debit=a),
                Split(credit_acc, credit=a),
            ]
        elif cat == "C":
            j.splits = [
                Split(ACC_TDS_PARTNERSHIP, debit=a),
                Split(credit_acc, credit=a),
            ]
        journals.append(j)
    return journals


# -------------------- output --------------------

def journal_date() -> str:
    """31-March of the current calendar year, ISO format."""
    return _dt.date(_dt.date.today().year, 3, 31).isoformat()


def fy_prefix(fy: str) -> str:
    """Compact financial-year prefix for Transaction IDs: '2025-26' -> '2526'.

    Keeps Transaction IDs unique across years (next year's TDSJ01 would
    otherwise collide). Falls back to the journal date's FY when fy is blank
    (31-March of year Y closes FY (Y-1)-(Y))."""
    m = re.match(r"\s*(\d{4})-(\d{2})\s*$", fy or "")
    if m:
        return m.group(1)[2:] + m.group(2)
    y = _dt.date.today().year
    return f"{(y - 1) % 100:02d}{y % 100:02d}"


def _fmt(x: float) -> str:
    return f"{x:.2f}" if x else ""


def write_csv(journals: list[Journal], out_path: Path, fy: str) -> None:
    date = journal_date()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        # GnuCash-native multi-split layout. One signed "Amount" column per
        # split (maps to the importer's "Amount" column type). GnuCash's sign
        # convention: Debit = positive, Credit = negative; per transaction the
        # Amounts sum to zero. (If a given build of GnuCash imports the signs
        # reversed, map this column to "Amount (Negated)" instead.)
        w.writerow(["Date", "Transaction ID", "Number", "Description", "Account",
                    "Amount", "Currency"])
        fy_pfx = fy_prefix(fy)
        for j in journals:
            txn_id = f"{fy_pfx}-TDSJ{j.sr:02d}"
            desc = f"TDS FY {fy} - {j.deductor} (Sec {j.section_label})" if fy \
                else f"TDS - {j.deductor} (Sec {j.section_label})"
            # Repeat Date / Transaction ID / Description on EVERY split row.
            # GnuCash's multi-split importer groups splits by matching
            # transaction fields (and the Transaction ID), NOT by blank-date
            # continuation rows — blank rows show up as parse errors.
            for s in j.splits:
                signed = round(s.debit - s.credit, 2)   # Dr +, Cr -
                # Number duplicates Transaction ID -> GnuCash visible Num field.
                w.writerow([date, txn_id, txn_id, desc, s.account,
                            f"{signed:.2f}", CURRENCY])


def write_review(journals: list[Journal], path: Path, accounts: list[Account]) -> None:
    existing = {a.path for a in accounts}
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Sr", "Deductor", "Section", "Category", "Credit Account",
                    "Confidence", "Account Exists", "Balanced", "Debit", "Credit",
                    "Needs Review", "Basis"])
        for j in journals:
            w.writerow([
                j.sr, j.deductor, j.section_label, j.category, j.credit_account,
                j.credit_confidence, "yes" if j.credit_account in existing else "NO",
                "yes" if j.balanced else "NO", f"{j.total_debit:.2f}",
                f"{j.total_credit:.2f}", "yes" if j.needs_review else "",
                j.credit_basis,
            ])


# -------------------- orchestration --------------------

def run(xlsx_path: Path, gnucash_path: Path, out_path: Path,
        overrides: Optional[dict] = None) -> dict:
    deductors, fy = parse_part_i(xlsx_path)
    accounts = load_accounts(gnucash_path)
    journals = build_journals(deductors, accounts, overrides)
    write_csv(journals, out_path, fy)

    review_path = out_path.with_name(out_path.stem + "-review.csv")
    write_review(journals, review_path, accounts)

    existing = {a.path for a in accounts}

    # Any split account (debit or credit) not present in the book must be
    # created by the user before import (e.g. 'Expense:TDS on Partnership
    # Payments'). Surface them explicitly so nothing imports silently wrong.
    missing = sorted({s.account for j in journals for s in j.splits
                      if s.account not in existing})

    review_rows = []
    for j in journals:
        review_rows.append({
            "sr": j.sr, "deductor": j.deductor, "section": j.section_label,
            "category": j.category, "credit_account": j.credit_account,
            "confidence": j.credit_confidence,
            "account_exists": j.credit_account in existing,
            "balanced": j.balanced, "needs_review": j.needs_review,
            "candidates": j.candidates, "basis": j.credit_basis,
        })

    return {
        "fy": fy,
        "journal_date": journal_date(),
        "deductors": len(deductors),
        "balanced_all": all(j.balanced for j in journals),
        "needs_review": [r for r in review_rows if r["needs_review"]],
        "missing_accounts": missing,
        "rows": review_rows,
        "output": str(out_path),
        "review": str(review_path),
    }


def main(argv: list[str]) -> int:
    if len(argv) not in (4, 5):
        print("Usage: python build_tds_journals.py <26as.xlsx> <book.gnucash> "
              "<out.csv> [overrides.json]", file=sys.stderr)
        return 2
    overrides = None
    if len(argv) == 5:
        import json
        raw = json.loads(Path(argv[4]).read_text(encoding="utf-8"))
        overrides = {int(k): v for k, v in raw.items()}
    stats = run(Path(argv[1]), Path(argv[2]), Path(argv[3]), overrides)
    print(f"FY {stats['fy']}  date {stats['journal_date']}  "
          f"deductors {stats['deductors']}  balanced_all {stats['balanced_all']}")
    for r in stats["rows"]:
        flag = "  <-- REVIEW" if r["needs_review"] else ""
        print(f"  Sr{r['sr']:>2} [{r['category']}/{r['section']:<5}] {r['deductor'][:34]:34} "
              f"-> {r['credit_account']}  ({r['confidence']}){flag}")
        if r["needs_review"] and r["candidates"]:
            print(f"        candidates: {', '.join(r['candidates'])}")
    if stats["needs_review"]:
        print(f"\n{len(stats['needs_review'])} deductor(s) need review (Suspense/low confidence).")
    if stats["missing_accounts"]:
        print("\nAccounts to CREATE in GnuCash before import:")
        for acc in stats["missing_accounts"]:
            print(f"  - {acc}")
    print(f"Saved: {stats['output']}")
    print(f"Review: {stats['review']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
