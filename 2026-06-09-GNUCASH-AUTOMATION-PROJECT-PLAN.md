# GnuCash Accounting Automation — Phased Project Plan

**Created:** 2026-06-09 by Claude (Cowork session)
**Seed doc:** `2026-06-05-gnucash-import-skill-prompt.md` (system-prompt
draft for the first skill — keep, will become the skill's AGENT.md).
**Status:** scoping draft. Phase 1 scaffold landing alongside this doc;
phases 2+ are pure plan.
**Owner:** Harshal.

---

## 1. North star

End state: the user can take **any** raw bank/financial data (PDF
statement, CSV download, pasted text, OFX) and end up with **categorised,
reconciled entries inside GnuCash** with minimal manual intervention.
The pipeline runs locally — no cloud, no SaaS — using the existing PA
Skills Portable LLM-agnostic architecture.

In one line: **"Drop in a statement, get cleanly-categorised GnuCash
entries out the other side."**

## 2. Scope decomposition

Five capability layers, each potentially its own skill:

| Layer | What it does | Why it's separate |
|---|---|---|
| **Ingest** | Pull rows out of varied source formats (PDF, CSV, XLSX, OFX, pasted text). | Already partially covered by `skill_hsbc`, `skill_bob`, `skill_26as` for specific banks. Generic ingest = harder. |
| **Normalise** | Canonical schema: `Date,TxnID,Description,Account,Deposit,Withdrawal,Balance` (ISO dates, period decimals, scrubbed descriptions). | Output of every ingest path; input to everything downstream. |
| **Categorise** | Map descriptions → GnuCash account tree via user rules + LLM suggestions for unmatched. | Requires reading the user's account tree; rules can be re-used across statements. |
| **Reconcile** | Detect duplicates against existing GnuCash entries; verify running balances; flag anomalies. | Needs read access to the GnuCash file/DB. |
| **Write back** | Produce CSV/QIF for GnuCash's importer, OR write directly to the GnuCash file (XML / SQLite). | CSV/QIF is safe and supported by all GnuCash versions; direct write is faster but riskier. |

## 3. Phases

### Phase 1 — `skill_gnucash_import` (CSV-out, single-statement)

**Scope:** Ingest CSV/XLSX/pasted text → normalised CSV output that
GnuCash's CSV importer accepts.

* Direct mode (no tool-calling). LLM does the schema-mapping work
  under a tight system prompt (the existing 2026-06-05 prompt).
* Inputs: one CSV/XLSX/.txt file. Dropdown: source-bank hint (Auto,
  HDFC, SBI, ICICI, HSBC, BoB, Other). Optional account name string
  to populate the Account column.
* Outputs: one CSV in the canonical schema.
* PDF support **deferred** — too much variability for a single
  generic skill; existing bank-specific PDF skills already cover the
  big ones.
* QIF output **deferred** — Phase 2.

**Scaffolded in this session:** `src/agents/skill_gnucash_import/`
with `skill.yaml`, `AGENT.md`, `agent.py` skeleton. Wired to the
registry by virtue of folder placement. **No tested business logic
yet** — the agent.py plumbs the file → LLM → output flow on top of
`run_direct()`, and the AGENT.md is the existing prompt verbatim.

**Definition of done:**
* Runs end-to-end on at least 2 different real bank CSVs (HDFC + SBI
  recommended — they have the most distinctive layouts).
* Output passes GnuCash 5.x's CSV importer without manual column
  re-mapping.
* Unit tests for the few deterministic helpers we add (date format
  detection, description cleanup) — LLM behaviour itself goes in
  e2e tests.

### Phase 2 — QIF output + PDF ingest

Two parallel sub-features bolted onto Phase 1's skill:

* **QIF emission**: add a Format dropdown (CSV/QIF) to the input
  schema, emit a QIF file conforming to the spec in the existing
  prompt. ~half-day.
* **PDF ingest**: route `.pdf` inputs through pdfplumber/qpdf first
  (already in the build), then hand the extracted text to the same
  LLM step. Pre-deduplicates repeated page headers. ~half-day to a
  day depending on PDF variance.

### Phase 3 — `skill_gnucash_account_mapper`

**Scope:** Reads the user's GnuCash account tree (from a `.gnucash`
XML file or a flat list they paste), produces a mapping rule file
(YAML or JSON) the Phase 1 skill consumes to populate the Account
column automatically.

* Agent mode (tool-calling): needs to walk a tree, ask follow-up
  questions, persist rules.
* Output: a per-account-tree `mapping_rules.yaml` saved under
  `Data/gnucash/`.
* Phase 1's skill grows an optional input: "Mapping rules file (auto)"
  pointing at this YAML. Unmatched descriptions stay blank for the
  user to assign at import time (matches the existing prompt
  contract).

### Phase 4 — `skill_gnucash_reconciler`

**Scope:** Given a normalised CSV and an existing GnuCash file, flag:
* Duplicates already in GnuCash (date + amount + description match).
* Balance discontinuities.
* Missing transactions (gaps in date or running-balance arithmetic).

Reads-only — never writes back in this phase.

### Phase 5 — Direct GnuCash file write (`skill_gnucash_write`)

**Scope:** Replace CSV-importer round-trip with direct write to the
user's `.gnucash` XML (gzipped XML format only — SQLite backend is
out of scope for v1).

* High-risk: a bug here corrupts the user's books.
* Required guards: dry-run mode by default, mandatory file backup,
  diff preview before commit, GnuCash file version check.
* Probably warrants a separate "danger zone" UI affordance.

### Phase 6 — Orchestrator agent

**Scope:** A meta-skill that chains 1+3+4 (and optionally 5) into one
"drop your statement here" flow. Likely a top-level Cowork skill
rather than a registry skill — it spawns or invokes the others.

### Phase 7 — Recurring-transaction detection and forecast

**Scope:** From historical data, identify recurring entries (salary,
rent, EMIs, subscriptions), predict next occurrence, surface in the
UI as a forecast view.

Pure analysis — touches only the existing GnuCash data, doesn't
ingest anything new.

## 4. Sequencing recommendation

Pick **either**:

* **Path A (depth-first, fast user value):** Phase 1 → Phase 2 →
  small Phase 3 (rules can start as a hand-written YAML, no skill
  needed yet) → ship to user use. Adds Phase 3-as-a-skill once the
  format stabilises.
* **Path B (breadth-first, robustness):** Phase 1 → Phase 4 → Phase 3.
  Reconciliation before mapping. Lower wow factor but catches bad
  data sooner.

**Default recommendation: Path A.** Phase 1 already gets the user
unblocked for most of their actual import workflow; Phases 2–3
multiply the value without architectural risk.

## 5. Open questions (TBD)

1. **Does the user already maintain a GnuCash file?** If yes — what
   format (XML, gzipped XML, SQLite)? Where on disk? This drives the
   Phase 3/4/5 design and decides whether direct file writes (Phase 5)
   are remotely realistic.
2. **Which banks/accounts dominate the user's flow?** Phase 1's
   Auto-detect hint will be more useful if we know the top 3.
3. **Date format ambiguity policy.** The seed prompt says "always
   confirm DD/MM vs MM/DD". For a non-interactive batch flow that
   guidance is wrong; user needs to either (a) set a per-bank default
   in a config file, or (b) accept that ambiguous CSV rows get a flag
   row in the output. Pick one before Phase 1 ships.
4. **Account-tree ingestion format.** Phase 3 input: parse the
   `.gnucash` XML directly, or have the user paste/export a flat list?
   First is robust; second is faster to ship.
5. **Multi-currency.** Out of scope or in? The seed prompt mentions
   FX preservation but doesn't commit to FX gain/loss handling.
   Default assumption: single base currency for v1.
6. **GnuCash version target.** 5.x (current stable) is the default.
   Anything older opens up CSV importer quirks. **TBD** which the
   user actually runs.
7. **Storage location for outputs.** `outputs/<skill>/<timestamp>/`
   matches the existing pattern. Confirm — and decide whether the
   user wants the output to land somewhere closer to their GnuCash
   file by default.
8. **Test data.** Need 2–3 real (anonymised) statements for Phase 1
   validation. Synthetic CSVs are OK for unit tests but won't catch
   real-world description-format variation.
9. **Streaming progress.** The existing streaming wrapper supports
   agent-mode only. Phase 1 is direct-mode — needs the simpler
   `run_with_progress` path. Confirm no UX regression.
10. **Privacy.** All processing local. But do we want a "scrub before
    sending to LLM" pre-pass for account numbers / customer IDs?
    The LLM in this app is user-configured (could be local Ollama,
    could be a hosted endpoint). Sensible default: scrub by default,
    let the user opt out.

## 6. What lands in this session vs later

**This session:**
* This plan doc.
* `src/agents/skill_gnucash_import/skill.yaml` (registry hook).
* `src/agents/skill_gnucash_import/AGENT.md` (the seed prompt content
  with a thin header — minimal change, mostly the existing text).
* `src/agents/skill_gnucash_import/agent.py` (skeleton — file read,
  call `run_direct`, write output; no parsing logic yet).
* **No tests, no commit.** Scaffold sits in working tree for review.

**Next session (Phase 1 finishing):**
* Add CSV/XLSX-aware reading (use `xlsx` skill helpers or pandas).
* Add the bank-hint dropdown wiring through `skill.yaml` → tab → agent.
* Smoke-test against real statements.
* Unit + e2e tests.
* Commit Phase 1.

## 7. Risks

1. **LLM determinism.** Direct-mode CSV transformation is sensitive
   to model temperature and prompt drift. Lock the temperature at 0
   and watch for column-ordering drift in tests.
2. **Description scrubbing too aggressive.** The "trim trailing codes"
   rule in the prompt can eat cheque numbers or merchant IDs. Counter
   by adding explicit "preserve N-digit reference numbers" examples
   in the prompt.
3. **GnuCash importer quirks.** Empty `Deposit`/`Withdrawal` columns
   sometimes need to be 0 instead of blank, depending on GnuCash
   version. Test against the target version before declaring Phase 1
   done.
4. **Long statements.** A 12-month statement may exceed the LLM
   context window. Phase 1 should split into chunks if needed and
   recombine outputs — but that adds enough complexity to defer to
   Phase 2.

## 8. Out of scope (explicitly)

* Tax categorisation, GST/VAT handling.
* Multi-entity bookkeeping.
* Investment account import (different schema entirely).
* Receipt OCR.
* Two-way sync with a bank API.
