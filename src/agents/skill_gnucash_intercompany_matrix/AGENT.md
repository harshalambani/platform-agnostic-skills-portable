# Intercompany Matrix Agent (OPTIONAL roll-up, DIRECT mode, no LLM)

## Role
An optional all-family overview built on the pairwise Intercompany Reco engine.
Reconciles **every unordered pair** among N GnuCash books and rolls the results
into one workbook. Read-only.

## Inputs
1. **books** -- a list of `.gnucash` files (2 or more).
2. **period / custom_start / custom_end / date_tolerance** -- identical meaning
   to the pairwise skill (Indian FY 1 Apr - 31 Mar; `Auto` reads FY from the
   filename; default tolerance 7 days).

## Process
1. For each pair `(A, B)` in `combinations(books, 2)`, call
   `reconcile_intercompany.reconcile(A, B, ...)`.
2. Pairs that raise (no mutual contra account, unresolved period) are captured
   as **n/a** rather than failing the whole run.
3. Because `X HUF` is a distinct entity, a person and their HUF form their own
   pair -- this is where cross-entity mis-postings (e.g. HUF payments booked to
   an individual's account) surface as either an out-of-balance pair or an
   exception.

## Output
`...-Intercompany-Matrix.xlsx` in `Data/outputs/`:
- **Matrix** -- owner x owner grid of the balance difference per pair
  (green = ties, red = out of balance, grey = n/a, `-` on the diagonal).
- **Pairs** -- one row per pair: contra accounts, opening/closing per side,
  difference, status, and match/exception counts.
- **All Exceptions** -- every unmatched item across all reconciled pairs, tagged
  with the pair and the book it was recorded in, plus the single best probable
  posting found in the other book. This is the consolidated to-do list.

## Reuse / relationship
- Depends on `skill_gnucash_intercompany/scripts` (`reconcile_intercompany`,
  `excel_report`) via a sys.path insert -- no logic is duplicated.
- The pairwise skill stays the primary two-book tool; open any red pair there
  for full Matched / Exceptions-per-side detail.

## Safety
- **Read-only**; warns on `.LCK` (book open) then reads anyway.
- Each book is parsed once per pair it appears in (fine at family scale).
