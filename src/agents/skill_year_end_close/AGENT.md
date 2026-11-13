# Year-End Close Agent (DIRECT mode, no LLM)

One entity, one closing FY, one step per run (see skill.yaml). Paths come only from
config (entity folder_name, the two year-end roots) and the book registry.

Rules that must not change:
- Nothing is deleted, moved, overwritten or renamed; a .gnucash file is only copied or read.
- Any .LCK file in the source or target GnuCash folder stops every step.
- One entity's run never touches another entity's folders or registry entry.
- The year is not done until step 4 passes (zero Income/Expense at 31 March AND closing entries).
- Step 5 only lists and writes a Recycle Bin script; the final book is never listed.
