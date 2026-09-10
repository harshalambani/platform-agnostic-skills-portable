"""
jv_emitter.py -- Stage 1b: GnuCash multi-split journal CSV emitter for the
Partner Compensation Reconciliation skill.

The reconciled year this skill produces always implies a set of journal
entries -- one per monthly payout, plus an optional prior-period opening
reclassification. This module makes that implication explicit, tested, and
importable, instead of leaving it as an exercise for whoever reads the
workbook.

CSV dialect -- MANDATORY, matches
src/agents/skill_26as_journal/scripts/build_tds_journals.py exactly (that
module's docstring + JOURNAL_HEADERS are the ground truth this restates):

  a. Columns, exactly, in this order: Date, Transaction ID, Number,
     Description, Account, Amount, Currency.
  b. One row per split. Date / Transaction ID / Number / Description are
     REPEATED on every split row of a transaction -- GnuCash's multi-split
     importer groups splits by matching transaction fields plus the
     Transaction ID, and does NOT reliably attach blank-date continuation
     rows (a blank row imports as a parse error). This is the specific
     defect this stage exists to make impossible.
  c. A single signed Amount column holds the split value using GnuCash's
     convention: Debit is positive, Credit is negative. Each transaction's
     Amounts sum to exactly zero. Deposit/Withdrawal column pairs are never
     emitted.
  d. Transfer Amount / Transfer Account are two-split-only columns and are
     never emitted here -- every transaction in this file has three or more
     splits.
  e. Account is the full colon-separated path WITHOUT the "Root Account:"
     prefix.
  f. Currency is the constant "INR" on every row.
  g. Date is ISO YYYY-MM-DD.
  h. Transaction ID is unique per transaction AND unique across financial
     years -- an FY-prefixed series, itself prefixed with a short firm
     token derived from report.firm_name (its first whitespace-separated
     word, stripped to alphanumerics and upper-cased), e.g. "KPMG-2526-M01"
     for the first monthly payout of FY 2025-26 with firm_name "KPMG India
     Services LLP", "KPMG-2526-RECT" for that year's opening
     reclassification. When firm_name is empty or the token would come out
     empty, the firm prefix is omitted entirely (bare "2526-M01" / no
     leading hyphen) rather than crashing or emitting a malformed ID.
     Number duplicates Transaction ID, landing in GnuCash's visible Num
     field.
  i. No Notes/Memo columns are emitted.

Import settings (see AGENT.md's "Importing into GnuCash" section): tick
Multi-split, skip 1 header line, map Date as ISO, map the single Amount
column to the importer's "Amount" column type (or "Amount (Negated)" if a
build reverses the sign convention).

Architecture: this is the ONLY module in this package that emits CSV.
build_journals() is pure (no I/O, no openpyxl -- does not import writer.py).
write_journal_csv() is the only function that touches the filesystem.
"""
from __future__ import annotations

import calendar
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

CURRENCY = "INR"

# Column order for the GnuCash multi-split journal CSV -- see dialect point
# (a) above. Matches build_tds_journals.py's JOURNAL_HEADERS exactly.
JOURNAL_HEADERS = ["Date", "Transaction ID", "Number", "Description", "Account",
                   "Amount", "Currency"]

# A split amount smaller than this is treated as zero and the split is
# omitted rather than emitted as a "0.00" row (spec 2.4: "Omit any split
# whose amount rounds to 0.00 -- do not emit zero rows"). Half a paisa, same
# tolerance the tests use for the zero-sum check.
_ZERO_TOLERANCE = 0.005

# The accounts.* keys this emitter understands, each required only if the
# corresponding amount is non-zero somewhere in the year (spec 2.5).
#
# Interest on capital and remuneration are both PGBP income under
# s.28(v), NOT Income from Other Sources -- do not "correct" the account
# placement implied by these keys' names to an Other Sources bucket.
#
# tds_expense is an EXPENSE account (a debit of s.194T tax deducted at
# source), NOT an asset/tax-credit account -- this ledger's convention
# books every deducted-tax leg as an expense, consistently. Do not
# "correct" the account placement implied by this key's name to an Assets
# tax-credit bucket.
ACCOUNT_KEYS = (
    "bank", "tds_expense", "interest_on_capital", "current_account",
    "capital_contribution", "medical_expense", "remuneration_income",
    "share_of_profit_income",
)


class JournalValidationError(ValueError):
    """Raised by build_journals() for a user-fixable input problem: a
    required accounts.* key missing/empty for a non-zero split, an
    unbalanced transaction, or a malformed opening_reclass block. Caught by
    agent.run() and turned into an "ERROR: ..." string -- never a
    traceback."""


@dataclass
class Split:
    account: str
    debit: float = 0.0
    credit: float = 0.0


@dataclass
class Journal:
    txn_id: str
    date: str
    description: str
    splits: list = field(default_factory=list)

    @property
    def total_debit(self) -> float:
        return round(sum(s.debit for s in self.splits), 2)

    @property
    def total_credit(self) -> float:
        return round(sum(s.credit for s in self.splits), 2)

    @property
    def balanced(self) -> bool:
        return abs(self.total_debit - self.total_credit) < _ZERO_TOLERANCE


def fy_prefix(fy: str) -> str:
    """Compact financial-year prefix for Transaction IDs: '2025-26' ->
    '2526'. Mirrors build_tds_journals.py's fy_prefix() so both skills'
    journal CSVs use the same cross-year-unique ID shape."""
    m = re.match(r"\s*(\d{4})-(\d{2})\s*$", fy or "")
    if m:
        return m.group(1)[2:] + m.group(2)
    return re.sub(r"[^0-9A-Za-z]", "", fy or "FY")


def _firm_token(firm_name: str) -> str:
    """Short firm-scoped Transaction ID prefix derived from firm_name, e.g.
    "KPMG India Services LLP" -> "KPMG": the first whitespace-separated
    word, stripped to alphanumerics and upper-cased. Returns "" when
    firm_name is empty or the token would come out empty, so the caller
    falls back to today's bare ID shape rather than crashing or emitting a
    leading hyphen."""
    words = (firm_name or "").split()
    if not words:
        return ""
    return re.sub(r"[^0-9A-Za-z]", "", words[0]).upper()


def _txn_id(fy_pfx: str, firm_name: str, suffix: str) -> str:
    """Build a Transaction ID as '<firm_token>-<fy_pfx>-<suffix>' when a
    firm token is available (dialect point h), else the bare
    '<fy_pfx>-<suffix>' with no leading hyphen."""
    token = _firm_token(firm_name)
    if token:
        return f"{token}-{fy_pfx}-{suffix}"
    return f"{fy_pfx}-{suffix}"


def _strip_root(account: str) -> str:
    """Account is the full colon path WITHOUT the "Root Account:" prefix
    (dialect point e). Strip it if supplied, rather than reject, since
    GnuCash's own account-tree UI often shows the full path including the
    root -- this is a common copy/paste shape for an accounts.* value."""
    account = account.strip()
    if account.startswith("Root Account:"):
        account = account[len("Root Account:"):].lstrip(":").strip()
    return account


def _account_for(accounts: dict, key: str, ctx: str) -> str:
    value = accounts.get(key)
    if not isinstance(value, str) or not value.strip():
        raise JournalValidationError(
            f"accounts.{key} is required for {ctx} but was not supplied "
            "(or is empty) in the input's accounts: block."
        )
    return _strip_root(value)


def _month_end(month: str) -> str:
    """'YYYY-MM' -> ISO date of that month's last day (dialect point g)."""
    year_s, month_s = month.split("-")
    year, mon = int(year_s), int(month_s)
    last_day = calendar.monthrange(year, mon)[1]
    return f"{year:04d}-{mon:02d}-{last_day:02d}"


def _add_leg(splits: list, accounts: dict, key: str, ctx: str, signed_amount: float) -> None:
    """Append a Split for one leg of a transaction, Dr positive / Cr
    negative (dialect point c). Omits the split entirely if the amount
    rounds to zero -- the accounts.* key is then not required either, since
    a key is only required "if the corresponding amount is non-zero
    somewhere in the year" (spec 2.5)."""
    signed_amount = round(float(signed_amount or 0.0), 2)
    if abs(signed_amount) < _ZERO_TOLERANCE:
        return
    account = _account_for(accounts, key, ctx)
    if signed_amount >= 0:
        splits.append(Split(account=account, debit=signed_amount))
    else:
        splits.append(Split(account=account, credit=-signed_amount))


def _check_balanced(journal: "Journal") -> None:
    if not journal.balanced:
        diff = journal.total_debit - journal.total_credit
        raise JournalValidationError(
            f"Journal '{journal.txn_id}' ({journal.description}) does not "
            f"balance: debit {journal.total_debit:.2f} vs credit "
            f"{journal.total_credit:.2f} (difference {diff:.2f}). Check the "
            "input figures for that transaction."
        )


def _monthly_journal(line, accounts: dict, fy_pfx: str, firm_name: str, idx: int) -> Journal:
    """Build the one transaction implied by a single monthly payout line
    (spec 2.4):

        Dr  bank                     = total_paid
        Dr  tds_expense              = -tds                     (tds is negative)
        Dr  capital_contribution     = -capital_transferred     (negative)
        Dr  medical_expense          = -medical_topup           (negative)
        Cr  remuneration_income      = -remuneration
        Cr  share_of_profit_income   = -(share_of_profit_gross + firms_tax_sop
                                          + additional_share_of_profit)
        Cr  interest_on_capital      = -interest_on_capital
        Cr  current_account          = -prior_cohort_drawdown

    share_of_profit_income is booked as ONE folded leg: the current year's
    own gross share of profit, netted by firms_tax_sop (the firm's tax on
    THAT share of profit), PLUS additional_share_of_profit in the same
    leg -- never as two separate share_of_profit_income postings. Firm's
    tax is NEVER booked as an expense in this ledger, in any year: it is a
    permanent cost the firm already deducted before paying out, already
    netted into the income figure recognised here. Grossing it up and
    booking it as an expense would create a permanent, non-deductible
    add-back that puts this ledger on a different basis than both the
    firm's own statement of account and the filed return -- which both
    report the same net figure. The gross amount and the firm's-tax rate
    stay in the workbook's working paper (the One-offs / Monthly sheets)
    only; do not "fix" this by grossing the credit back up.

    additional_share_of_profit is folded into the SAME share_of_profit_income
    leg as the current year's own share of profit (see above) -- not a
    separate posting, and not netted by firms_tax_sop or firms_tax_other a
    second time here: mapper.py (parsers/payout_advice.py's Class B
    contract) already hands this figure through NET -- it is either a
    prior-year PLMI instalment already net of firms_tax_other, or (in the
    FY's final month) interest on capital net of its own TDS. firms_tax_other
    is the firm's tax on that PLMI drawdown, not on the current year's share
    of profit -- subtracting it again here would double-count a tax the
    payslip has already netted out. Folding this figure into the leg at
    exactly its own (already-net) value is what "the PLMI leg is netted by
    firms_tax_other" means in practice: the netting already happened
    upstream, and this leg must not undo or repeat it by subtracting
    firms_tax_other again (the historic defect this fold fixes).

    prior_cohort_drawdown is a drawdown of the current-account balance with
    the firm, NOT current-year income -- the income (and the firm's tax on
    it) was already recognised in the award year's own journal. Booking it
    as income again here would double-count it.

    tds_expense is a debit to an EXPENSE account -- s.194T TDS deducted at
    source on remuneration and interest on capital is booked as an expense
    here, not as an asset/tax-credit, consistently with every other
    deducted-tax leg in this ledger. This is distinct from firms_tax above
    (the firm's own tax on its profit share, which is never booked as an
    expense at all): tds_expense is the partner's own TDS credit, and this
    ledger's convention is to expense it.
    """
    ctx = f"month {line.month}"
    date = _month_end(line.month)
    if firm_name:
        desc = f"{firm_name} - monthly payout {line.month}"
    else:
        desc = f"monthly payout {line.month}"

    splits: list = []
    _add_leg(splits, accounts, "bank", ctx, line.total_paid)
    _add_leg(splits, accounts, "tds_expense", ctx, -line.tds)
    _add_leg(splits, accounts, "capital_contribution", ctx, -line.capital_transferred)
    _add_leg(splits, accounts, "medical_expense", ctx, -line.medical_topup)
    _add_leg(splits, accounts, "remuneration_income", ctx, -line.remuneration)
    _add_leg(
        splits, accounts, "share_of_profit_income", ctx,
        -(line.share_of_profit_gross + line.firms_tax_sop + line.additional_share_of_profit),
    )
    _add_leg(splits, accounts, "interest_on_capital", ctx, -line.interest_on_capital)
    _add_leg(splits, accounts, "current_account", ctx, -line.prior_cohort_drawdown)

    txn_id = _txn_id(fy_pfx, firm_name, f"M{idx:02d}")
    return Journal(txn_id=txn_id, date=date, description=desc, splits=splits)


def _opening_reclass_journal(block: dict | None, fy_pfx: str, firm_name: str = "") -> "Journal | None":
    """Build the optional opening reclassification entry (spec 2.6). This
    exists because a closed, filed year is corrected by a prior-period
    reclassification booked in the FOLLOWING year, never by reopening the
    closed year and never by crediting current-year income."""
    if not block:
        return None

    date = block.get("date")
    if not isinstance(date, str) or not date.strip():
        raise JournalValidationError(
            "opening_reclass.date is required and must be an ISO YYYY-MM-DD string."
        )
    description = block.get("description") or "Opening reclassification"

    raw_splits = block.get("splits") or []
    if not raw_splits:
        raise JournalValidationError(
            "opening_reclass.splits must list at least one split."
        )

    splits: list = []
    for i, s in enumerate(raw_splits):
        account = s.get("account")
        if not isinstance(account, str) or not account.strip():
            raise JournalValidationError(
                f"opening_reclass.splits[{i}] is missing a non-empty 'account'."
            )
        amount = round(float(s.get("amount") or 0.0), 2)
        if abs(amount) < _ZERO_TOLERANCE:
            continue
        account = _strip_root(account)
        if amount >= 0:
            splits.append(Split(account=account, debit=amount))
        else:
            splits.append(Split(account=account, credit=-amount))

    txn_id = _txn_id(fy_pfx, firm_name, "RECT")
    return Journal(txn_id=txn_id, date=date, description=description, splits=splits)


def build_journals(report, accounts: dict) -> list:
    """Build the list of Journal objects implied by a reconciled Report
    (pure -- no I/O, no openpyxl, does not import writer.py).

    accounts is the raw accounts: block from the structured input (see
    ACCOUNT_KEYS / AGENT.md) -- kept separate from Report rather than a
    Report field, since it is purely an output-formatting concern.

    Raises JournalValidationError (a ValueError) for a missing required
    account key or an unbalanced transaction -- agent.run() catches this
    and returns an "ERROR: ..." string, never a traceback.
    """
    accounts = accounts or {}
    fy_pfx = fy_prefix(report.financial_year)
    journals: list = []
    firm_name = getattr(report, "firm_name", "") or ""

    opening = _opening_reclass_journal(getattr(report, "opening_reclass", None), fy_pfx, firm_name)
    if opening is not None and opening.splits:
        _check_balanced(opening)
        journals.append(opening)

    for idx, line in enumerate(report.monthly, start=1):
        journal = _monthly_journal(line, accounts, fy_pfx, firm_name, idx)
        if not journal.splits:
            continue
        _check_balanced(journal)
        journals.append(journal)

    return journals


def write_journal_csv(journals: list, output_path: str) -> None:
    """The ONLY filesystem-touching function in this module. Writes
    journals as a JOURNAL_HEADERS-shaped multi-split CSV: one row per
    split, transaction fields repeated on every row (dialect point b)."""
    rows = []
    for j in journals:
        for s in j.splits:
            signed = round(s.debit - s.credit, 2)  # Dr +, Cr - (dialect point c)
            rows.append({
                "Date": j.date, "Transaction ID": j.txn_id, "Number": j.txn_id,
                "Description": j.description, "Account": s.account,
                "Amount": f"{signed:.2f}", "Currency": CURRENCY,
            })

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=JOURNAL_HEADERS)
        w.writeheader()
        w.writerows(rows)
