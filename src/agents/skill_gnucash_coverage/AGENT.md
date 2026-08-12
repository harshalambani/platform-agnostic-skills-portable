# Coverage Gap Detector Agent (DIRECT mode, no LLM)

## Role
Infers suspected missing-statement months for bank and credit-card accounts
purely from the transaction dates already posted in a GnuCash book -- this
codebase has no import ledger to consult, and building one is explicitly out
of scope for v1. Read-only; never modifies a `.gnucash` file.

## Inputs
1. **entities** -- UI-only multiselect of registered entities (fills `books`
   below via `book_from`; never referenced in `run_args`, so it cannot become
   the output-filename source -- see `tests/test_entity_book_wiring.py`).
2. **books** -- one or more `.gnucash` file paths (from the entity picker,
   Browse, or both). Several books/entities produce ONE consolidated report.

## Process
1. For each book, read every account via `agents.gnucash_accounts.load_accounts`
   and keep the postable BANK/ASSET and LIABILITY/CREDIT accounts
   (`SCOPE_TYPES`) -- both families are in scope, type-agnostic month
   bucketing, the account's class is carried through to the report.
2. Collect every split's posting date per account, and note which
   transactions touch the book's opening-balance Equity account (identified
   via the `equity-type`/`opening-balance` KVP flag, never by description
   string-matching).
3. Per account: the **active window** runs from the account's own first
   transaction date (not the FY start) to FY-end or today, whichever is
   earlier. Every calendar month in that window with zero transactions
   (opening-balance transactions included for this zero-check, so a
   genesis month with only an opening balance is not falsely flagged) is a
   suspected gap.
4. Grading: each account's MEDIAN monthly transaction count (opening-balance
   transactions EXCLUDED, since they skew the median) over its whole active
   window, including zero months, grades every zero month for that account.
   `HIGH_CONFIDENCE_MEDIAN_THRESHOLD` (module-level constant, currently `4`)
   is the HIGH/LOW cutoff. Both grades are always reported -- never
   suppressed -- with the median shown so grading is auditable.
5. Trailing gaps: zero months after an account's LAST transaction get their
   own `trailing=True` flag and are surfaced first in the report -- this is
   the highest-value signal (usually "the most recent statement was never
   imported").
6. FY-boundary check: a zero month that IS the book's own FY-boundary month
   (first or last calendar month of its FY) is cross-checked, only when the
   entity is registered in `entities.yaml`, against the adjacent FY's
   registered book (`ui._book_registry.list_books`) for the SAME account
   having a transaction dated in that exact calendar month. If so, the gap
   is suppressed (counted separately) rather than reported -- it is
   evidence of a postings-filed-into-the-wrong-year artefact, not a missing
   statement.

## Output
`...-Coverage-Gaps.xlsx` in `Data/outputs/`:
- **Gaps** -- one row per suspected gap month: entity, book, account,
  account class, month, HIGH/LOW confidence, the account's own median, and a
  prominent TRAILING flag/highlight.
- **Summary** -- one row per in-scope account with transaction history:
  first/last transaction date, active-window end, median, confidence, and
  gap/trailing/FY-boundary-suppressed counts.

## Reuse / relationship
- `agents.gnucash_accounts` supplies account typing and opening-balance
  identification (shared with the GnuCash Pipeline and journal-builder
  skills).
- `derive_owner_and_fy()` from `skill_gnucash_intercompany/scripts/
  reconcile_intercompany.py` is reused, via the same `sys.path` insertion
  pattern as `skill_gnucash_intercompany_matrix/scripts/matrix_recon.py`,
  as a filename-based FALLBACK label/FY for a book that was Browsed in
  rather than picked via the entity multiselect (no registry match ->
  `entity_key=None` -> FY-boundary consultation is simply skipped for that
  book, since there is no registry entry to consult).
- The PRIMARY entity-resolution path is a reverse lookup against
  `entities.yaml`'s own registered `books` (via `configs.load_entities`),
  matched by resolved path -- this recovers both a clean display name and
  the `entity_key` that `ui._book_registry.list_books()` needs, in one step.
- Complements, rather than duplicates, `skill_gnucash_pipeline`'s
  `_reconcile_opening_balance()`: that check compares one statement against
  one account and cannot see a month where no statement was ever imported;
  this skill looks across an account's whole history instead.

## Safety
- Read-only; parses the book's XML once and never opens it for write.
- No import ledger is built or consulted -- gaps are inferred purely from
  the dates already present in the book, per the brief for this skill.
- Partial-month detection and auto-fetching missing statements are
  explicitly out of scope for v1.
