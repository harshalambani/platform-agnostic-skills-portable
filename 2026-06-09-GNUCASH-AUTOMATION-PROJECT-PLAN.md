# GnuCash Accounting Automation — Phased Project Plan

**Created:** 2026-06-09 by Claude (Cowork session)
**Seed doc:** `2026-06-05-gnucash-import-skill-prompt.md` (system-prompt
draft for the first skill — keep, will become the skill's AGENT.md).
**Status:** v3 — revised 2026-06-11 (second pass, same day).
v2 changes: spec-then-transform architecture; multi-file batch input;
dedup in Phase 4 lite; deterministic Phase 3 extraction; risks +
bank-quirks sections.
v3 changes: real per-person test corpus landed in `Data/<Person>/`
(see §5 TBD #8 and §8) — legacy `.xls` ingest added to Phase 1;
no-text-layer print-to-PDF class (OCR) added to Phase 2 and raises its
priority; Karnataka Bank is bank #6; Khyati's hand-labelled
"Nature of exp" column becomes the Phase 3 evaluation set; mapping
file path pattern now per-person.
v4 changes (2026-06-11, third pass): per-bank description-scrub rules
in Phase 1 (config-driven, harvested from existing bank skills; keys
extracted from the RAW description before scrubbing); Phase 3 mapper
training scoped per source bank account with book-wide fallback.
Both changes address gaps confirmed in the current build
(`cleanup_description()` is generic; `parse_gnucash_file()` trains
book-wide).
v5 changes (2026-06-11, fourth pass): ICICI carved out of the generic
skill into its own dedicated deterministic skill (`skill_icici`,
Phase 1b) following the proven HSBC/BoB pattern — ICICI proved too
tricky for the generic path.
v6 changes (2026-06-12): `skill_icici` BUILT (852 lines, deterministic,
post-validated). Checkpoint finding: NONE of the fine-tuned bank
skills (HSBC → enriched xlsx, BoB → native columns, ICICI → native
5-column CSV) emit the canonical schema, so none currently feed
Phases 3/4/6. New Phase 1c: thin ingest adapters that map each bank
skill's existing output to the canonical schema — reusing, not
rewriting, the fine-tuned skills.
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
| **Normalise** | Canonical schema: `Date,TxnID,Description,Account,Deposit,Withdrawal,Balance,Currency` (ISO dates, period decimals, scrubbed descriptions, ISO 4217 currency code). | Output of every ingest path; input to everything downstream. |
| **Categorise** | Map descriptions → GnuCash account tree via user rules + LLM suggestions for unmatched. | Requires reading the user's account tree; rules can be re-used across statements. |
| **Reconcile** | Detect duplicates against existing GnuCash entries; verify running balances; flag anomalies. | Needs read access to the GnuCash file/DB. |
| **Write back** | Produce CSV/QIF for GnuCash's importer, OR write directly to the GnuCash file (XML / SQLite). | CSV/QIF is safe and supported by all GnuCash versions; direct write is faster but riskier. |

## 3. Phases

### Phase 1 — `skill_gnucash_import` (CSV-out)

**Scope:** Ingest CSV/XLSX/pasted text → normalised CSV output that
GnuCash's CSV importer accepts.

* **Architecture — spec-then-transform (REVISED 2026-06-11):**
  1. **Deterministic pre-parse:** read the file with csv/pandas;
     strip preamble/legend junk rows; isolate the transaction table.
  2. **LLM sees only a sample** (header + ~20 rows) and emits a
     column-mapping spec as JSON: which column is the date and its
     format, description column, debit/credit convention (separate
     columns vs a Dr/Cr indicator), balance column, currency.
  3. **Code applies the spec deterministically to ALL rows.**
  4. **Deterministic post-validation:** row count in == row count
     out; deposit/withdrawal sums consistent with the balance
     movement; every date parses; every amount is numeric.

  Rationale: full-LLM row transformation risks silently dropped or
  hallucinated rows (data corruption that goes unnoticed), exceeds
  context on long statements, and is unreliable on small local
  Ollama-class models. A small model *can* classify ~8 columns from
  a sample; it *cannot* reliably transform 800 rows. Full direct-mode
  transformation (original design, the 2026-06-05 prompt) is retained
  only as a fallback for pasted unstructured text — and the
  post-validation step is mandatory on that path too.
* Number parsing must handle Indian digit grouping (lakh/crore:
  `1,23,456.78`) and Dr/Cr indicator columns in the deterministic
  transform.
* Inputs: **one or more** CSV/XLSX/**XLS**/.txt files. Multi-file
  batch is the user's actual workflow (backlog catch-up across 6
  banks × multiple GnuCash files) — do not defer batching to
  Phase 6. Dropdown: source-bank hint (Auto, HDFC, SBI, ICICI, HSBC,
  BoB, Kotak, Karnataka Bank, Other). Optional account name string
  to populate the Account column.
* **Legacy .xls support (ADDED 2026-06-11, v3):** ICICI and HDFC
  exports in the real corpus are CDFV2 binary .xls, not xlsx.
  Pre-parse converts via `libreoffice --headless --convert-to csv`
  (verified working on all three corpus .xls files) or xlrd if
  bundling it is cheaper. Conversion is part of step 1
  (deterministic pre-parse), before any sampling.
* **Per-bank description-scrub rules (ADDED 2026-06-11, v4):**
  statement descriptions carry bank-specific repetitive junk on
  every line (e.g. ICICI `BIL/ONL/`, `BIL/INFT/` prefixes and
  trailing slash-padding; HDFC narration codes; BoB/HSBC patterns
  already handled inside their dedicated skills). Replace the single
  generic `cleanup_description()` with a config-driven scrubber:
  `bank_scrub_rules.yaml`, keyed by `bank_hint` (same lookup pattern
  as `bank_date_formats.yaml`), each entry a list of regex
  strip/replace rules plus an optional "extract to Extra Info" list.
  **Harvest the patterns already proven in `skill_hsbc` and
  `skill_bob` into this config** rather than re-inventing them.
  Ordering rule (critical): Phase 3 matching keys (UPI VPA,
  NEFT/IMPS counterparty, reference numbers) are extracted from the
  RAW description BEFORE scrubbing — the scrubbed text goes to the
  output CSV, the keys go to the mapper. Scrub rules can therefore
  be aggressive about display junk without endangering matching.
* Outputs: one canonical-schema CSV per input file.
* Prompt split: a new spec-generation prompt becomes the primary
  AGENT.md content; the existing 2026-06-05 full-transform prompt is
  retained as the pasted-text fallback.
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
* Runs end-to-end on at least 2 different real bank CSVs (ICICI + HDFC
  recommended — the user's top-volume banks per TBD #2).
* Output passes GnuCash 5.x's CSV importer without manual column
  re-mapping — after the user saves an importer preset once per
  format (document this one-time step; it is part of DoD).
* Post-validation passes on every test file: row counts match, sums
  reconcile against balance movement, all dates/amounts parse.
* Unit tests for the deterministic transform and validators (date
  format detection, Indian number parsing, Dr/Cr handling,
  description cleanup) — LLM spec-generation behaviour goes in
  e2e tests, snapshot-testing the spec JSON per bank sample.

### Phase 1b — `skill_icici` (dedicated ICICI transformer) — ADDED v5

**Scope:** ICICI legacy .xls → canonical 8-column CSV, as a
standalone bank-specific skill following the proven `skill_hsbc` /
`skill_bob` pattern. ICICI is carved OUT of the generic Phase 1
skill — its quirks proved too tricky for the generic
spec-then-transform path to handle reliably.

* **Fully deterministic — no LLM step.** The format is fixed
  (JasperReports CDFV2 export), so hardcode it like HSBC/BoB:
  LibreOffice-headless conversion, strip ~12-row preamble +
  ~28-row legend tail, drop the leading empty column, parse
  `"01,Apr,2024"` quoted DD,Mon,YYYY dates, use Transaction Date
  (not Value Date), handle the ~50-char remark truncation.
* ICICI-specific description scrubbing (the `BIL/ONL/`, `BIL/INFT/`,
  `UPI/` prefix families, trailing slash-padding) lives INSIDE this
  skill — consistent with where HSBC/BoB keep theirs — rather than
  in `bank_scrub_rules.yaml`. Keys for Phase 3 matching are still
  extracted from the raw description before scrubbing (v4 ordering
  rule applies here too).
* Same canonical output schema as Phase 1, so Phases 3/4/6 consume
  it unchanged. The generic skill's `bank_hint` dropdown keeps
  "ICICI" but routes to / recommends this skill.
* Both corpus variants are the test set: `icici.xls` (full FY2024–25,
  ~490 txns, primary) and `OpTransactionHistory...xls` (5-day,
  secondary). DoD: both transform with zero BAD_ROWs, post-validation
  passes (row counts, balance arithmetic), output imports into
  GnuCash 5.x cleanly.
* Generic Phase 1 DoD adjusts: its two real-data test banks become
  HDFC (Harshal's + Khyati's files) — ICICI moves to Phase 1b.

### Phase 1c — Bank-skill ingest adapters (ADDED v6, 2026-06-12)

**Scope:** make the existing fine-tuned bank skills first-class
citizens of the GnuCash pipeline WITHOUT touching their proven
internals. One thin adapter per skill converts its native output to
the canonical 8-column schema:

| Source skill | Native output | Adapter work |
|---|---|---|
| `skill_hsbc` | enriched .xlsx workbook | read txn sheet; map TxnID/date/description/amounts; Currency=INR |
| `skill_bob` | native CSV (`DATE, PARTICULARS, CHQ.NO., WITHDRAWALS, DEPOSITS, BALANCE`) | rename + ISO dates (DD-MM-YY); Balance carries through |
| `skill_icici` | native 5-col CSV (`Value Date, Cheque Number, Transaction Remarks, Withdrawal, Deposit`) | rename + ISO dates; see discrepancies below |

* Adapters are pure deterministic column/format mapping — all
  scrubbing stays inside the bank skills where it was fine-tuned.
  Account column is added empty (Phase 3 fills it); Currency
  defaults to INR.
* **skill_icici discrepancies to resolve in the build session
  (CHECKPOINT 2026-06-12):**
  1. Output header says `Value Date` — plan decided Transaction
     Date is authoritative. Verify which date actually populates
     the column; fix or document.
  2. Output drops the Balance column — Phase 4 lite's balance
     verification needs it. Either emit it (source has it) or have
     the reconciler read balances from the original statement.
  3. No Account/Currency columns — adapter adds them, or extend
     the skill's writer to emit canonical directly (it's new code,
     low risk — preferred over an adapter for this one).
* With 1b+1c done, the generic Phase 1 skill's real-data obligation
  is HDFC only; everything else routes through dedicated skills +
  adapters. The orchestrator (Phase 6) chains: bank skill → adapter
  → mapper → reconciler.

### Phase 2 — QIF output + PDF ingest

Two parallel sub-features bolted onto Phase 1's skill:

* **QIF emission**: add a Format dropdown (CSV/QIF) to the input
  schema, emit a QIF file conforming to the spec in the existing
  prompt. ~half-day.
* **PDF ingest** — two classes (REVISED 2026-06-11, v3):
  1. *Text-layer PDFs* (e.g. BoB "Transaction Details"): route
     through pdfplumber/qpdf (already in the build), then hand the
     extracted text to the same spec/transform step.
     Pre-deduplicates repeated page headers. ~half-day.
  2. *No-text-layer print-to-PDF* (CONFIRMED in corpus: Kiran's and
     Vaikunth's HDFC statements, "Microsoft: Print To PDF" producer,
     text drawn as vector outlines — pdftotext extracts NOTHING):
     requires render-to-image (pdftoppm) + OCR (Tesseract — already
     bundled in the build). This is the ONLY route to Kiran's and
     Vaikunth's HDFC data, which makes OCR ingest a required
     feature, not an extra. ~1–2 days including table-structure
     recovery from OCR text.
* **Passbook-photo ingest (NEW, optional):** Khyati's Karnataka Bank
  data exists only as phone photos of dot-matrix passbook pages
  (rotated, skewed). Same OCR machinery as class 2 plus
  deskew/rotation. Low volume (locker rent + SB interest entries) —
  defer behind classes 1–2; manual entry is a fine interim answer.

### Phase 3 — `skill_gnucash_account_mapper`

**Scope:** Reads the user's GnuCash account tree (from a `.gnucash`
XML file or a flat list they paste), produces a mapping rule file
(YAML or JSON) the Phase 1 skill consumes to populate the Account
column automatically.

* Agent mode (tool-calling): needs to walk a tree, ask follow-up
  questions, persist rules.
* **Per-file scoping (critical):** each GnuCash file has its own
  account tree and naming conventions (e.g. Harshal's file vs a family
  member's file). Mapping rules are therefore **per-GnuCash-file**,
  not global. Output path follows the per-person Data layout
  (REVISED 2026-06-11, v3):
  `Data/<Person>/<gnucash_filename>.mapping.yaml`
  (e.g. `Data/Harshal/MyFinances2425.mapping.yaml`). All four
  family books exist and are valid gzipped XML: Harshal (13.0 MB
  uncompressed), Khyati (9.4 MB), Kiran (4.8 MB), Vaikunth (2.6 MB).
* Phase 1's skill grows an optional input: "Mapping rules file (auto)"
  pointing at this YAML. Unmatched descriptions stay blank for the
  user to assign at import time (matches the existing prompt
  contract).
* The mapping YAML should be human-editable — the user may want to
  tweak rules manually after the skill generates them.
* **Deterministic extraction (REVISED 2026-06-11):** historical
  description→account mappings are extracted from the gzipped XML
  with code, NOT the LLM — 7,949 transactions is trivially parseable,
  and deterministic extraction keeps confidence scores honest. The
  LLM's only job is suggesting accounts for descriptions that have
  no historical match.
* **Per-bank-account training scope (ADDED 2026-06-11, v4):**
  description patterns are bank-specific — an ICICI statement's
  descriptions look like the ICICI account's history in GnuCash,
  not like HDFC's. During extraction, record each transaction's
  SOURCE bank account (the asset-side split) alongside the dominant
  expense/income split, and partition mapping rules by source
  account. The mapping YAML gains a top-level key per source bank
  account plus a `_global` partition (book-wide union). At
  classification time the statement's target bank account (the
  Phase 1 "account name" input — it doubles as the partition
  selector) picks the partition; rules from the matching partition
  score normally, `_global`-only matches cap at Medium confidence
  with Reason "cross-bank match". This requires changing the current
  build's `parse_gnucash_file()`, which trains book-wide and
  discards the source split.
* **Key-based matching, not full-string:** statement descriptions
  and GnuCash-stored descriptions will not be string-identical.
  Tokenise both sides to stable keys: UPI VPA (`merchant@bank`),
  NEFT/IMPS counterparty name, card merchant ID. UPI rows dominate
  Indian statements, so VPA extraction is the single
  highest-leverage matching feature in the whole project.
* **Multi-split transactions:** a transaction with 3+ splits has no
  single description→account mapping. Policy: take the largest
  non-source split; if no split dominates (>70% of the amount), skip
  the transaction during extraction.
* **Recency × frequency weighting (REVISED 2026-06-11):** pure
  "most recent wins" is too blunt — one mis-categorised recent
  transaction would override 50 consistent older ones. Score each
  candidate mapping by frequency × recency decay (last 12 months
  weighted highest). When recency and frequency disagree, emit the
  mapping at Medium confidence rather than silently picking one.
  Stale mappings (no match in last 2 years) get flagged for review
  rather than applied silently.
* **Mapping validation:** on every run, validate the YAML's target
  accounts against the current account tree — account renames in
  GnuCash silently invalidate rules otherwise. Invalid targets are
  reported, never silently dropped.
* **Labelled evaluation set (NEW 2026-06-11, v3):** Khyati's HDFC
  .xls carries a hand-maintained `Nature of exp` column — ~900 rows
  of human description→category labels. Use it as ground truth to
  MEASURE mapper accuracy (e.g. "X% of rows match the human label
  at High confidence") rather than eyeballing. Define the accuracy
  bar before building; report against it in Phase 3's DoD.
* **Confidence report:** Every auto-classified row produces a
  confidence score or tier (High / Medium / Low). Output is a sidecar
  report (e.g. `<output_name>.confidence.csv`) alongside the main
  GnuCash-importable CSV, with columns:
  `Row,Description,Assigned Account,Confidence,Reason`
  — where Reason explains the basis (e.g. "exact keyword match",
  "recency-weighted historical match", "LLM suggestion — no rule
  match"). This lets the user sort by Low confidence and focus manual
  review there, rather than eyeballing every row.
  High = exact rule match or strong historical pattern.
  Medium = fuzzy/partial match, older historical data, or
  recency/frequency disagreement.
  Low = LLM best-guess with no supporting rule.

### Phase 4 — `skill_gnucash_reconciler`

**Scope:** Given a normalised CSV and an existing GnuCash file, flag:
* Duplicates already in GnuCash (date + amount + description match).
* Balance discontinuities.
* Missing transactions (gaps in date or running-balance arithmetic).

Reads-only — never writes back in this phase. Check for GnuCash's
`.LCK` lock file before reading: if GnuCash has the book open, the
on-disk XML may be stale relative to unsaved changes — warn the user
to save (or close GnuCash) first.

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

## 4. Sequencing — DECIDED (2026-06-11)

Revised based on user's actual workflow (batch catch-up across 5
banks, imports via GnuCash GUI, multiple GnuCash files for family
members):

1. **Phase 1** — generic CSV formatting. Get HDFC exports into
   GnuCash-importable shape with zero column remapping needed.
   Existing HSBC and BoB skills already cover those two banks.
   Kotak deprioritised (no sample, 2026-06-11).
1b. **Phase 1b** — `skill_icici` (v5): dedicated deterministic ICICI
   transformer, HSBC/BoB pattern. **BUILT (2026-06-12)** — pending
   the three discrepancy fixes in Phase 1c.
1c. **Phase 1c** — ingest adapters (v6): canonical-schema adapters
   for skill_hsbc / skill_bob / skill_icici so the fine-tuned skills
   feed Phases 3/4/6. Small, deterministic, high leverage — do this
   BEFORE Phase 3, since the mapper's input contract depends on it.
2. **Phase 3** — Account mapping. Highest value-add: pre-populate the
   Account column so GnuCash auto-assigns most entries. Per-GnuCash-
   file scoping (each family member has their own `.gnucash` file with
   different account trees).
3. **Phase 4 lite** — Balance check PLUS duplicate detection
   (date + amount match against existing GnuCash entries). Dedup
   moved up from full Phase 4 (REVISED 2026-06-11): batch catch-up
   across overlapping statement periods is exactly where duplicates
   occur, so it cannot wait. Read-only.
4. **Phase 2** — PDF ingest + QIF output. **RESOLVED (2026-06-11):**
   backlog is ~1 year (annual catch-up cycle) — at the edge of
   typical 6–12-month rolling CSV download windows. Mitigation:
   **download all 12 months of CSVs from all banks NOW**, before
   build work starts, to freeze the risk.
   **UPDATED (v3):** the real corpus shows Kiran's and Vaikunth's
   HDFC data exists ONLY as no-text-layer print-to-PDF — OCR ingest
   (Phase 2 class 2) is the sole route to two of the four family
   books. Phase 2 stays at position 4 for Harshal's and Khyati's
   books, but must land before Kiran's and Vaikunth's books can be
   processed — unless fresh CSV/XLS downloads can be obtained for
   their HDFC accounts (preferred; try that first, it's cheaper
   than OCR).
5. **Phase 5** — ~~Direct GnuCash file write~~ **DEPRIORITISED.**
   User explicitly prefers GnuCash's import GUI as a safety layer.
   Remains documented but off the active roadmap.
6. **Phase 6** — Orchestrator (chains 1+3+4 into one flow).
7. **Phase 7** — Recurring-transaction detection.

## 5. Open questions — all RESOLVED (2026-06-11)

1. **Does the user already maintain a GnuCash file?**
   **RESOLVED:** Yes. Gzipped XML at
   `Data/MyFinances2425.gnucash` (237 accounts, 7 949 transactions,
   XML book version 2.0.0). This confirms Phases 3/4/5 are viable —
   the file is the standard gzipped-XML format used by GnuCash 2.6+.
   Phase 5 (direct write) targets this format only; SQLite backend
   remains out of scope.

2. **Which banks/accounts dominate the user's flow?**
   **RESOLVED:** Five banks, priority order: ICICI, HSBC, HDFC,
   Bank of Baroda, Kotak Bank. Phase 1 testing must cover at least
   ICICI + HDFC CSVs. **Follow-on:** add "Kotak" to the `bank_hint`
   dropdown in `skill.yaml` (currently missing).

3. **Date format ambiguity policy.**
   **RESOLVED: Per-bank default in a config file.**
   Add a `bank_date_formats.yaml` (or a `[date_formats]` section in
   the existing `config.yaml`) mapping bank names to their date format:
   ```yaml
   date_formats:
     ICICI: "DD,Mon,YYYY"    # VERIFIED 2026-06-11: "01,Apr,2024"
     HSBC: "DD/MM/YYYY"
     HDFC: "DD/MM/YY"        # VERIFIED 2026-06-11
     "Bank of Baroda": "DD-MM-YYYY"
     Kotak: "DD/MM/YYYY"     # unverified — deprioritised
     "Karnataka Bank": "DD-MM-YY"
     _default: "DD/MM/YYYY"
   ```
   The `bank_hint` dropdown value is the lookup key. `_default` is the
   fallback for "Auto-detect" and "Other". User edits this file once;
   no runtime ambiguity prompts. The AGENT.md batch-mode rule
   ("prefer DD/MM/YYYY") stays as the LLM's own fallback if the config
   is absent.

4. **Account-tree ingestion format.**
   **RESOLVED: Both — XML parse primary, paste fallback.**
   Phase 3 tries to parse the `.gnucash` gzipped-XML file first
   (walk `<gnc:account>` elements, reconstruct the colon-delimited
   hierarchy). If that fails or no file is provided, accept a
   pasted/flat-list input. This is safe given the confirmed file
   format (TBD #1).

5. **Multi-currency.**
   **RESOLVED: Add a `Currency` column to the output schema.**
   Canonical schema becomes 8 columns:
   `Date,Transaction ID,Description,Account,Deposit,Withdrawal,Balance,Currency`
   `Currency` contains the ISO 4217 code (e.g. `INR`, `USD`). For
   single-currency statements it's a constant column; for FX
   transactions it preserves the original currency. **No FX gain/loss
   calculation** — that's GnuCash's job at import time.
   **Ripple effects:** AGENT.md prompt, agent.py output, and the
   GnuCash CSV importer column mapping all need updating when Phase 1
   business logic lands. Downstream phases (3–6) must carry the column
   through.

6. **GnuCash version target.**
   **RESOLVED: GnuCash 5.x (current stable, 5.14/5.15 as of
   2026-06-11).** The file's XML book version (2.0.0) is compatible
   with all 5.x releases. No need to support 4.x.

7. **Storage location for outputs.**
   **RESOLVED: User-configurable, default to existing pattern.**
   Add an `output_dir` key to `config.yaml`. Default value:
   `outputs/{skill_name}/{timestamp}/` (the existing convention).
   User can override to e.g. `Data/gnucash_imports/` to land output
   near their `.gnucash` file. `agent.py` reads this at runtime and
   falls back to the standard path if unset.

8. **Test data.**
   **RESOLVED (UPDATED 2026-06-11, v3): real per-person corpus
   provided in `Data/<Person>/`** — un-anonymised, which is fine
   given the all-local constraint (data never leaves the machine
   unless the user points the LLM endpoint off-box). Inventory:

   | Person | File | Bank / format | Phase |
   |---|---|---|---|
   | Harshal | `icici.xls` | ICICI full FY2024–25 (~490 txns), legacy .xls (JasperReports CDFV2); leading empty column; ~12 preamble rows; ~28-row legend tail; **primary skill_icici test file** | 1b |
   | Harshal | `OpTransactionHistory18-04-2026...xls` | ICICI, same format, 5-day sample (14–18 Apr 2026) — secondary | 1b |
   | Harshal | `Acct Statement_0125_...xls` | HDFC, legacy .xls; 20-row preamble; header at row 21 | 1 |
   | Harshal | `2026-04-19-HSBC-...xlsx` | HSBC, output of existing skill | covered |
   | Harshal | `76000100001791.pdf` | BoB Transaction Details, text layer OK | covered (skill_bob) |
   | Khyati | `Bank Stmnt Khyati FY2425.xls` | HDFC .xls + hand-labelled `Nature of exp` column (~900 rows) | 1 + Phase 3 eval set |
   | Khyati | `IMG-...WA000x.jpg` ×2 | Karnataka Bank passbook photos (rotated, dot-matrix) | 2 (optional photo-OCR) |
   | Kiran | `KIRAN V AMBANI.pdf` | BoB, text layer OK | covered (skill_bob) |
   | Kiran | `kiran.pdf` | HDFC print-to-PDF, 9 pp, NO text layer | 2 (OCR required) |
   | Vaikunth | `VAIKUNTH MANOHARLAL AMBANI.pdf` | BoB, text layer OK | covered (skill_bob) |
   | Vaikunth | `vaikunth ambani.pdf` | HDFC print-to-PDF, 3 pp, NO text layer | 2 (OCR required) |
   | All four | `<Name>Ambani2425.gnucash` / Khyati's | valid gzipped XML books | 3/4 |

   **This corpus is the Phase 1/1b/2/3 test bed.** Phase 1 (generic)
   DoD files: Harshal's HDFC .xls + Khyati's HDFC .xls. Phase 1b
   (`skill_icici`) DoD files: `icici.xls` (full FY, primary) +
   `OpTransactionHistory...xls` (secondary). **Kotak: DEPRIORITISED
   (2026-06-11)** — no sample available and user has deprioritised
   it; stays in the dropdown but carries no test obligation until a
   sample exists.

9. **Streaming progress.**
   **RESOLVED: Use `run_with_progress` (direct-mode path).**
   Already proven by `skill_summarize` and other direct-mode skills.
   Shows a spinner + "Processing…" status. No streaming of partial
   CSV tokens to the UI (that would risk the user seeing/copying
   incomplete output). Zero new code required.

10. **Privacy.**
    **RESOLVED: Deferred to Phase 2+.**
    Phase 1 ships without a scrubbing pre-pass. Rationale: scrubbing
    risks eating cheque numbers, transaction references, and merchant
    IDs that the LLM needs for correct schema mapping. The user's LLM
    is self-configured (local Ollama or a hosted endpoint they chose),
    so the privacy posture is already a user decision. A scrub-before-
    send feature can be added in Phase 2 once the schema is stable and
    we can define safe regex patterns that don't break the transform.

## 6. What lands in this session vs later

**This session (2026-06-09):**
* This plan doc.
* `src/agents/skill_gnucash_import/skill.yaml` (registry hook).
* `src/agents/skill_gnucash_import/AGENT.md` (the seed prompt content
  with a thin header — minimal change, mostly the existing text).
* `src/agents/skill_gnucash_import/agent.py` (skeleton — file read,
  call `run_direct`, write output; no parsing logic yet).
* **No tests, no commit.** Scaffold sits in working tree for review.

**Next session (Phase 1 finishing):**
* Implement spec-then-transform: deterministic pre-parse → sample →
  LLM spec JSON → deterministic row transform → post-validation.
* Split the prompts: new spec-generation prompt (primary AGENT.md);
  existing 2026-06-05 full-transform prompt kept as the pasted-text
  fallback.
* Add CSV/XLSX-aware reading (use `xlsx` skill helpers or pandas).
* Add the bank-hint dropdown wiring through `skill.yaml` → tab → agent.
* Multi-file input support.
* Smoke-test against real statements.
* Unit + e2e tests (including post-validation checks and spec-JSON
  snapshots per bank).
* Commit Phase 1.

## 7. Risks (REVISED 2026-06-11)

1. **Silent row loss/hallucination — top risk.** Any LLM step that
   touches row data can drop or invent rows: data corruption that
   goes unnoticed until reconciliation. Mitigated structurally by
   spec-then-transform (the LLM never emits row data on the main
   path) plus mandatory post-validation (row counts, sum checks).
   The pasted-text fallback path still carries this risk —
   post-validation is non-optional there.
2. **LLM determinism.** Spec generation is sensitive to temperature
   and prompt drift. Lock temperature at 0; snapshot-test the spec
   JSON for each bank's sample file.
3. **Description scrubbing too aggressive.** Largely mitigated in v4
   by the ordering rule: matching keys (VPA, counterparty, reference
   numbers) are extracted from the RAW description before any
   scrubbing, so scrub rules can't break Phase 3 matching. Residual
   risk is display-side only (scrubbed CSV text losing something the
   user wanted to read) — keep scrub rules per-bank in
   `bank_scrub_rules.yaml` so a bad rule is a one-line config fix,
   and surface dropped fragments in the Extra Info column where the
   existing HSBC skill already proved that pattern.
4. **GnuCash importer quirks.** Empty `Deposit`/`Withdrawal` columns
   sometimes need to be 0 instead of blank, depending on GnuCash
   version. Test against the target version before declaring Phase 1
   done.
5. **Long statements.** ~~Context-window chunking~~ RESOLVED by
   spec-then-transform — the LLM only ever sees a ~20-row sample
   regardless of statement length. Chunking no longer needed.
6. **Indian number formats.** Lakh/crore digit grouping
   (`1,23,456.78`) breaks naive float parsing; some banks use a
   Dr/Cr indicator column instead of separate Deposit/Withdrawal
   columns. Both handled in the deterministic transform + unit
   tests.
7. **Account renames in GnuCash** silently invalidate mapping
   YAMLs — covered by Phase 3's mapping-validation step.
8. **Local-model capability variance.** The pipeline must work on
   Ollama-class models, not just hosted endpoints.
   Spec-then-transform minimises what the LLM must get right; run
   e2e tests against at least one small local model.

## 8. Bank export quirks (UPDATED 2026-06-11, v3 — mostly VERIFIED)

Verified against the real corpus in `Data/<Person>/` (TBD #8):

* **ICICI (VERIFIED — handled by dedicated `skill_icici`, Phase 1b,
  not the generic skill):** legacy CDFV2 .xls produced by JasperReports.
  Leading empty column; ~12 rows of search-criteria preamble; header
  `S No., Value Date, Transaction Date, Cheque Number, Transaction
  Remarks, Withdrawal Amount(INR), Deposit Amount(INR), Balance(INR)`;
  ~28-row transaction-code legend BELOW the table — strip both ends
  in pre-parse. **Dates are `"01,Apr,2024"` (DD,Mon,YYYY, quoted,
  comma-separated)** — NOT DD/MM/YYYY; naive comma-splitting of the
  CSV will shred them, parse the quoted cell first. Transaction
  Remarks appear width-truncated by the export (~50 chars) — affects
  Phase 3 matching; prefer the VPA/reference tokens that survive
  truncation. Both Value Date and Transaction Date present — use
  Transaction Date for the output Date column.
* **HDFC .xls (VERIFIED):** legacy CDFV2 .xls "Statement of
  accounts". 20 rows of letterhead/address preamble; header at row 21:
  `Date, Narration, Chq./Ref.No., Value Dt, Withdrawal Amt., Deposit
  Amt., Closing Balance`; DD/MM/YY dates.
* **HDFC print-to-PDF (VERIFIED):** statements produced via
  "Microsoft: Print To PDF" have NO extractable text layer (vector
  outlines) — pdftotext returns nothing; OCR is mandatory (Phase 2
  class 2).
* **Karnataka Bank (VERIFIED, new bank #6):** passbook pages only,
  photographed; dot-matrix `Date | Particulars | Ch.No. |
  Withdrawals | Deposits | Balance` layout with `C`/`D` suffix on
  balance amounts.
* **Kotak (DEPRIORITISED 2026-06-11 — no sample, user call):**
  commonly a single amount column with a Dr/Cr indicator instead of
  separate Withdrawal/Deposit columns. Stays in the dropdown; no
  test obligation until a sample exists.
* **All banks:** UPI transactions dominate row count; the UPI VPA in
  the description is the stable matching key (see Phase 3).
* **HSBC, BoB:** already solved by the existing bank-specific skills;
  corpus BoB PDFs confirmed to have a clean text layer.

## 9. Out of scope (explicitly)

* Tax categorisation, GST/VAT handling.
* Multi-entity bookkeeping.
* Investment account import (different schema entirely).
* Receipt OCR. (Statement/passbook OCR is NOT out of scope — it's
  Phase 2 class 2 and the optional passbook-photo path.)
* Two-way sync with a bank API.
