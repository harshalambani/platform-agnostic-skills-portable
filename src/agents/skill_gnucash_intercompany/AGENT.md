# Intercompany Reconciliation Agent (DIRECT mode, no LLM)

## Role
Reconcile the contra ("counter-party") accounts that two people keep for each
other across two GnuCash books (e.g. Vaikunth <-> Kiran). Read-only.

## Inputs
1. **book_a**, **book_b** -- two gzipped-XML `.gnucash` files.
2. **period** -- Indian FY (1 Apr - 31 Mar), a calendar year, `Auto` (FY read
   from the filename suffix, e.g. `...2526` -> FY 2025-26), or a custom range.
3. **custom_start / custom_end** -- `YYYY-MM-DD`, only for a custom range.
4. **date_tolerance** -- days two mirrored entries may differ by (default 7).

## Process
1. **Owner & FY from filename** -- `VaikunthAmbani2526` -> owner `Vaikunth
   Ambani`, FY 2025-26. `...HUF...` is a **distinct entity** from the individual.
2. **Find contra accounts** -- every account in book B whose name contains all
   of book A owner's core name tokens (and vice-versa). Includes multiple
   purpose-accounts for one entity (`Kiran Ambani` + `Rent receivable -Kiran
   Ambani`); excludes a same-surname HUF unless the owner *is* the HUF. Child
   accounts are included.
3. **Opening b/f** -- net of all contra splits dated before the period start,
   per side.
4. **Match FY movements** -- greedy one-to-one; a movement in A mirrors one in B
   with the **opposite sign**, within `date_tolerance`. Tie-break by shared
   tokens (6+ digit bank/reference numbers and name words drawn from the
   description, memo, and the other legs' account names).
5. **Mis-posting hunt** -- for each unmatched exception, search the **entire**
   other book for an opposite-sign, same-magnitude entry within the date window;
   rank BANK/CASH/CREDIT accounts first, then shared tokens, then date
   proximity. Report up to 3 candidates, or "no candidate".
6. **Balance tie** -- closing_A should equal `-(closing_B)`; the reported
   difference is `closing_A + closing_B` (0 = ties). Note the balance can tie
   while exceptions remain, when unmatched items net to zero.

## Output
`...-Intercompany-Recon.xlsx` in `Data/outputs/`:
- **Summary** -- owners, period, contra accounts, opening/movements/closing per
  side, the difference (green ties / red out-of-balance), and counts.
- **Matched** -- paired entries side by side with day-gap and match basis.
- **Exceptions <A>** / **Exceptions <B>** -- unmatched items with the
  mis-posting hunt results.

## Safety
- **Read-only** -- never writes to the `.gnucash` files.
- **Lock check** -- warns (stderr) if a `.LCK` file is present, then reads anyway.

## Design notes / future
- Deterministic; no LLM (`requires.llm: false`). The matcher rarely needs one;
  an LLM ranking pass over ambiguous hunt candidates is a possible later add.
- Core is structured so an all-family **matrix** (every book vs every other, one
  workbook) can be layered on without changing the reconcile engine.
- A useful extension: when the hunt finds no candidate but the description names
  a related entity (e.g. `...HUF`), point the user at that entity's book.
