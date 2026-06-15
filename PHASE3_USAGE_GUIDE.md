# Phase 3: GnuCash Account Mapping Pipeline

## Overview

Phase 3 automates the account mapping process to transform bank statements into GnuCash-ready CSVs.

```
Raw Bank Statement 
    ↓
[Phase 1 Bank Skill] → Canonical CSV (8 columns)
    ↓
[Phase 3: Mapping Pipeline]
  - Extract: Parse historic GnuCash file
  - Generate: Create mapping rules (YAML)
  - Validate: Test rules against eval set
  - Map: Apply rules to canonical CSV
    ↓
[Mapped CSV with Account column + Confidence report]
    ↓
[GnuCash Import] → Transactions in correct accounts
```

---

## Components

### Task 5: skill_gnucash_xml_extractor
Parse a GnuCash file and extract transaction mappings.

**Input:** `.gnucash` file (gzipped XML)  
**Output:** JSON with account tree + description→account mappings

**Usage:**
```bash
cd src/agents
python3 skill_gnucash_xml_extractor/agent.py \
  path/to/GnuCash.gnucash \
  output_extract.json
```

**Output structure:**
```json
{
  "account_tree": {...},
  "mappings": {
    "ICICI": [
      {"description": "UPI/...", "account": "Expense:Food", "frequency": 15, "last_date": "2025-06-10"}
    ],
    "HDFC": [...],
    "HSBC": [...],
    "BoB": [...]
  },
  "metadata": {...}
}
```

---

### Task 6: skill_gnucash_mapping_generator
Generate mapping rules (YAML) from extracted patterns.

**Input:** Extractor JSON  
**Output:** `mapping.yaml` with categorized rules

**Usage:**
```bash
python3 skill_gnucash_mapping_generator/agent.py \
  output_extract.json \
  mapping.yaml
```

**Output structure:**
```yaml
_global:
  - patterns: ["pattern1", "pattern2"]
    account: "Expense:Food"
    confidence: high
    reason: "15 occurrences, last 2025-06-10"

ICICI:
  - patterns: [...]
    account: ...
```

---

### Task 7: skill_gnucash_mapping_validator
Test rules against eval set to measure accuracy.

**Input:** 
- Rules JSON
- Extractor JSON  
- Bank statement CSV (eval set)

**Output:** Accuracy report

**Usage:**
```bash
python3 skill_gnucash_mapping_validator/agent.py \
  candidate_rules.json \
  output_extract.json \
  Bank_Statement.csv \
  validation_report.txt
```

**Output:** Accuracy % + mismatch list

---

### Task 8: skill_gnucash_account_mapper
Apply rules to canonical CSV, add Account column.

**Input:** 
- Canonical CSV (from Phase 1 bank skill)
- `mapping.yaml` (from Task 6)

**Output:**
- Mapped CSV with Account column
- Confidence report

**Usage:**
```bash
python3 skill_gnucash_account_mapper/agent.py \
  canonical_input.csv \
  mapping.yaml \
  mapped_output.csv \
  confidence_report.txt
```

**Output CSV columns:**
- All original columns from canonical input
- `Account` — assigned GnuCash account
- `Confidence` — High/Medium/Low/None
- `MatchReason` — why this account was chosen

**Output report:**
- Confidence distribution (%)
- Manual review list (Low/None confidence items)

---

## Full Workflow Example

### Step 1: Extract from historic GnuCash
```bash
python3 src/agents/skill_gnucash_xml_extractor/agent.py \
  ~/Data/MyName/MyGnuCashBook.gnucash \
  extract.json
```

### Step 2: Generate mapping rules
```bash
python3 src/agents/skill_gnucash_mapping_generator/agent.py \
  extract.json \
  mapping.yaml
```

### Step 3: (Optional) Validate rules
```bash
python3 src/agents/skill_gnucash_mapping_validator/agent.py \
  candidate_rules.json \
  extract.json \
  Bank_Statement_EvalSet.csv \
  validation_report.txt
```

Expected: 80%+ accuracy

### Step 4: Map new bank statement
```bash
# First, convert raw statement to canonical using Phase 1 skill
python3 src/agents/skill_icici/agent.py \
  raw_icici_statement.csv \
  canonical_icici.csv

# Then, apply mapping
python3 src/agents/skill_gnucash_account_mapper/agent.py \
  canonical_icici.csv \
  mapping.yaml \
  mapped_icici.csv \
  icici_confidence_report.txt
```

### Step 5: Import into GnuCash
1. Open GnuCash
2. File → Import → CSV
3. Select `mapped_icici.csv`
4. Review account assignments
5. Confirm import

---

## Testing

### Quick Test
```bash
# Test on Khyati data
python3 src/agents/skill_gnucash_xml_extractor/agent.py \
  Data/Khyati/KhyatiAmbani2425.gnucash \
  /tmp/khyati_extract.json

python3 src/agents/skill_gnucash_mapping_generator/agent.py \
  /tmp/khyati_extract.json \
  /tmp/khyati_mapping.yaml
```

---

## File Locations

**Skills:**
- `src/agents/skill_gnucash_xml_extractor/agent.py`
- `src/agents/skill_gnucash_mapping_generator/agent.py`
- `src/agents/skill_gnucash_mapping_validator/agent.py`
- `src/agents/skill_gnucash_account_mapper/agent.py`

**Generated artifacts (reference):**
- `/tmp/khyati_extract.json` — Khyati extractor output
- `/tmp/khyati_mapping.yaml` — Khyati mapping rules
- `/tmp/harshal_mapping.yaml` — Harshal mapping rules (120 rules)

---

## Troubleshooting

### High "No match" rate in confidence report
- Rules may be too specific
- Generate rules from bank statement directly (not GnuCash)
- Add fallback patterns

### Low accuracy in validator
- Eval set descriptions differ from GnuCash descriptions
- Create rules from bank statement instead
- Manually review and refine YAML rules

### Import fails in GnuCash
- Verify Account column values match GnuCash account hierarchy
- Check date/amount formats match expected schema
- Review sample of mapped CSV before importing

---

## Next Steps

1. Test on your GnuCash file
2. Generate mapping rules
3. Validate on eval set
4. Apply to new statements
5. Import into GnuCash
