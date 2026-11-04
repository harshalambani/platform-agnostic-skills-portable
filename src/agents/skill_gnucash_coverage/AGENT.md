# Coverage Gap Detector Agent (DIRECT mode, no LLM)

## Role
Infers months with no transactions for bank and credit-card accounts
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
1. **Scope.** For each book, read every account via
   `agents.gnucash_accounts.load_accounts` and keep the postable BANK and
   CREDIT accounts (`CORE_TYPES`) -- the accounts that receive a monthly
   statement. An opt-in input `include_other` (select no/yes, default no)
   also checks ASSET and LIABILITY accounts (`OTHER_TYPES`); they are
   reported in their own section, "Other accounts", because an empty month
   there is often normal. EQUITY and every other type are never checked.
   Hidden, placeholder and no-transaction accounts are always excluded.
2. Collect every split's posting date per account, and note which
   transactions touch the book's opening-balance Equity account (identified
   via the `equity-type`/`opening-balance` KVP flag, never by description
   string-matching).
3. **Window.** Only the book's own financial year (1 Apr - 31 Mar), capped
   at today. The year comes from the entity registry, or from the filename
   when the book is Browsed in. If neither gives a year, the year of the
   book's latest transaction is used and the opening line says so. The
   window never starts at an account's first-ever transaction, so old
   history (for example from 2009) cannot leak in. If an account's first
   transaction falls inside the year, the months before it are not missing.
4. **Gaps.** Every month in the window with no transaction (opening-balance
   entries count as transactions for this test) is a gap. Months after the
   account's last transaction are reported first, as "no transactions since
   <Mon YYYY> - the latest statement(s) probably not imported".
5. **Quiet accounts.** An account with fewer than `QUIET_TXNS_PER_MONTH`
   (1.0) transactions a month on average inside the year, opening-balance
   entries excluded, is "quiet": its line says "this account is quiet, so
   these may be months with no activity - check before importing". There is
   no HIGH/LOW grade, no median and no "Trailing" wording anywhere the user
   sees.
6. FY-boundary check: a gap on the year's first or last month is
   cross-checked, only when the entity is registered in `entities.yaml`,
   against the adjacent FY's registered book (`ui._book_registry.list_books`)
   for the SAME account having a transaction dated in that exact month. If
   so the gap is left out (and counted in the reply) rather than reported --
   it is evidence of a postings-filed-into-the-wrong-year artefact, not a
   missing statement.

## Output
Opening line, in the reply and on the first sheet: "N bank and card accounts
checked for FY 2025-26 (Apr 2025 - Mar 2026); K have months with no
transactions." Then one line per account with gaps. `...-Coverage-Gaps.xlsx`
in `Data/outputs/`:
- **Missing months** -- the opening line, then Account | Months with no
  transactions | What it means, one row per account with gaps. With the
  opt-in on, "Other accounts" rows sit in their own labelled section below.
- **Accounts checked** -- one row per checked account, including accounts
  with no gaps: kind of account, transactions in the year, months with
  transactions ("x of y"), first and last transaction in the year.

Tab placement is unchanged: GnuCash > Banks (`_BANKS_TAB_ORDER`).

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
