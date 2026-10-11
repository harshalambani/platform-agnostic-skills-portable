# Credit Card - Book Spends

Direct skill (no LLM loop). `agent.run` reads the card statements (through the CC Transactions
extractor), reads the book read-only, matches bank payments to statements, and writes an
import-ready CSV plus a run workbook. See skill.yaml for the inputs and the help text.

Rules that must not change:
- Cash basis. A payment line on statement k settles statement k-1 of the same card.
- Payments are never guessed: ambiguous, unmatched and partly matched cases are reported.
- Only PASS statements are booked. HSBC EMI (loan) statements are booked from the instalment rows: the instalment principal as a spend (the original purchase is withheld, so it is counted once), the instalment interest to the entity card_emi_interest_account, the processing fee and GST to Bank Service Charge. The credit legs and the conversion credit are never booked. Other banks' EMI wording is UNVALIDATED; any unrecognised EMI, instalment or loan row is listed, never booked.
- A payment with no statement is a RED FLAG at the top of the result; the run is never "successful" while one exists.
- Large spends are flagged, never moved to an asset account.
