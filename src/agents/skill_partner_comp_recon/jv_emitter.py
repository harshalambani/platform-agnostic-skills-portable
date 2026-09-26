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

from .engine import (
    RECONCILIATION_TOLERANCE,
    residual_current_account_check,
    year_end_accrual_diff,
    fy_prefix,
    firm_token as _firm_token,
    journal_txn_id as _txn_id,
)

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


# H35-04 rework: fy_prefix / _firm_token (as firm_token) / _txn_id (as
# journal_txn_id) moved to engine.py and imported back above under their
# original names here, so build_report() can also compute a pending
# journal's Transaction ID (for the "PENDING JOURNAL POSTING" verdict on
# the current-account-closing tie-out row) without engine.py importing
# FROM this module (which already imports from engine.py -- that would be
# circular). Behaviour is unchanged; this is a pure relocation.


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


def build_accrual_journal(report, accounts: dict) -> tuple:
    """H35-02: the year-end share-of-profit accrual journal.

    The L5 LLP Statement of Account is the final authority for the year's
    actual "Profit Share for the Year" (user ruling: "Y-3 should come from
    the Statement of account as the final say" -- see engine.py's Exempt-SoP
    reconciliation row). _monthly_journal(), above, only ever books
    share_of_profit_income month by month from the payout advices; when the
    L5 statement's own year-end figure is higher, the shortfall (arrears
    included -- it is a single lump comparison against the whole year's
    monthly total, not booked per-month) is accrued ONCE here, as a
    separate 31-March entry -- never inside the monthly journal, and never
    as a second share_of_profit_income posting layered on top of what
    build_journals()'s monthly loop already booked (that loop is untouched
    by this function).

    RED-FLAG-relevant by construction: this function posts Dr
    current_account / Cr share_of_profit_income ONLY. It never reads or
    writes tds_expense (or any account other than those two), so it cannot
    create or duplicate a TDS posting -- s.194T TDS is deducted at source on
    each MONTHLY remuneration/share-of-profit payout and is already fully
    booked by _monthly_journal(); this accrual is a pure profit-recognition
    entry with no cash movement, so there is nothing for it to withhold tax
    on. See tests/test_skill_partner_comp_recon.py's H35-02 tests for the
    exact proof that monthly-booked SoP + this accrual == the L5 figure,
    i.e. share of profit is booked once in total, never twice.

    Returns (Journal | None, note, residual):
      - `residual` (H35-02 item 4) is always an engine.ReconciliationResult
        from engine.residual_current_account_check(), comparing the L5
        statement's current_closing_balance against the booked current
        account AFTER whatever this call applied to it (0.0 in every branch
        below that returns Journal None). It is computed and returned on
        every path -- including "no L5" -- so the caller always has it,
        never something the caller must remember to compute separately. A
        VARIANCE there is reported, never booked -- this function's own
        splits never react to it.
      - report.llp_record is None -> (None, a note saying no L5 was
        supplied, so no accrual journal was produced, residual).
      - L5 present but 'current_profit_share' ("Profit Share for the Year")
        is None -> (None, a note naming the missing field, residual).
      - |L5 profit share - already-booked monthly total| <=
        RECONCILIATION_TOLERANCE (the shared Re 1 tolerance -- see
        engine.RECONCILIATION_TOLERANCE) -> (None, "ties, no accrual
        needed", residual).
      - Difference negative (the monthly total already booked EXCEEDS the
        L5 figure) -> (None, a note flagging this for manual review --
        never a silent reversing/negative entry, residual).
      - Otherwise -> (Journal, a note stating the amount booked and how it
        was derived, residual computed with that amount applied).
    """
    llp_record = getattr(report, "llp_record", None)
    if llp_record is None:
        return None, (
            "No L5 (LLP Statement of Account) was supplied for this FY -- the "
            "year-end share-of-profit accrual cannot be computed and no "
            "accrual journal was produced. Supply the L5 statement to enable it."
        ), residual_current_account_check(report, 0.0)
    l5_profit_share = llp_record.get("current_profit_share")
    if l5_profit_share is None:
        return None, (
            "The L5 (LLP Statement of Account) was supplied but its 'Profit "
            "Share for the Year' figure could not be parsed -- the year-end "
            "accrual cannot be computed and no accrual journal was produced."
        ), residual_current_account_check(report, 0.0)

    monthly = getattr(report, "monthly", None) or []
    # Arrears included: this is the WHOLE year's already-booked total in one
    # comparison, not a per-month accrual -- exactly mirrors the
    # share_of_profit_income leg formula in _monthly_journal(), above.
    # H35-04 rework: this diff is now computed by engine.year_end_accrual_diff(),
    # which build_report() also calls (to offer this same journal as a
    # "PENDING JOURNAL POSTING" closure on the current-account-closing L5
    # tie-out row) -- routing both through one function means the two can
    # never desync. booked_sop is recomputed here only for the human-
    # readable note text below; it uses the identical formula.
    booked_sop = sum(
        (m.share_of_profit_gross + m.firms_tax_sop + m.additional_share_of_profit)
        for m in monthly
    )
    diff = year_end_accrual_diff(llp_record, monthly)

    if abs(diff) <= RECONCILIATION_TOLERANCE:
        return None, (
            f"L5 'Profit Share for the Year' ({l5_profit_share:,.2f}) already "
            f"ties to the monthly total already booked ({booked_sop:,.2f}) "
            f"within Rs {RECONCILIATION_TOLERANCE:.2f} -- no accrual journal needed."
        ), residual_current_account_check(report, 0.0)
    if diff < 0:
        return None, (
            f"FLAGGED, NOT BOOKED: the monthly total already booked "
            f"({booked_sop:,.2f}) EXCEEDS the L5 'Profit Share for the Year' "
            f"({l5_profit_share:,.2f}) by {abs(diff):,.2f}. This is not reversed "
            "automatically -- review manually before any correcting entry."
        ), residual_current_account_check(report, 0.0)

    fy = report.financial_year
    m = re.match(r"\s*(\d{4})-(\d{2})\s*$", fy or "")
    if not m:
        raise JournalValidationError(
            f"financial_year {fy!r} is not in 'YYYY-YY' form -- cannot date the "
            "31 March year-end accrual entry."
        )
    # The accrual belongs in the FY being reconciled, i.e. 31 March of the FY
    # END year -- "2025-26" means the year that ENDS 31 March 2026, not the
    # year that STARTS in 2025. Dating this from the FY start year (a past
    # defect) would misbook the accrual into the wrong, already-closed FY.
    # Computed from the integer start year (never string-glued from the "YY"
    # suffix) so the century rolls over correctly, e.g. "2099-00" -> 2100,
    # not the nonsensical "209900" a naive f"{start}{yy}" concat would give.
    start_year = int(m.group(1))
    yy_suffix = int(m.group(2))
    end_year = start_year + 1
    expected_yy = end_year % 100
    if yy_suffix != expected_yy:
        raise JournalValidationError(
            f"financial_year {fy!r} is not a valid 'YYYY-YY' span -- the "
            f"second year must be {start_year} + 1 = {end_year} (suffix "
            f"'{expected_yy:02d}'), not '{yy_suffix:02d}'. Cannot date the 31 "
            "March year-end accrual entry against an inconsistent FY."
        )
    date = f"{end_year:04d}-03-31"
    fy_pfx = fy_prefix(fy)
    firm_name = getattr(report, "firm_name", "") or ""
    ctx = "year-end share-of-profit accrual (H35-02, per L5)"

    splits: list = []
    _add_leg(splits, accounts, "current_account", ctx, diff)
    _add_leg(splits, accounts, "share_of_profit_income", ctx, -diff)

    if firm_name:
        description = f"{firm_name} - year-end share-of-profit accrual per L5 (FY{fy})"
    else:
        description = f"Year-end share-of-profit accrual per L5 (FY{fy})"

    txn_id = _txn_id(fy_pfx, firm_name, "ACCR")
    journal = Journal(txn_id=txn_id, date=date, description=description, splits=splits)
    _check_balanced(journal)
    return journal, (
        f"Accrual of {diff:,.2f} booked (Dr current account / Cr share of "
        f"profit income): L5 'Profit Share for the Year' {l5_profit_share:,.2f} "
        f"vs {booked_sop:,.2f} already booked by the monthly journal."
    ), residual_current_account_check(report, diff)


def write_accrual_journal_csv(journal, output_path: str) -> None:
    """Writes the H35-02 year-end accrual journal to its OWN, SEPARATE CSV
    file, in exactly write_journal_csv()'s dialect -- it must never be
    merged into the monthly journal CSV (H35-02 item 3). `journal` is a
    single Journal or None (as returned by build_accrual_journal()); if
    None, no file is written at all -- the caller already has a note
    explaining why."""
    if journal is None:
        return
    write_journal_csv([journal], output_path)


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
