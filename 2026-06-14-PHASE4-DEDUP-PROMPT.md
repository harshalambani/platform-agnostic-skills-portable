# Phase 4 Lite — Duplicate Detection Integration Prompt

Paste this into a Haiku Cowork session with the project folder mounted.

---

## Context

Project: `platform-agnostic-skills-portable` — a PortableApps-style LLM-agnostic skills app (Gradio UI, PyInstaller frozen build).

The GnuCash import pipeline (`src/agents/skill_gnucash_pipeline/agent.py`) currently runs:
1. Bank extraction (ICICI/BoB/HSBC/HDFC/Other)
2. Canonical CSV conversion (8-column schema: Date, Transaction ID, Description, Account, Deposit, Withdrawal, Balance, Currency)
3. Balance verification (opening balance reconciliation vs GnuCash, running balance check, closing balance check)
4. Account mapping (`skill_gnucash_account_mapper`)

**Missing:** Before Step 4 (account mapping), the pipeline should run duplicate detection against the GnuCash book — flagging CSV rows that already exist as GnuCash transactions so they aren't imported twice. This is critical because the user does batch catch-up across overlapping statement periods.

## What exists

### `src/agents/skill_gnucash_reconciler/agent.py`
A standalone reconciler skill that:
- Parses the .gnucash XML (gzip'd) to extract transactions
- Parses a canonical CSV
- Matches by `(date, round(amount, 2))` — date+amount tuple
- Classifies each CSV row as: "Match" (1 GnuCash hit), "Duplicate" (>1 hit), or "New" (no hit)
- Writes a reconciliation report CSV and summary JSON
- Has both a `GnuCashReconcilerAgent.invoke()` method and a `run()` PA Skills UI entry point
- Currently works as a standalone tab in the UI (category: "gnucash", display_name: "Reconcile")

### `src/agents/skill_gnucash_pipeline/agent.py`
The pipeline already has:
- `_reconcile_opening_balance()` which does Scenario A duplicate skipping (rows dated ≤ GnuCash's last txn date)
- `_get_gnucash_account_balance()` which parses the GnuCash XML for balance info
- Balance verification via `balance_utils.py`

### Pipeline `run()` signature
```python
def run(bank, statement_files, gnucash_file, output_path, config_path=None, model_override=None) -> str:
```

### Reconciler `run()` signature
```python
def run(normalized_csv, gnucash_file, output_path, config_path=None, model_override=None) -> str:
```

## Task

Wire the reconciler's duplicate detection into the pipeline as a new step between balance verification and account mapping. Specifically:

### 1. Add reconciliation step in `skill_gnucash_pipeline/agent.py`

After the balance verification block (around line 698, before "Step 3: Account mapping"), add:

```
Step N — Duplicate detection: comparing against GnuCash book
```

- Import and call the reconciler's `reconcile()` function (not the full `run()` — we don't need file I/O, just the in-memory comparison)
- Parse the GnuCash book using the reconciler's `parse_gnucash_for_reconcile()` 
- Feed it the `canonical_rows` (already loaded in memory at that point)
- From the reconciliation result, count matches/duplicates/new
- **Filter out matched/duplicate rows from canonical_rows** before passing to account mapper — these are already in GnuCash
- Rewrite the canonical CSV on disk (same pattern as the opening balance Scenario A filter on line 686-690)
- Log how many were filtered

### 2. Update the summary output

Add dedup stats to the pipeline's return string. Example:
```
✓ Duplicate check — 15 already in GnuCash (removed), 42 new (will be mapped)
```

### 3. Handle edge cases

- If ALL rows are duplicates, return an early success message: "All N transactions are already in GnuCash — nothing to import."
- If reconciler import fails, catch the exception and proceed without dedup (log a warning)
- The reconciler currently computes `amount = deposit - withdrawal` for matching. The canonical CSV has separate Deposit and Withdrawal columns — make sure the sign is correct.

### 4. Do NOT change

- The standalone Reconcile tab (`skill_gnucash_reconciler`) — it should still work independently
- The balance verification logic (Interventions 1-3) — that stays as-is
- The reconciler's `run()` function — pipeline uses the internal functions directly
- The `skill.yaml` for either skill

### 5. Tests

Check if `tests/test_phase4c_skills.py` or similar has pipeline tests. If so, add a test that verifies:
- Duplicate rows are removed from the output
- The dedup count appears in the summary string
- All-duplicates case returns the early exit message

## Files to modify

- `src/agents/skill_gnucash_pipeline/agent.py` — main change
- `tests/test_phase4c_skills.py` or new test file — add dedup tests

## Files to read (reference only, do not modify)

- `src/agents/skill_gnucash_reconciler/agent.py` — import `parse_gnucash_for_reconcile`, `reconcile`, `parse_csv`
- `src/agents/balance_utils.py` — already imported in pipeline
- `src/agents/skill_gnucash_pipeline/skill.yaml` — for context

## Conventions

- Follow existing code style (logging via `log = logging.getLogger(__name__)`, f-strings, type hints)
- Use the same `log_lines.append()` pattern for progress messages
- File naming: `YYYY-MM-DD-descriptive-name` for any new files
- Before modifying files, outline the plan and wait for approval
- After each major step, briefly summarize what you did
