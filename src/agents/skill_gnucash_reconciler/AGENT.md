# GnuCash Reconciler Agent (Phase 4 lite)

## Role
Compare normalized CSV (from Phase 1) against GnuCash file. Flag duplicates, balance gaps, missing transactions. Read-only — no writes.

## Inputs
1. **normalized_csv** — 8-column canonical CSV (Date, TxnID, Description, Account, Deposit, Withdrawal, Balance, Currency)
2. **gnucash_file** — gzipped XML `.gnucash` file
3. **account_filter** (optional) — restrict to one account (e.g., "Assets:Checking")

## Process

### Step 1: Parse GnuCash
- Extract account tree + transaction list
- For specified account_filter, extract transactions in that account only
- Build existing txn index: (date, amount, description_key) → GnuCash txn

### Step 2: Index CSV Rows
For each CSV row:
- Parse date, amount (deposit - withdrawal), description
- Build key: (date, amount, description_key)
- Extract description_key: UPI VPA, NEFT counterparty, or first 40 chars

### Step 3: Match & Flag
For each CSV row, check against GnuCash index:

| Status | Condition | Action |
|--------|-----------|--------|
| **Match** | (date, amount, description_key) found in GnuCash | ✓ Matched |
| **Duplicate** | Multiple GnuCash txns match same key | ⚠ Review |
| **New** | Not in GnuCash | ℹ To import |
| **Missing** | GnuCash txn not in CSV | ⚠ Check filters |

### Step 4: Balance Check
If CSV has Balance column:
- Track running balance per account in GnuCash
- Compare to CSV's closing balance
- Flag if delta > ±1 (rounding tolerance)

### Step 5: Output

**reconciliation_report.csv:**
```
Row,Date,Description,Status,Details
1,2025-04-02,CGST BENEFIT,Match,"Found in Assets:Checking"
2,2025-04-02,LOCKER RENT,Match,"Found in Assets:Checking"
3,2025-04-10,UPI-CRED CLUB,New,"Not in GnuCash (1 alternative: Low confidence)"
4,2025-04-17,NEFT-ACME,Match,"Found in Assets:Checking"
```

**summary.json:**
```json
{
  "file": "MyFinances2425.gnucash",
  "account": "Assets:Checking",
  "csv_rows": 91,
  "matched": 85,
  "duplicates": 2,
  "new": 4,
  "missing": 0,
  "balance_gaps": 0,
  "actions": [
    "Review 2 potential duplicates before import",
    "4 new transactions ready to import",
    "No balance gaps detected"
  ]
}
```

## Safety
- **No writes** — read-only analysis
- **Lock check** — warn if `.LCK` file present (GnuCash has file open)
- **Dry run** — safe to run anytime

## Known Limitations
- Description matching: key-based only (UPI VPA, NEFT name, merchant ID)
- Balance check: only if CSV has Balance column
- Multi-currency: all amounts must be same currency as account

## Phase Integration
Output feeds into Phase 6 (orchestrator):
- "duplicates" count → user review before import
- "new" rows → ready for Phase 1 CSV import
- "missing" → investigate; possibly external transactions not yet in statement
