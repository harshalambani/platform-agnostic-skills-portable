# Changelog

All notable changes to this project are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Help system, single-source-of-truth.** Every skill's user help now lives in
  a `help:` block in its `skill.yaml` (overview, when-to-use, per-input
  tooltips/formats/gotchas, steps, per-output-file interpretation, tips,
  troubleshooting). One generator, `scripts/gen_docs.py`, renders it to per-skill
  guides in `docs/user-guide/`, a bundled standalone `docs/USER-GUIDE.html`, and
  the developer `docs/dev/skills-reference.md`.
- **In-app help.** A collapsible "How to use — formats & output" panel on every
  skill tab, a central **Help** tab, and two-tier tooltips (native `info=` helper
  text on inputs; `title=` hover on each output file). All read the `help:` block
  live via `agents.registry` (new `SkillHelp` model) — see `ui/_help.py`.
- **Docs.** `docs/dev/help-block-schema.md` and `docs/dev/editing-help.md`;
  `USER-GUIDE.html` bundled into the frozen package via `paskills.spec`.
- **CI.** `tests/test_help_coverage.py` fails if any UI skill lacks help or if
  the generated docs are stale (`gen_docs.py --check`).
- **Bank abstraction, P1 — contracts.** `agents/bank_contract.py` gains
  `BankStatementMeta` (account number, statement period, source format,
  OCR-vs-exact fidelity, password-used flag — never the password itself) and
  `RowProvenance`; `BankResult` now carries an optional `meta` field, and the
  `BankSkill` protocol gains `formats()`. New `agents/bank_common/` package
  (`normalize`, `tabular`, `text_quality`, `password`) promotes HDFC's
  header-detection, alias-table mapping, date/amount normalization, garbled-
  PDF-text-layer heuristic, and password-error handling into shared,
  bank-agnostic utilities — moved verbatim, so behavior is unchanged. HDFC
  (`skill_hdfc`) is re-expressed on `BankSkill` (`detect()`/`parse()`/
  `formats()` + a `bank_skill` instance) alongside its existing `run()` entry
  point, which is untouched; `parse()` shares the same extraction core via a
  new `_extract_transactions()` helper, verified byte-identical against the
  existing cross-format golden suite. New `agents/banks.py` registry
  discovers banks via a `bank: true` skill.yaml key (frozen-safe, no dynamic
  imports at discovery time — same pattern as `agents/registry.py`); HDFC is
  the first bank onboarded to it. BoB/HSBC/ICICI (already on `BankSkill` from
  earlier work) needed a matching `formats()` method added to stay conformant
  with the extended protocol — no change to their parsing logic. Pipeline
  dropdown/Banks-tab wiring to the new registry and migrating BoB/HSBC/ICICI
  onto it are deferred to later sessions (one bank per session; the pipeline
  already dispatches to their existing `BankSkill` classes directly).
- **Bank abstraction, P2 — BoB (`skill_bob`, v1.1.0) onto the contract.**
  `BoBSkill.parse()` now accepts an optional `password` and returns a fully
  populated `BankStatementMeta` — account number and statement period parsed
  from the PDF front matter (`A/C Number :` / `Statement of account for the
  period of ...`), `source_format` (`pdf` / `pw-pdf`), `fidelity`, and
  `password_used` (never the password). BoB now builds on `bank_common`
  instead of private duplicates: `normalize.clean_amount`/`normalise_date`
  (extended with a trailing Cr/Dr balance-suffix strip and dash-separated
  dates), `text_quality.text_layer_usable` (rejects a garbled/scanned text
  layer before parsing, no OCR fallback), and `password.is_password_error`
  (clear, non-echoing password-error messages). `extract_bob_statement.py`'s
  page-1 x-coordinate column-geometry detection (multi-page tables without a
  repeated header row) stays BoB-specific but now sits on top of these shared
  primitives. Registered in the `banks.py` registry via `skill.yaml`
  (`bank: true`, `bank_key: "bob"`). New synthetic cross-format golden family
  (`tests/skill_bob/bob_fixture_gen.py`): the same 5 fake transactions as a
  2-page PDF (no repeated header, Cr-suffixed balances) and the native CSV
  `extract_bob_statement.py` emits, with an identity test asserting
  byte-identical canonical rows. Canonical CSV output verified byte-identical
  before/after migration against the real local BoB corpus sample (74 rows,
  opening/closing balances unchanged) and the Session-A independent
  closing-balance verdict fix is untouched. HDFC/ICICI/HSBC not touched.
- **Bank abstraction, P2 — ICICI (`skill_icici`, v1.1.0) onto the contract.**
  `ICICISkill.parse()` now returns a fully populated `BankStatementMeta` —
  account number and statement period parsed from the XLS "Search" preamble
  (`Account Number` / `Transaction Date from ... to ...` rows), `source_format`
  (`"xls"`), `fidelity`, and `password_used` (ICICI statements are never
  password-protected). ICICI now builds on `bank_common.normalize` for amount
  cleanup and date parsing instead of a private `MONTH_MAP`: a new
  `parse_comma_month_date()` / `MONTH_ABBR` pair handles ICICI's distinctive
  "DD,Mon,YYYY" date shape, layered under the same `clean_amount()` used by
  HDFC/BoB. `formats()`/`detect()`/the directory glob in `parse()` are
  narrowed from `(".xls", ".xlsx")` to `(".xls",)` — a pre-existing latent
  bug, since `xlrd` 2.x cannot actually read `.xlsx` (support dropped in
  2.0+), and ICICI's own `skill.yaml` already declared `.xls`-only. Registered
  in the `banks.py` registry via `skill.yaml` (`bank: true`, `bank_key:
  "icici"`) plus a module-level `bank_skill` instance. New synthetic golden
  fixture (`tests/skill_icici/icici_fixture_gen.py`): a single `.xls` (ICICI
  has only one real input shape) encoding 5 fake transactions in the real
  12-row-preamble + header-row-13 + data-row-14+ layout, with an identity
  test asserting the expected canonical rows, balances, and meta fields, plus
  a `.xlsx`-rejected test. Canonical CSV output verified byte-identical
  before/after migration against the real local ICICI corpus sample (465
  rows, opening/closing balances unchanged). HDFC/BoB/HSBC not touched.
- **Bank abstraction, P2 — HSBC (`skill_hsbc`, v1.1.0) onto the contract, and
  last bank of P2.** HSBC is the OCR bank (scanned PDFs -> Tesseract ->
  `parse_tsv.py` -> `enrich.py` -> `build_xlsx.py`), so unlike HDFC/BoB/ICICI
  its `BankSkill` boundary is a hybrid: `parse(path, password=None,
  output_path=None)` accepts a PDF, a folder of PDFs, or an already-enriched
  `.xlsx` (the existing `skill_gnucash_pipeline` call site passes an enriched
  workbook plus `output_path` and keeps working unmodified). A PDF/folder
  input runs the OCR pipeline end-to-end via a new `_run_ocr_pipeline()`
  helper (`--password` now threads through `run_pipeline.py` ->
  `ocr_to_tsv.py` -> `pdftoppm -upw`); an `.xlsx` input skips straight to
  `_read_enriched_rows()`. `BankStatementMeta.fidelity` is always
  `"ocr-approx"` (Tesseract output is inherently non-deterministic, never
  `"exact"`); `source_format` is `"pdf"`/`"pw-pdf"`. `_parse_number_hsbc` now
  delegates comma/Cr-Dr cleanup to `bank_common.normalize.clean_amount`
  instead of a private regex. Fixed a real data-loss bug:
  `_read_enriched_rows` was silently dropping the enriched workbook's "Extra
  Information" column; it's now folded into `Description`
  (`"<desc> | <extra>"`) so no field is lost. `detect()` gains an `.xlsx`/
  `.xlsm` fast path (checks for `Transaction Details`/`Withdrawals` headers,
  confidence 0.9) alongside the existing `.pdf` heuristic (text-layer sniff
  for "hsbc", confidence 0.8/0.5, 0.0 for a missing file). Registered in the
  `banks.py` registry via `skill.yaml` (`bank: true`, `bank_key: "hsbc"`)
  plus a module-level `bank_skill` instance; two false claims in its
  `help:` block (a stale "start Ollama" LLM troubleshooting entry and a
  false "text-extractable pages skip OCR" claim) corrected to match the
  actual direct-mode, always-OCR behavior. Session A's float-string
  OCR-confidence crash fix, direct/no-LLM mode, and multi-statement
  date-ordering + continuity detection were confirmed present in code (not
  assumed) and are untouched. Golden strategy differs from BoB/ICICI: OCR
  output isn't byte-deterministic, so the new golden family
  (`tests/skill_hsbc/hsbc_fixture_gen.py`) fixes the deterministic stage only
  — an already-enriched synthetic workbook, shaped exactly like
  `build_xlsx.py`'s real output, asserted against expected canonical rows,
  balances, and meta fields — while the existing OCR-stage tests
  (`parse_tsv.py`'s float-confidence + continuity tests) stay separate and
  untouched. No real HSBC corpus was available locally this session, so the
  pre-existing skipif-guarded corpus tie-out test skips cleanly rather than
  running. HDFC/BoB/ICICI not touched. This completes P2 (all four banks now
  on `BankSkill`); P3 (migrating the pipeline/UI to consume only the
  contract) is next.
- **Bank abstraction, P3a — contract-only pipeline.** `skill_gnucash_pipeline`
  now dispatches every dedicated bank (ICICI, Bank of Baroda, HSBC, HDFC)
  through a single registry-driven path — `agents.banks.discover()` matched
  on `display_name` → `load_bank_skill()` → `skill.parse(path,
  password=pdf_password)` — replacing the four hardcoded `if bank == "..."`
  branches and their four duplicated error-handling blocks with one. Restores
  the `BankSkill` protocol's actual contract (`parse(path, password=None) ->
  BankResult`, canonical rows only): removed the `output_path` side-channel
  from `BoBSkill.parse`, `ICICISkill.parse`, and `HSBCSkill.parse` — three of
  four banks had grown it to write their own canonical CSV, exactly the
  duplication `bank_contract.py`'s docstring forbids. The pipeline now writes
  the canonical CSV + sidecar exactly once, for every bank, via the shared
  `canonical_io.write_canonical_csv`/`write_sidecar` tail (previously only
  used inside each bank's own `parse()`). HDFC — the reference bank — is now
  called through `HDFCBankSkill.parse()` instead of `skill_hdfc.agent.run()`;
  `run()` itself is untouched and still backs the standalone HDFC UI tab.
  HSBC's two-step pipeline seam (`skill_hsbc.tools.run_hsbc_pipeline` to build
  an enriched `.xlsx`, then a separate `HSBCSkill().parse()` call) collapses
  to one: the pipeline now hands HSBC's PDF directory straight to
  `HSBCSkill.parse()`, which already folds OCR-to-enriched-workbook and
  enriched-to-canonical into a single call (from P2) — the
  `run_hsbc_pipeline` import is gone from the pipeline entirely. Each bank
  still needs a little input shaping before the uniform `parse()` call (ICICI
  and HDFC resolve a staged-upload directory to a single matching file; HSBC
  resolves to a PDF directory; BoB keeps its pre-existing "no PDFs found in
  this directory" check) — that's the one piece of per-bank logic that
  couldn't be pushed into the registry itself, since no two banks accept a
  staged upload in quite the same shape. This is a pure wiring phase — zero
  extraction/OCR/canonical-output behavior change, confirmed by every
  existing bank's golden tests passing unchanged (BoB/ICICI/HSBC-deterministic-
  stage/HDFC cross-format goldens all byte-identical). New tests: a contract-
  conformance check (`inspect.signature` asserts every discovered bank's
  `parse()` is exactly `(path, password=None)`, so the `output_path` drift
  can't silently return) and a registry round-trip test per bank (BoB/ICICI/
  HSBC — HDFC's already existed) that parses a synthetic fixture through
  `agents.banks` only, with no direct `from skill_* import`. Grep-verified
  zero `from skill_hdfc`/`skill_bob`/`skill_icici`/`skill_hsbc` imports remain
  in `skill_gnucash_pipeline/agent.py`. Central verdict engine and
  multi-statement-consolidation promotion into `bank_common` are deferred to
  P3b; `skill_itr_workbook`, `skill_26as`, and intercompany skills untouched.
- **Bank abstraction, P3b — shared multi-statement consolidation.** New
  `agents/bank_common/consolidate.py` (`StatementGroup`, `consolidate()`,
  `check_continuity()`) lifts HSBC's reference multi-file logic — order
  statements by actual transaction date (not filename), flag gaps (>3 days)
  and overlaps (<-1 days) as warnings without raising — into a pure,
  bank-agnostic helper. HSBC's own runtime (`skill_hsbc/scripts/parse_tsv.py`)
  is routed through it via a `sys.path` bootstrap (the script runs as a
  subprocess, so it can't rely on `agents` being importable — same convention
  already used by `skill_bob/scripts/extract_bob_statement.py`); its old
  inline sort/continuity/concat logic is gone, replaced by a thin call into
  the shared helper, with `check_statement_continuity()`'s public signature
  preserved for backward compatibility. BoB (`skill_bob/agent.py`) and ICICI
  (`skill_icici/agent.py`) now route their multi-file `BankSkill.parse()`
  batches through the same helper too, replacing their previous naive
  `sorted(glob)` + blind-concat merge, which silently misordered batches with
  non-chronologically-sorting filenames and never reported missing/
  overlapping periods. A single-file batch (the dominant real-world case —
  e.g. a full-year statement for BoB/ICICI/HDFC/Kotak) is a no-op through
  `consolidate()`, so single-statement behavior for all banks is unchanged
  and verified byte-identical against the existing golden suite. Scope note:
  only the registry-driven `BankSkill.parse()` path (used by the GnuCash
  pipeline and covered by goldens) was changed for BoB/ICICI; each bank's
  legacy standalone-UI-tab `run()` entry point (used only by
  `ui/tabs/_generic.py`, untested by any golden) still does its old naive
  filename-sorted concat and was deliberately left untouched. HDFC and Kotak
  remain single-statement only (no multi-file path added). Central verdict
  engine, HDFC/Kotak, and the statement-profile engine untouched.

### Fixed
- **Bank gating, registry-driven (closes the Kotak offer-then-reject leak).**
  Onboarding Kotak (#89) added it to the `skill_gnucash_pipeline` Bank
  dropdown's static `options:` list but not to `agent.py`'s hardcoded
  `DEDICATED_BANKS = ["ICICI", "Bank of Baroda", "HSBC", "HDFC"]`, so
  selecting "Kotak" in the UI passed the dropdown but then failed the
  `SUPPORTED_BANKS` guard at runtime ("Supported banks: ICICI, Bank of
  Baroda, HSBC, HDFC") — an offer-then-reject bug live on `main`. Both
  gating surfaces are now registry-driven off the single source of truth,
  `agents.banks.discover()`, so they can never diverge again for any future
  bank: a new `_options_from_banks()` resolver in `ui/tabs/_generic.py`
  (registered as `"banks"` in `_OPTIONS_FROM_RESOLVERS`) drives the dropdown
  via `skill_gnucash_pipeline/skill.yaml`'s `bank` input (now
  `options_from: "banks"` instead of a static list), and `DEDICATED_BANKS`
  is now `[b.display_name for b in discover()]` instead of a literal. Dropdown
  order is now alphabetical by `display_name` with "Other Bank (CSV)" last
  (an accepted cosmetic change — previously ICICI/BoB/HSBC/HDFC/Kotak/Other).
  No dispatch logic changed; all 5 banks' extraction goldens remain
  byte-identical. New `tests/test_bank_gating.py` is the permanent regression
  guard: asserts `"Kotak" in SUPPORTED_BANKS`, `DEDICATED_BANKS == [b.display_name
  for b in discover()]`, and that the dropdown's resolved options exactly
  match `discover()` display names + `"Other Bank (CSV)"` last.
- **HDFC — Value Dt now used on every input path.** HDFC statements carry
  both a posting Date and a Value Dt; the canonical CSV's "Date" column
  (which flows unchanged through balance checks, dedup, and account mapping)
  now emits the Value Dt on the PDF text path (`skill_hdfc`) and the PDF OCR
  path, matching the XLS/XLSX path which already preferred it. Falls back to
  the posting date only when Value Dt is blank. Note: opening-balance
  reconciliation and duplicate detection key on this field, so rows where
  posting and value dates differ (e.g. cheque clearing) may now be bucketed
  by a different date than before.
- **ICICI — docstring corrected.** The module docstring wrongly claimed
  ICICI used Transaction Date; the code already preferred Value Date
  (falling back to Transaction Date only when blank). No behavior change —
  documentation and a regression test now match the existing code.
- **Intercompany skills moved out of GnuCash > Banks.** "Intercompany Reco"
  and "Intercompany Matrix" are not bank-statement tools and were rendering
  alongside statement-import skills under the Banks sub-tab. Both now use a
  dedicated `category: "intercompany"` and render under a new
  GnuCash > Intercompany sub-tab (Reco first, Matrix second). Banks now shows
  only statement import + Review Mappings.

## [2.4.0] — 2026-07-16

### Fixed
- **ITR Workbook — Data/itr paths no longer double up under the frozen
  Launcher.** `entities_path`/`rules_dir`/`scrips_path` were CWD-relative
  defaults with `Data/` baked in (`agent.py::run()`); the frozen PortableApps
  build sets CWD to `...\Data\`, so they silently resolved to
  `...\Data\Data\itr\...` and the run read stale/empty config while the
  entity/AY dropdowns (already anchored via `ui/_config.data_root_dir()`)
  showed the correct list. `ui/tabs/_generic.py` now anchors all three via a
  new `{data_root}` `run_args` token (same anchor the dropdowns use);
  `skill.yaml`'s `run_args` route through it. Agent defaults remain a
  source-mode-only fallback.
- **ITR Workbook — missing/unresolved entity now fails loud.** A missing or
  unreadable `entities.yaml`, or an explicitly selected entity not found in
  it, used to silently substitute a generic `UNKNOWN`/`Individual`/new-regime
  profile — picking the wrong regime/age band without any warning.
  `agent.py::_resolve_entity()` now raises when an *explicitly selected*
  entity can't be resolved, naming the resolved path it looked at; the run
  reports an `ERROR:` summary and writes no green stub. An entity key merely
  *inferred* from a mapping file's stem (no explicit selection) still
  degrades gracefully, unchanged.
- **ITR Workbook — mapping-less run no longer silently emits an empty green
  stub.** With the Entity mapping box empty, a run used to report
  `STATUS: OK` and write a one-sheet scaffold with no schedules — easy to
  mistake for a real, populated workbook. Two changes: (1) when an entity is
  selected and it has an existing
  `<data_root>/itr/mappings/<entity>.mapping.yaml`, the run now auto-derives
  and uses it (logged in the summary as `Mapping: auto-derived ...`); (2) a
  true cold start (no mapping anywhere for the entity) now treats every leaf
  as unmapped and routes into the existing BLOCKED-FOR-REVIEW +
  proposed-mappings-snippet learning loop, the same as a partially mapped
  file — a mapping-less run can no longer report a green `STATUS: OK`.

## [2.5.0] — 2026-07-16

### Changed
- **ITR Workbook — best-effort workbook instead of block-to-nothing (Part
  1).** An unmapped account used to set `STATUS: BLOCKED-FOR-REVIEW` and
  skip the workbook build entirely (`_build_and_write_workbook` returned
  `[]`, and `run()` wrote a one-sheet scaffold) — a user with even one
  unmapped leaf got nothing usable. Any unmapped leaf (a partially mapped
  file, or a true cold start with none) now still builds the full
  BS + P&L + IT working workbook:
  - Every unmapped leaf routes into a new UNCLASSIFIED/REVIEW bucket
    (`schedules.build_unclassified`, rendered on a new `Unclassified` sheet
    plus red call-outs on Mapping Review/Reconciliation) instead of being
    silently dropped — its amount is included in that bucket's own total,
    so the accounting identity (Assets = Equity+Liabilities; the
    RetainedEarnings P&L control total) still ties out exactly.
  - The IT working (Computation sheet) shows two tax figures whenever
    anything is unclassified: **DRAFT** (tax computed on resolved items
    only, stamped with the unclassified count/₹ total, not filing-ready)
    and a **worst-case upper bound** (every unclassified INCOME-type leaf
    assumed fully taxable at the top slab rate for the selected regime;
    unclassified expense/deduction/BS-side items are never assumed to
    reduce tax — conservative). Neither is presented as a final total; the
    plain "Tax liability" row is relabelled DRAFT whenever N > 0.
  - `STATUS: BLOCKED-FOR-REVIEW` (nothing built) is replaced by
    `STATUS: BUILT -- N REVIEW ITEM(S)` whenever N > 0; the
    `<output>-proposed-mappings.yaml` learning-loop snippet is still
    written every time.
  - Hard-error paths (unparseable HTML, unresolved entity, AY-vs-HTML
    mismatch, a mapping file with a `VALIDATION ERROR`) are unchanged —
    still fail loud with a stub, no workbook.
  - A fully-mapped run (0 unmapped) is unchanged: no DRAFT stamp, no
    `Unclassified` sheet, tax shown as final — same as before this change.
  - Part 2 (an in-app ITR Mapping review UI, so a user never has to
    hand-edit the proposed-mappings YAML) is tracked separately and not
    included in this release.

### Pending
- **Frozen-build smoke test** (Harshal-side, PortableApps install) not run
  as part of this release — flagged pending, not blocking.

## [2.6.0] — 2026-07-16

### Added
- **ITR Mapping review UI (Part 2).** A new "ITR Mapping" tab (GnuCash >
  ITR Mapping, next to ITR Workbook) gives the account-tag mapping the same
  review UX as the post-bank-transformation "Review & Edit Account Mappings"
  tab — no more hand-editing the `-proposed-mappings.yaml` snippet or
  running a CLI script:
  - Select an entity (same `Data/itr/entities.yaml` dropdown source as the
    ITR Workbook tab); Load shows every account for that entity, sourced
    from `Data/itr/mappings/<entity>.mapping.yaml` plus the most recent
    `-proposed-mappings.yaml` run artifact — unmapped accounts are flagged
    (red UNMAPPED badge) with any LLM suggestion shown alongside.
  - A searchable tag-assignment picker (typeahead over `tags.py`'s
    vocabulary, showing each tag's description) plus row multi-select and
    "Apply to selected", mirroring `ui/tabs/gnucash_review.py`'s account
    picker.
  - Save writes `Data/itr/mappings/<entity>.mapping.yaml` (anchored via
    `data_root_dir()`, works in both source and frozen layouts) —  always
    backing up the pre-save file first (timestamped `.bak-YYYYMMDD-HHMMSS`)
    before any in-place rewrite, and never touching disk at all for a blank
    entity or an empty change set. Touched entries are marked approved
    (`suggested_by_llm` cleared, note replaced) the same way the CLI
    correction script already did.
  - `apply_mapping_corrections.py` gained an importable
    `apply_corrections_map(mapping_file, {guid: tag}, output_yaml, paths=...)`
    — the new core the UI calls directly (no more shelling out); the
    existing CLI (`apply_corrections(mapping_file, reviewed_xlsx,
    output_yaml)`) is now a thin wrapper over it and its behaviour is
    unchanged (round-trip test still green).

### Pending
- **Frozen-build UI smoke test** (Harshal-side, PortableApps install) not
  run as part of this release — flagged pending, not blocking.

## [2.8.0] — 2026-07-17

### Added
- **ITR Mapping review UI polish (Part 4): sortable/filterable columns.**
  The "ITR Mapping" table's headers are now click-to-sort (click again to
  flip direction) with a per-column text filter row underneath — the same
  UX as `ui/tabs/gnucash_review.py`'s "Review & Edit Account Mappings" tab,
  which this screen had not previously matched.
- **ITR Mapping review UI polish (Part 4): tag vocabulary help.** Every tag
  code shown in the table (Current tag / Suggested / New tag) now carries a
  hover tooltip with its one-line meaning, and a new toggleable "? Tag
  glossary" panel lists the full, searchable tag vocabulary (code, target
  sheet, meaning) — the raw tag codes (e.g. `OS_INTEREST_BANK`) previously
  had no in-UI explanation.

### Pending
- **Frozen-build UI smoke test** (Harshal-side, PortableApps install) not
  run as part of this release — flagged pending, not blocking.

## [2.7.0] — 2026-07-16

### Added
- **ITR Mapping review UI polish (Part 3): RAG confidence coding.** Mapped
  accounts on the "ITR Mapping" tab now show a confidence tier instead of
  just a tag: green "(confirmed)" once a human has approved/set the entry,
  amber "(needs review)" while it's still an unapproved LLM suggestion
  (`suggested_by_llm` set), red "UNMAPPED" as before — shown as both a
  left-border row accent and an inline badge. The "Show" filter gained
  "Needs review" and "Confirmed only" options alongside the existing
  All/Unmapped/Mapped.

### Fixed
- **ITR Mapping "Show" filter defaulted to hiding everything but unmapped
  rows**, and its native `<select>` chrome had poor contrast against the
  dark theme (reported as barely visible). The filter now defaults to
  **All** and is explicitly styled to match the rest of the tab.

### Changed
- **ITR nav restructured.** "ITR Workbook" and "ITR Mapping" were flat
  sub-tabs directly under GnuCash; they now live inside a single "ITR"
  sub-tab (GnuCash > ITR > ITR Workbook / ITR Mapping), mirroring how
  "Banks" already groups its own sub-tabs.

### Pending
- **Frozen-build UI smoke test** (Harshal-side, PortableApps install) not
  run as part of this release — flagged pending, not blocking.

## [1.0.1] — 2026-06-25

### Fixed
- **README skills table** — was stale at 9 skills from the 1.0.0 release;
  now lists all 16 user-facing skills (KRChoksey ledger/import/reconcile,
  HDFC, GnuCash Import pipeline, 26AS Journal, ICICI added since), grouped
  to match the UI, with accurate mode and LLM-requirement columns.

### Verified
- Frozen-build smoke test against current `main` (sha `9158ed5`): rebuilt
  `pa_skills.exe` from source, launched it, confirmed HTTP 200 on the
  Gradio root and all 16 skill tabs present in the served UI tree. No
  regressions found.

## [1.0.0] — 2026-06-04

### Added
- **MSG / Email Parser skill** — direct-mode skill that parses `.msg`
  (Outlook) and `.eml` files into structured JSON (sender, date, subject,
  body, attachment list). Uses `extract-msg` for `.msg` and stdlib `email`
  for `.eml`. No LLM required. (B7 resolved)
- **Auto-update checker** — Home tab checks the GitHub releases API on
  startup and shows a banner when a newer version is available. Background
  thread, cached for the process lifetime. (D2 resolved)
- **Frozen-build CI smoke test** — new step in `release.yml`: launches
  `pa_skills.exe`, waits for port file, GETs the root URL, verifies
  HTTP 200, then kills the process. Catches PyInstaller bundling
  regressions. (E4 resolved)
- **qpdf vendored** — added to `binaries.toml`, `refresh_binaries.py`,
  `build.py` (step 6b), and `_native.py` resolver. CC Sort no longer
  requires qpdf to be installed by the user. (B2 resolved)
- Unit tests for `_config.py` (~15 tests), `_runner.py` (~12 tests),
  `_health.py` (~13 tests), MSG parser (~15 tests), update checker
  (~12 tests). Total test count ~175+. (E1 substantially resolved)
- `README.md` overhauled — architecture overview, dev + user setup,
  skill authoring guide, CI badge. (F1 resolved)
- `BUILDING.md` — consolidated build guide replacing 4 date-stamped
  notes files. (F3 resolved)

- **Dependency management infrastructure:**
  - `requirements-lock.txt` — exact pinned versions for reproducible
    frozen builds. `build.py` prefers the lock file when present.
  - Dependabot config (`.github/dependabot.yml`) — weekly PRs for pip
    and GitHub Actions dependency updates.
  - Native binary update checker (`.github/workflows/check-native-binaries.yml`)
    — weekly scheduled job checks Tesseract, Poppler, qpdf releases via
    GitHub API and opens an issue when updates are available.
  - Compatibility check (`.github/workflows/compat-check.yml`) — weekly
    scheduled job installs latest-compatible deps from loose pins, runs
    full test suite, and attempts frozen build + smoke test.

### Changed
- **Upstream repo published** — `platform-agnostic-skills` pushed to
  GitHub. `sources.toml` switched from `kind = "local"` to `kind = "git"`
  with public URL. CI no longer uses `--skip-pull`. (B6 resolved)
- Historical plan/notes/prompt files moved to `docs/history/`.
- Skill count: 8 → 9 (MSG Parser added).
- `_native.py` now resolves Tesseract, Poppler, and qpdf.
- `.gitattributes` comment updated for qpdf.

## [0.4.1] — 2026-06-03

### Fixed
- **Self-hosted Launcher Generator (D1)** — bundled PortableApps.com
  Launcher Generator v2.2.4 under `bundling/launcher-gen/2.2.4/` so CI
  no longer needs to download it from portableapps.com (TLS handshake
  was failing on GitHub-hosted runners). Every release zip now ships
  with `PASkillsPortable.exe`.
- Simplified `release.yml` — removed download step, `SKIP_LAUNCHER`
  env var, and fallback zip logic.
- Updated `build.py` `LAUNCHER_GEN_HINTS` to check bundled copy first.
- Smoke test `test_registry_discovers_all_skills` updated for 8 skills.
- `test_webui_constructs` skipped gracefully when gradio is not installed.
- `test_history_tab.py` mocks gradio safely (try real import first).

## [0.4.0] — 2026-06-03

### Added
- `type: "select"` input for generic skill tabs — renders a `gr.Dropdown`
  with predefined choices from `options:` in `skill.yaml`. Allows custom
  values typed by the user.
- `--clean` flag on `bundling/build.py` — deletes
  `build_pyinstaller/.agents_cache/` and exits.
- **Agent progress streaming (C4)** — agent-mode skills now show live
  intermediate steps (tool calls, tool results, LLM reasoning) in the
  result area instead of just elapsed-time ticks. Implemented via
  `_StreamingAgentWrapper` in `base_agent.py` and `run_with_streaming()`
  in `_runner.py`. Zero changes to individual skill files.
- **Skill output history tab (C5)** — scan outputs directory, sortable
  table with download and delete actions.
- **Document Summarizer** — direct-mode skill for PDF/text summarization.
- **Text Translator** — direct-mode skill with select dropdowns.
- **CSV Data Analyzer** — agent-mode skill with pandas tools and safety guards.
- 60-test suite for Phase 4C skills with synthetic fixtures.
- 30-test suite for history tab.
- 17 unit tests for streaming infrastructure.
- End-to-end test runner (`test_4c_e2e.bat`) for Ollama-backed validation.

### Changed
- CI Python version bumped from 3.10 to 3.13 to match `pyproject.toml`
  (`requires-python = ">=3.13"`).
- Translator skill now uses `type: "select"` dropdowns for source/target
  language instead of free-text fields.

---

## Phase 4C — 2026-05-28

### Added
- **Document Summarizer** skill (`skill_summarize`, `mode: "direct"`) —
  upload a PDF or text file, get a structured markdown summary with
  Key Points / Detailed Summary / Conclusions sections.
- **Text Translator** skill (`skill_translate`, `mode: "direct"`) — paste
  text, specify source and target languages, get a translated `.txt` file.
  Works with any chat-capable LLM including local models via Ollama.
- **CSV Data Analyzer** skill (`skill_csv_analyzer`, `mode: "agent"`) —
  upload a CSV file and ask a natural-language question. Uses pandas-based
  tools (`describe_csv`, `query_csv`) with expression safety guards
  (allowlist + blocklist) to prevent code injection.
- 60 unit tests in `tests/test_phase4c_skills.py` covering registry
  discovery, YAML validation, file reading, truncation, input validation,
  safety guards, and CSV tool functions with synthetic fixtures.

---

## Phase 4B — 2026-05-27

### Added
- Multi-file upload input type (`type: "files"` in `skill.yaml`) — Gradio
  `file_count="multiple"`, staged into a temp directory for the skill.
- BoB skill updated to accept multiple PDF uploads via `type: "files"`.

### Changed
- HSBC skill switched from `type: "file"` to `type: "directory"` input
  to match its agent API (accepts a folder of PDFs).
- cc_sort and cc_transactions agent scripts: replaced `subprocess` calls
  with `runpy.run_path()` for frozen-mode compatibility.
- Fixed `check_extract_msg_available` — was broken in frozen mode due to
  `sys.executable -c` pattern; now uses direct import.

### Fixed
- Frozen-mode subprocess failures for cc_sort and cc_transactions skills.

---

## Phase 4A — 2026-05-27

### Added
- **Pluggable skill architecture:** `agents/registry.py` auto-discovers
  `agents/*/skill.yaml` at startup and exposes `SkillInfo` dataclass
  objects. Adding a new skill requires only a `skill.yaml` manifest —
  no code changes to `webui.py`.
- **Generic tab rendering:** `ui/tabs/_generic.py` dynamically builds
  Gradio tabs from skill manifests. Supports `file`, `directory`, and
  `text` input types.
- **`run_direct()` execution path** in `base_agent.py` for simple
  prompt → LLM → response skills (no tools, no agent loop). Skills
  declare `mode: "direct"` in `skill.yaml` to use it.
- Home tab now dynamically lists all discovered skills from the registry
  instead of hard-coded text.

---

## Phase 3 — 2026-05-25 (v0.3.0 / v0.3.1)

### Added
- Real icon artwork (gear + sparkle, rounded container) at 16/32/75/128 px.
- Real `git clone --depth 1` agent pull from `sources.toml` with
  SHA-256-keyed cache at `build_pyinstaller/.agents_cache/`.
- GitHub Actions CI release pipeline (`.github/workflows/release.yml`)
  triggered on tag push. Graceful fallback when PortableApps.com Launcher
  Generator CDN is unreachable from Azure runners.
- Vendored Tesseract 5.4.0 and Poppler 24.07.0 binaries via Git LFS.

---

## Phase 2b — 2026-05-24 (v0.2.0)

### Added
- `step8_render_inis` — renders `appinfo.ini` and Launcher INI from
  `bundling/templates/`.
- `step9_copy_defaults` — copies `bundling/templates/DefaultData` into
  staging.
- `step10_launcher_gen` — invokes the PortableApps.com Launcher Generator
  to produce `PASkillsPortable.exe`.
- `step11_zip` — builds a deterministic `dist/PASkillsPortable_<ver>.zip`.
- CLI flags: `--launcher-gen <PATH>`, `--skip-launcher`.

### Changed
- `paskills.spec`: `console=True` → `console=False`. `pa_skills.exe` now
  runs windowless; the PortableApps launcher is the user-facing entry point.

---

## Phase 2a — 2026-05-22

### Added
- Tesseract + Poppler vendoring under `vendor/` via Git LFS.
- `bundling/refresh_binaries.py` — download + SHA-256 verify + extract
  native binaries into `vendor/`.
- `ui/_native.py` — resolves native binary paths, prepends to `PATH`,
  configures `pytesseract`. Idempotent.
- BoB tab (`ui/tabs/skill_bob.py`) — pdfplumber only, no native binaries.
- HSBC tab (`ui/tabs/skill_hsbc.py`) — calls `_native.ensure_native_path()`
  with clear UI error if Tesseract or Poppler are missing.
- `build.py` steps 5–6: copy `vendor/*` into `staging/App/PASkills/`.
- `build.py` step 7: reads `bundling/sources.toml` for agent pull source
  (local sibling folder or git clone).
- Three new smoke tests: BoB import, HSBC import, `_native` resolver.

### Changed
- Pinned `gradio>=6.0,<7.0` in `requirements.txt` and `pyproject.toml`.
- Fixed `datetime.utcnow()` → `datetime.now(timezone.utc)` in `build.py`.

---

## Phase 1 — 2026-05-20

### Added
- Phase 1 scaffold of the portable packaging project per the v0.2 spec
  (`2026-05-01-portable-apps-packaging-spec.docx`).
- Repo root: `LICENSE` (Apache 2.0), `NOTICE`, `README.md`, `pyproject.toml`,
  `requirements.txt`, `.gitignore`, `.gitattributes` (LFS rules per spec §10.5).
- Source tree skeleton: `src/agents/`, `ui/{tabs,_buildinfo.py}`,
  `bundling/{templates,icons}`, `tests/`, placeholder `vendor/`.
- `src/agents/` mirrored from the sibling `platform-agnostic-skills` project
  per the build-time-pull contract (locked decision §15.1).
- Minimal Gradio `ui/webui.py` with Home and 26AS tabs only, custom black +
  electric-blue theme (locked decision §15.4), bound to 127.0.0.1 with
  free-port pick.
- `bundling/build.py` covering steps 1–4 of spec §10.2 (read git tag,
  reset staging, create venv, run PyInstaller `--onedir`).
- `bundling/paskills.spec` with hidden imports per spec §10.3.

### Changed
- Renamed top-level `packaging/` folder to `bundling/` to avoid a Python
  import-path shadow on the PyPI `packaging` package.

### Notes
- Native binaries (Tesseract, Poppler) deferred to Phase 2.
- AppInfo, DefaultData, Launcher INI, and Launcher Generator deferred to Phase 2.
- Frozen `pa_skills.exe` must be smoke-tested manually; see `BUILD-NOTES.md`.
