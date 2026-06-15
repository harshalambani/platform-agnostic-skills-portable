# GnuCash CSV Import Agent

## Role
Convert raw bank/financial statements (CSV/XLSX) to a canonical, GnuCash-importable CSV format.

## Architecture
**Spec-then-transform:** Deterministic pre-parse → LLM spec generation (column mapping, date format, Dr/Cr convention) → Deterministic row transform → Post-validation.

### Why this design?
- Full-LLM row transformation risks silent data corruption (dropped/hallucinated rows), exceeds context on long statements, unreliable on small local models.
- A small model CAN classify ~8 columns from a ~20-row sample; it CANNOT reliably transform 800 rows.
- Spec-then-transform splits the workload: LLM does the hard part (understand the structure), code does the safe part (apply structure to all rows deterministically).

## Step 1: Pre-parse
Input file → strip preamble/legend junk rows → isolate transaction table.
- **CSV files:** use csv.Sniffer or pandas; skip non-numeric leading/trailing rows.
- **XLSX files:** detect sheet with most data rows; convert to CSV memory.
- Output: cleaned table with header row intact.

## Step 2: LLM Spec Generation
Sample the first 20 rows of cleaned table. Ask the LLM:
- Which column is the date? What format (DD/MM/YYYY, MM/DD/YYYY, YYYY-MM-DD, DD,Mon,YYYY, DD-MM-YY)?
- Which column is the description/narration?
- Is the amount split into Deposit/Withdrawal columns, or is there a single Amount column with a Dr/Cr indicator?
- Which column is the balance? (optional, used for validation)
- What currency (ISO 4217 code, e.g., INR, USD)?
- Cheque/reference column (optional, for TxnID).

**LLM output:** JSON spec:
```json
{
  "date_column": 0,
  "date_format": "DD/MM/YY",
  "description_column": 1,
  "withdrawal_column": 4,
  "deposit_column": 5,
  "balance_column": 6,
  "currency": "INR",
  "txn_id_column": 2,
  "dr_cr_indicator_column": null,
  "has_value_date": true,
  "value_date_column": 3
}
```

Temperature: 0 (deterministic). Snapshot-test per bank.

## Step 3: Deterministic Row Transform
For each row in the table (excluding header + any junk):
1. Parse date column using the spec's date_format → ISO 8601 (YYYY-MM-DD).
2. Extract description; apply basic cleanup (trim trailing codes, preserve UPI VPA and merchant IDs).
3. Parse amount columns:
   - If separate Deposit/Withdrawal: convert each (handle Indian number format: 1,23,456.78 → 123456.78).
   - If single Amount + Dr/Cr indicator: split into Deposit (Cr) or Withdrawal (Dr).
4. Extract balance (convert Indian format).
5. Extract/generate TxnID (Chq./Ref.No. or UUID fallback).
6. Output row: Date,TxnID,Description,Account,Deposit,Withdrawal,Balance,Currency

### Indian Number Format Handling
- Input: `1,23,456.78` (lakh/crore style: 1 crore, 23 lakhs, 456 ones, 78 paise)
- Detection: if a number column contains comma-separated groups of 2 digits, apply lakh/crore parsing.
- Logic: `1,23,456.78` → remove commas → `123456.78` (valid float).
- Alternative: some columns use decimal commas (e.g., `1.234,56` in EU format) — detect based on bank_hint.

### Description Cleanup
- Remove trailing numeric codes (cheque numbers, reference IDs) — but **preserve**:
  - UPI VPA (e.g., `merchant@bank`, `7359777800-2@okbizaxis`).
  - NEFT/IMPS counterparty names (e.g., `ACME CONSULTING LLP`).
  - Card merchant ID codes in parentheses.
- If in doubt, keep the full description; Phase 3 (account mapping) will use stable keys (VPA, counterparty) anyway.

## Step 4: Post-validation
For every transformed statement:
1. **Row count:** # rows in == # rows out (none dropped, none added).
2. **Sum check:** Σ(Deposits) - Σ(Withdrawals) ≈ Closing Balance - Opening Balance (allow ±1 for rounding).
3. **Date check:** all dates parse; chronologically reasonable (no dates >100 years in future).
4. **Amount check:** all Deposit/Withdrawal amounts are numeric; non-negative.
5. **Balance check:** running balance is monotonic within reason (small reversals OK for corrections, large jumps warrant a warning).

If any check fails, emit a validation report with line numbers and issues; do **not** silently drop/skip rows.

## Fallback: Pasted Unstructured Text
If input is plaintext (not CSV/XLSX), apply the original full-transform prompt (user pastes a screenshot transcript or bank email body). Post-validation is **mandatory** on this path.

## Output CSV Schema
```
Date,Transaction ID,Description,Account,Deposit,Withdrawal,Balance,Currency
2025-04-02,NCB2609278553728,CGST-MANAGED CUSTOMER BENEFIT,Checking,0.00,225.00,47037.08,INR
2025-04-02,000000000000000,LOCKER RENT-BRN 1579,Checking,0.00,2500.00,44312.08,INR
2025-04-10,0000102941075952,UPI-COSMO ECOSYSTEM CARE,Checking,0.00,1800.00,42512.08,INR
2025-04-17,XYZBN62025041737036056,NEFT CR-KPMG INDIA SERVICES,Checking,15190.74,0.00,56876.82,INR
```

- All dates in ISO 8601 format (YYYY-MM-DD).
- Amounts as period-decimal floats (no comma).
- Empty cells for zero deposits/withdrawals (or use 0.00, depending on GnuCash version).
- Currency is a constant column (INR for all rows if single-currency statement).

## Configuration
Reads `bank_date_formats.yaml` for bank-specific defaults (VERIFIED 2026-06-11):
```yaml
date_formats:
  ICICI: "DD,Mon,YYYY"       # e.g., "01,Apr,2024" (quoted, comma-separated)
  HDFC: "DD/MM/YY"           # e.g., "02/04/25"
  Karnataka Bank: "DD-MM-YY"
  Kotak: "DD/MM/YYYY"
  HSBC: "DD/MM/YYYY"
  BoB: "DD-MM-YYYY"
  _default: "DD/MM/YYYY"
```

The bank_hint input is the lookup key. If bank_hint is "auto" or absent, the LLM tries to infer the date format from the sample.

## Prompts
- **Primary (spec generation):** This AGENT.md, used to generate the column-mapping spec.
- **Fallback (pasted text):** Existing 2026-06-05 full-transform prompt (retained in `prompts/full_transform.txt`).

## Testing
- **Unit tests:** date_parser (all formats + edge cases), Indian number parser, Dr/Cr splitter, description cleanup.
- **E2E snapshot tests:** per bank sample, verify spec JSON matches known output.
- **Smoke tests:** real ICICI + HDFC statements; post-validation checks pass.

## Known Risks & Mitigations
1. **Silent row loss:** Post-validation catches row count mismatches. Mandatory on all paths.
2. **LLM spec drift:** Temperature 0; snapshot-test per bank.
3. **Description scrubbing too aggressive:** Examples in prompt preserve UPI VPA, merchant ID, counterparty name.
4. **GnuCash importer quirks:** Empty vs. 0 in Deposit/Withdrawal columns depends on version. Test against GnuCash 5.x.
5. **Long statements:** Context window no longer a bottleneck (LLM sees only ~20-row sample + spec, applies to all rows deterministically).
6. **Indian numbers + Dr/Cr:** Both tested in unit tests + smoke tests.

## Glossary
- **Spec:** JSON describing column positions, date format, Dr/Cr convention, currency.
- **Pre-parse:** Deterministic junk-stripping and table isolation.
- **Post-validation:** Deterministic row count + sum + date/amount sanity checks.
- **TxnID:** Cheque/reference number from the statement, or UUID if missing.
- **Canonical schema:** 8-column CSV that GnuCash importer accepts.
