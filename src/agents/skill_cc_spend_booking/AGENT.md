# Credit Card - Book Spends

Direct skill (no LLM loop). `agent.run` reads the card statements (through the CC Transactions
extractor), reads the book read-only, matches bank payments to statements, and writes an
import-ready CSV plus a run workbook. See skill.yaml for the inputs and the help text.

Rules that must not change:
- Cash basis. A payment line on statement k settles statement k-1 of the same card.
- Payments are never guessed: ambiguous, unmatched and partly matched cases are reported.
- Only PASS statements are booked. EMI interest goes to the entity card_emi_interest_account and the EMI processing fee and GST to Bank Service Charge (UNVALIDATED wording); EMI conversion, principal and unclassified rows are never booked.
- A payment with no statement is a RED FLAG at the top of the result; the run is never "successful" while one exists.
- Large spends are flagged, never moved to an asset account.
