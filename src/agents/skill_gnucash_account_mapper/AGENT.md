# GnuCash Account Mapper Agent

## Role
Extract historical description→account mappings from a GnuCash file and generate reusable mapping rules for automated transaction categorization.

## Input Processing
1. **GnuCash XML file** (gzipped): Parse account tree + transaction history.
2. **Fallback**: Accept pasted account list (flat format) if file not provided.

## Step 1: Parse Account Hierarchy
From `.gnucash` (gzipped XML):
- Walk `<gnc:account>` elements.
- Reconstruct colon-delimited hierarchy: `Assets:Checking:USD`, `Expenses:Groceries`.
- Build account ID → (full_path, type) mapping.
- Validate target accounts exist (used by Phase 4 to catch renames).

## Step 2: Extract Transactions & Build Description→Account Mapping
For each transaction in the file:
1. Iterate splits (each split = amount + account).
2. Skip multi-split transactions where no single split dominates (>70% of amount).
3. For single-dominant split or two-way transfers:
   - Extract transaction description.
   - Parse **key fields**:
     - **UPI VPA**: e.g., `merchant@bank` or `7359777800-2@okbizaxis` → extract stable VPA.
     - **NEFT/IMPS counterparty**: e.g., `ACME CONSULTING LLP` → extract exact name.
     - **Card merchant ID**: e.g., `MERCHANT-1234-ABC` → extract numeric code.
   - Build map: (key_field, account) → [(transaction_date, description)].

### Key Extraction Rules
- **UPI**: if description contains `@`, extract the VPA (`xxx@yyy`).
- **NEFT/IMPS**: if description starts with `NEFT` or `IMPS`, extract counterparty name (word after bank code).
- **Card**: if description contains merchant ID pattern (`[A-Z0-9]{4,}`), extract it.
- **Fallback**: use full description if no special key applies (lower confidence).

## Step 3: Confidence Scoring
For each (key_field, account) pair:
1. **Frequency**: count of matching transactions.
2. **Recency decay**: weight by time, favor recent matches:
   - Last 3 months: weight = 1.0
   - 3–12 months: weight = 0.7
   - >12 months: weight = 0.3
3. **Confidence tier** (rules):
   - **High**: frequency ≥ 5 AND weighted_frequency ≥ 3 AND last_seen <12 months.
   - **Medium**: frequency ≥ 2 OR recency/frequency disagreement (multiple candidates).
   - **Low**: frequency = 1 OR >12 months old.

## Step 4: Output Formats

### Mapping YAML (`mapping.yaml`)
```yaml
metadata:
  gnucash_file: MyFinances2425.gnucash
  generated: 2025-06-11
  accounts: 237
  rules: 142

rules:
  - key: salary@employer.com
    type: upi_vpa
    accounts: ["Income:Salary"]
    confidence: High
    frequency: 12
    last_seen: "2025-03-31"
    basis: "12 UPI VPA matches, all within last 12 months"

  - key: ACME CONSULTING LLP
    type: neft_counterparty
    accounts: ["Income:Consulting"]
    confidence: High
    frequency: 4
    last_seen: "2025-03-15"
    basis: "4 NEFT matches, most recent 3 months ago"

  - key: 7359777800
    type: upi_phone
    accounts: ["Expenses:Groceries"]
    confidence: Medium
    frequency: 8
    last_seen: "2025-02-20"
    basis: "8 matches; 1 recent High-confidence alternative"

  - key: UBER
    type: merchant_keyword
    accounts: ["Expenses:Transportation"]
    confidence: Medium
    frequency: 15
    last_seen: "2025-03-28"
    basis: "15 matches; slight mismatch with 1 alternative account"
```

### Confidence Report CSV
```
Rule Key,Key Type,Primary Account,Frequency,Confidence,Last Seen,Basis
salary@employer.com,upi_vpa,Income:Salary,12,High,2025-03-31,"12 matches, all <12mo"
ACME CONSULTING LLP,neft_counterparty,Income:Consulting,4,High,2025-03-15,"4 matches, most <3mo"
7359777800,upi_phone,Expenses:Groceries,8,Medium,2025-02-20,"8 matches; 1 High-conf alternative"
UBER,merchant_keyword,Expenses:Transportation,15,Medium,2025-03-28,"15 matches; fuzzy on 1 alt"
```

### Account Hierarchy JSON
```json
{
  "accounts": [
    {
      "id": "f533...",
      "name": "Root Account",
      "path": "",
      "type": "ROOT"
    },
    {
      "id": "7735...",
      "name": "Assets",
      "path": "Assets",
      "type": "ASSET",
      "children": [
        {
          "id": "1234...",
          "name": "Checking",
          "path": "Assets:Checking",
          "type": "BANK"
        }
      ]
    }
  ]
}
```

## Validation
1. **Account existence**: verify each rule's target accounts exist in current account tree.
2. **Key uniqueness**: warn if same key maps to multiple accounts (confidence downgraded to Medium/Low).
3. **Data quality**: report any malformed descriptions or edge cases encountered.

## Phase 1 Integration
Phase 1 skill reads `mapping.yaml` and uses rules to auto-assign Account column:
- If statement description matches a rule key (UPI VPA, NEFT counterparty, etc.), use High-confidence account.
- If no match, leave Account blank for user to assign at import time.
- Output includes confidence.csv sidecar; user sorts by Low confidence for manual review.

## Known Limitations
- Multi-split transactions (3+ splits): skipped unless one split clearly dominates.
- Stale mappings (>2 years old): flagged but not applied (require manual review/update).
- Edge case: transfer transactions between user's own accounts; these are filtered out.

## Glossary
- **Key field**: stable, extractable part of description (UPI VPA, NEFT name, merchant ID).
- **Confidence tier**: High/Medium/Low based on frequency + recency.
- **Per-file scoping**: rules are stored per `.gnucash` file (supports multiple family members).
