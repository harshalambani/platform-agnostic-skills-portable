# ICICI Bank Statement Import Agent

## Role
Convert ICICI Bank .xls statement downloads to GnuCash-importable CSV format.
Hardcoded, deterministic parser — no LLM needed.

## Input Format
ICICI net banking "Detailed Statement" XLS download (BIFF format).
Structure: 12 preamble rows → header row → data rows → optional legend/footer.

Columns (0-indexed in raw CSV after LibreOffice conversion):
- 0: empty (table offset)
- 1: S No.
- 2: Value Date (DD,Mon,YYYY — e.g., "01,Apr,2024")
- 3: Transaction Date (DD,Mon,YYYY) — dropped, Value Date is authoritative
- 4: Cheque Number (or "-")
- 5: Transaction Remarks
- 6: Withdrawal Amount (INR)
- 7: Deposit Amount (INR)
- 8: Balance (INR) — dropped

## Output Format
CSV with columns: Value Date, Cheque Number, Transaction Remarks, Withdrawal Amount (INR ), Deposit Amount (INR )
- Dates: dd/mm/yyyy (e.g., 01/04/2024)
- Numbers: plain decimals, trailing zeros stripped (300000.00 → 300000)
- Cheque: extracted ref from description where applicable, "-" if none

## Description Transform Rules
Reference numbers are extracted from descriptions into the Cheque Number column:

| Prefix | Extract | Example |
|--------|---------|---------|
| MMT/IMPS/REF/... | REF → cheque | MMT/IMPS/409222854999/On account → cheque=409222854999, desc=On account |
| NEFT-REF-... | REF → cheque | NEFT-CITIN24449621398-MANIPAL... → cheque=CITIN24449621398 |
| RTGS-REF-... | REF → cheque | RTGS-XYZBA2202403...-MR JOHN... → cheque=XYZBA2202403... |
| UPI/REF/... | REF → cheque, strip trailing bank hash (/ICI.../AXI...), drop NA/ | |
| BIL/ONL/REF/... | REF → cheque | |
| BIL/INFT/REF/... | REF → cheque | |
| INF/INFT/REF/... | REF → cheque | |
| NFS/.../REF/... | REF → cheque | |
| CMS/ CMSREF/... | CMSREF → cheque | |

Kept as-is (no transform): CLG, ATD, Rev Sweep, Closure Proceeds, AUTOSWEEP, Interest, TDS, Sweep Adj.

"Xfer to self" descriptions are preserved across all types — they mark contra entries.

## Pipeline
1. XLS → CSV via LibreOffice (`--headless --convert-to csv`)
2. Skip 12 preamble rows + 1 header row
3. Filter data rows (S No. must be numeric, skip legend/footer)
4. Transform each row: parse date, extract cheque from description, format amounts
5. Post-validate: row count match, sum reconciliation, date/amount sanity

## Post-Validation
- Row count: input == output (zero tolerance)
- Sum check: Σ(withdrawals) and Σ(deposits) match original within ±1.0
- Date check: all dates parse as dd/mm/yyyy, years 2000–2030
- Amount check: no negative values

## Dependencies
- LibreOffice (for XLS→CSV conversion)
- Python standard library only (csv, re, datetime, subprocess)

## Tested Against
- icici-2023-24.xls: 358 rows, FY 2023-24
- icici-2024-25.xls: 465 rows, FY 2024-25
- OpTransactionHistory18-04-2026.xls: 459 rows, 5-day sample with legend footer
