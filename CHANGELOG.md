# Changelog

All notable changes to this project are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

> **Note.** The entries below were never rolled into a released section at
> tag time, but git history shows they already shipped: the help-system
> entries in `v1.2.2` (`a284dc3`, `57975da`), the HDFC/ICICI value-date
> entries in `v2.3.0` (`cc2221e`, PR #62), and the Intercompany sub-tab
> entry in `v2.3.1` (`a8cf73a`, PR #76). They are left here rather than
> back-filed into sections that do not exist for those tags.

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

### Fixed
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
- **ITR workbook — refund/tax-payable line raised #NUM! for every tax-payable or loss-year return.** Excel's `MROUND(number, multiple)` raises `#NUM!` whenever `number` and `multiple` have opposite signs. The s.288B refund/tax-payable rounding (`Statement of Income` and `Computation` sheets) and the s.288A Total Income rounding always round to a positive Rules-sheet constant, but the number being rounded goes negative for a tax-payable (as opposed to refund) assessee or a loss year — so every such return produced a broken `#NUM!` cell instead of a number. Replaced with a new sign-safe `presentation.mround_safe()` helper (`ROUND((x)/m,0)*m`), which reproduces `MROUND`'s round-half-away-from-zero behaviour for both signs and never errors, at all three call sites.
- **ITR workbook — Salary sheet's displayed gross silently dropped perquisites.** The Salary sheet's "Gross salary (17(1)+17(2)+17(3))" label showed the book's `SALARY_GROSS` tag total, which only ever captures 17(1) — so any salary with non-zero 17(2) perquisites or 17(3) profits-in-lieu displayed a gross that didn't match its own label (and didn't match Form16). `build_salary()` now sources `gross` from Form16's `total_1d` (falling back to `s17_1+s17_2+s17_3` when `total_1d` is absent) on the Form16 path; the book-only path (no Form16) is unchanged. `verify.py`'s Book↔Form16 cross-check control is untouched and continues to compare the book's `SALARY_GROSS` total against 17(1) alone. Added a matching fail-loud "banner, no abort" control (`SalarySchedule.reconciliation_ok`/`reconciliation_diff`, mirroring the existing Capital Gains reconciliation banner): a genuine gross/exemptions/deductions vs income-chargeable mismatch now surfaces a prominent ERROR banner on the Salary and Statement of Income sheets and a Reconciliation-sheet line, and is picked up by `agent.py`'s exit-code logic — the workbook is still always fully written.
- **ITR workbook — Statement of Income now leads with income, not working machinery.** The "Brought forward losses set off" input block and the New/Old regime tax-working formulas used to sit at the very top of the `Statement of Income` sheet, ahead of "Income from Salary" — so a reader had to scroll past raw working cells before reaching any income figure. Both now live in a new "Workings / Inputs" section below "Refund Due / (Tax Payable)", ahead of the Assumptions block. The b/f-loss cells remain live, directly editable inputs (default 0); the regime tax-working formulas remain on the same sheet. A short pointer note ("see Workings below") was added to the House Property, Business, and Capital Gains section headings, which apply b/f set-off. The move is self-proving: presentation.py computes the new section's rows analytically ahead of time and asserts the real render lands exactly there, turning any future layout drift into an immediate generation-time `AssertionError` instead of a silent `#REF!`.
- **ITR workbook — Assumptions block now notes interest u/s 234A/234B/234C is not computed.** The tax-payable figure shown was always pre-interest; this was previously undocumented on the deliverable page itself. 234-interest computation remains out of scope.
- **CI now collects `src/agents/**/test_*.py`, not just `tests/`.** Bare
  `pytest` used `testpaths = ["tests"]`, so `skill_gnucash_import`'s
  `test_transforms.py` (20 tests) was silently skipped in every run — it only
  ran when invoked by explicit path. `testpaths` now also includes
  `src/agents`. That test file also did `sys.path.insert(...)` +
  `import agent`, writing the generic `agent` key into `sys.modules`; when
  collected alongside `tests/skill_itr_workbook/test_agent_full_pipeline.py`
  (which does the same for its own, different `agent.py`), whichever loaded
  first "won" and the other failed with
  `ImportError: cannot import name 'parse_date' from 'agent'`. Fixed by
  loading `skill_gnucash_import`'s `agent.py` via `importlib` under a unique
  module name instead, so it no longer collides with other skills' same-named
  `agent.py` modules regardless of collection order.

## [2.14.0] — 2026-07-21

### Changed
- **ITR workbook — income totals AND the standard tax computation now
  compute on the deliverable page, so manual overrides propagate end to
  end.** The `Statement of Income` sheet's income ladder (Gross Total Income
  → Chapter VI-A → Total Income, plus the normal-income / special-rate-CG
  split) **and** the tax computation itself (Tax on total income (slab) →
  less s.87A rebate → add Surcharge → less Marginal relief → add Health &
  Education Cess → Total tax liability → add special-rate CG tax → less
  prepaid taxes → Refund/(Payable)) are now built from live on-page Excel
  formulas over on-page cells, instead of mirroring a hidden `Computation`
  working sheet. `Computation` keeps its full slab/rebate/surcharge/cess
  machinery, re-anchored to read the page's own normal-income cell, and
  stays as a parallel hidden backing/audit sheet — but the page's own tax
  lines are now independently live, not a mirror of `Computation`'s output.
  Special-rate LTCG/STCG (111A/112A) stays carved out of the slab base
  (regression-tested).
  **Brought-forward-loss set-off is now FOUR statutory, editable, per-bucket
  input cells** — b/f House Property loss (s.71B), b/f Business loss (s.72),
  b/f Short-term capital loss (s.74), b/f Long-term capital loss (s.74) —
  replacing the previous single lump cell and its previously-parked
  placeholder. Each bucket sets off only against its own income
  head/gain-type, capped at that head's available income for the year, at
  the head level *before* aggregation into Gross Total Income (STCL sets
  off against STCG first with any remainder spilling to LTCG; LTCL sets off
  against LTCG only) — matching what the Act actually requires rather than
  a lump Total-Income deduction. An entered amount always stays visible even
  if it exceeds the available income in its head; only its *effect* is
  capped. No change to any tax rate, rule, or the default (no-override)
  figures — the generated workbook reconciles to the same numbers as
  before. Design: `docs/2026-07-20-itr-onpage-totals-plan.md` (section 11,
  "REVISION 2").

## [2.13.2] — 2026-07-20

### Fixed
- **ITR workbook — capital gains understated to ~zero on every equity sale.**
  The lot-reconstruction engine (`scripts/lots.py` `_sale_transactions`)
  identified the booked-gain leg of a stock disposal by requiring the income
  split's GnuCash `action` to be `"LTCG"`/`"STCG"`. Real books never carry
  that: GnuCash only auto-stamps `Buy`/`Sell` via the stock assistant, and a
  manually entered capital-gain income split has no `action`. So the gain leg
  was misclassified as a *proceeds* split, where its negative value cancelled
  the real broker proceeds — collapsing computed proceeds to the cost basis
  and the gain to ~0 for every sale. The gain leg is now detected by account
  **type** (`INCOME`) instead of `action`; books that do set an explicit
  `action` still classify correctly. Verified against a real book: a disposal
  that previously reported a 0 gain now reports the correct ~2.46 lakh gain.
- **Capital-gains reconciliation now fails loud (banner, no abort).** When the
  reconstructed lot gains do not reconcile to the books' `CG_*_CONTROL`
  totals, the workbook is still written in full, but a prominent ERROR banner
  is placed at the top of both the `CG` and `Statement of Income` sheets, and
  the new `agent.main()` CLI wrapper exits non-zero with a stderr line. Prior
  behaviour surfaced a mismatch only as a buried `OK`/`MISMATCH` cell on a
  working sheet, so a materially wrong return could be handed on unnoticed.

## [2.13.1] — 2026-07-19

### Fixed
- **Frozen build could not start (v2.13.0 was unusable).** `pydantic` 2.13.4
  declares `_COMPATIBLE_PYDANTIC_CORE_VERSION = 2.46.4` and raises
  `SystemError` at import when it finds a different `pydantic-core`, so every
  launch of the v2.13.0 package died in `gradio` → `fastapi` → `pydantic`
  before the UI came up. Dependabot PR #69 had bumped `pydantic-core` to
  2.47.0 on its own without bumping `pydantic`; because `bundling/build.py`
  installs the lock with `--require-hashes --no-deps`, pip never evaluated
  `pydantic`'s pin on `pydantic-core` and the mismatch installed silently.
  The test suite runs against the separate dev virtualenv, so it stayed green
  — only the release workflow's frozen smoke test caught this. `pydantic-core`
  is pinned back to 2.46.4. No application code changed; v2.13.1 is v2.13.0
  with a working package.

## [2.13.0] — 2026-07-19

### Added
- **ITR workbook — `PL for Business` sheet, subtree-driven.** A fifth
  presentable sheet nets an entity's business income against business
  expenses for entities that have both: `Remuneration from Partnership` and a
  nested `Business Expenses/` group. It is driven entirely by a new optional
  entity field, `EntityProfile.business_subtree` (e.g.
  `"Income/xBusiness Income"`) — a GnuCash account path prefix walked as a
  plain subtree (`path.startswith(prefix)`), never a keyword or account-name
  match, so a business-sounding account outside the configured subtree (e.g.
  `Expense/Professional Tax`) is never swept in. Reuses the existing
  `_write_hierarchy_sheet`/`build_hierarchy`/`render_hierarchy` engine
  unchanged (extended with an optional `extra_row_fn` hook to add the "Net
  Business Income / (Loss)" total row) — no second layout engine. The sheet
  is omitted, per-run and per-FY exactly like `CG`, when the entity has no
  `business_subtree` configured or the FY has no matching activity; if
  `business_subtree` **is** configured but nothing under it appears for the
  FY, generation now raises (`BusinessSubtreeError`) rather than silently
  rendering a zero sheet, so a GnuCash account rename can't quietly drop a
  real business year. Sheet order is now `Statement of Income`, `BS`, `IS`,
  `PL for Business`, `CG`. Presentation-only: business income is not routed
  into tax computation, and no other sheet's content, order relative to each
  other, or computed figures changed.
- **ITR workbook — Father's Name, Aadhaar and real residency, unparked.**
  Three placeholders on `Statement of Income`'s header block are now live,
  optional entity fields: `father_name` and `aadhaar` (Aadhaar rendered
  space-grouped `NNNN NNNN NNNN`, CA-file style, as a formula over the raw
  digits on `Entity` — never a second literal copy). Both stay PARKED
  (styled-empty, label keeps "(to be filled)") when absent on the entity, and
  drop the parked note per field the moment a value is supplied. Stored the
  same way as PAN/DOB (plaintext in `EntityProfile`/`entities.yaml`) — no
  at-rest protection exists for any identity field in this project, and this
  does not introduce one. Residential status (`R/OR` / `RNOR` / `NR`) is now
  a DECLARED entity field (`rules.resolve_residency()`), read from the
  pre-existing but previously-unconsumed `EntityProfile.residency`; only the
  exact statutory tokens count as declared, everything else (including the
  ubiquitous legacy free text `"Resident"`) is undeclared and defaults to
  `R/OR`, preserving prior behavior byte-for-byte. The "Assumptions" footnote
  now renders only while residency is defaulted, and disappears once an
  entity declares one of the three tokens. Brought-forward loss set-off
  remains PARKED — out of scope for this change.
- **ITR workbook — four presentable deliverable sheets.** The generated
  workbook was a calculation engine, not something that could be handed to a
  CA: `Computation` was a flat two-column list with no column widths at all,
  no header block, no print setup, and it showed both regimes side by side.
  Four new sheets now sit in front of the existing ones — `Statement of
  Income`, `IS`, `BS` and `CG` — modelled on the CA-prepared reference
  workbooks, with a letterhead header block, tiered money columns, Arial 10,
  Indian digit grouping, explicit column widths sized to the longest label
  actually present, borders, freeze panes, gridlines off, and A4
  fit-to-one-page-wide print setup. The four raw working sheets (`Rules`,
  `Mapping Review`, `IS_Transcript`, `BS_Transcript`) are now hidden — hidden,
  not deleted. This is a rendering change only: no computation, rule, rate or
  tax logic was touched, and no existing sheet's values changed.
  - **Every money cell on the four new sheets is a formula** into the existing
    sheets (`Computation`, `CapitalGains`, `OtherSources`, `IS_Transcript`,
    `BS_Transcript`, `TaxesPaid`, `Entity`). Nothing is recomputed or
    hardcoded, so the audit trail survives into the printable output.
  - `IS`/`BS` rebuild the full GnuCash hierarchy by splitting the transcripts'
    `Path` column on `/`, preserving every intermediate group as its own row
    with its own subtotal. Sibling groups are never merged — in particular
    `Fixed Deposits` stays a sibling of `Cash and Bank`. (Schedule AL's
    statutory buckets do combine them; that is a different sheet with a
    different purpose.) Depth is derived from the path, not a fixed level
    count.
  - `CG` is a view over `CapitalGains`. It deliberately does not copy two
    traits of the CA reference: inline 31-Jan-2018 FMV price literals, and
    grandfathering arithmetic that is inconsistent between rows.
  - `CG` is omitted entirely when the financial year being generated has no
    capital-gains activity, mirroring what the CA produced for such a year.
    The test is per-run and per-FY — never an entity-level flag or a cached
    answer that could silently drop a real CG sheet the year it matters.
  - Three items render as a label plus an empty, visibly-styled cell rather
    than being invented or dropped: Father's Name, Aadhaar No. and
    brought-forward loss set-off. Residential status renders the assumed
    constant `R/OR` with a footnote marker and an Assumptions note, because it
    is an assumption the tool does not determine. The age half of the status
    line comes from the existing `rules.resolve_age_class()` — no new age
    logic.

### Fixed
- **ITR rules — senior-citizen age-class benefit no longer leaks to
  non-residents.** `rules.resolve_age_class()`'s docstring always claimed it
  "applies only to resident Individuals," but the code only checked `status`,
  never residency — a non-resident senior/super-senior citizen wrongly
  received the higher basic exemption (300000/500000 vs 250000), which is a
  resident-only benefit. Now gated on residency too (`NR` → `'general'`;
  `RNOR`, a resident sub-status under s.6, is unaffected). A regression test
  proves NR-65 → general slabs while resident-65 → senior and resident-82 →
  super-senior still resolve as before. Every real entity on file declares
  the legacy `"Resident"` value (undeclared, defaults to `R/OR`), so this fix
  changes no real entity's computed numbers — confirmed by direct
  before/after comparison of `resolve_age_class()` across all five.
- **ITR mapping — approved corrections now actually reach a run.** The root
  cause of a real entity showing almost every mapped account as `heuristic`
  despite real review work: `apply_mapping_corrections.py`'s CLI wrote
  corrections to a separate output file instead of the live mapping file,
  requiring a manual rename step that was never performed. It now defaults
  to writing in place, with an automatic timestamped backup; an explicit
  output path remains available as an opt-in dry run. Matching was already
  GUID-based (rename-safe) — a regression test now proves it, alongside a
  persist-reload-apply round-trip test.
- **ITR mapping — fail-loud on guessed tags.** The run summary now states
  heuristic-vs-approved tag counts and prominently warns when any INCOME
  account resolved via an unreviewed heuristic guess, in addition to the
  existing `Mapping Review` sheet detail; the Reconciliation sheet also
  gained a "Mapping provenance" block for the same reason.
- **ITR schedules — 80TTA/80TTB no longer includes NBFC/HFC deposit
  interest.** For senior/super-senior filers, the deduction base wrongly
  summed savings + bank FD + NBFC/HFC interest; 80TTB (like 80TTA) only
  ever covers banks/co-operative societies/post office deposits, never
  NBFC/HFC. Fixed with a regression test; savings, bank-FD, and NBFC/HFC
  interest were already tracked on separate lines and already correctly
  excluded PPF interest and NCD/securities routing from Schedule AL and
  ExemptIncome — added synthetic tests confirming that tag-driven routing
  was already correct, since the real defects there turned out to be
  per-entity mapping-file mistags outside this project's `Data/` fence,
  not code bugs.

## [2.12.0] — 2026-07-19

### Fixed
- **Bank abstraction, P3b follow-up — legacy `run()` UI path now routes
  through the shared consolidator.** P3b deliberately left each bank's
  legacy standalone-UI-tab `run()` entry point untouched, but BoB's and
  ICICI's tabs turned out to be reachable multi-file paths: the generic
  runner stages uploads into a temp directory and `run()` iterates every
  file there, still doing the old naive `sorted(glob)` + blind concat. That
  silently misordered batches and never surfaced missing or overlapping
  periods — worse than the pipeline case, since temp-staging filenames bear
  no relation to statement chronology. Both `skill_bob.agent.run()` and
  `skill_icici.agent.run()` now build `StatementGroup`s per file and route
  through the same `bank_common.consolidate()` / `check_continuity()` helper
  that `BankSkill.parse()` already uses, so multi-file uploads are ordered
  by actual transaction date and gap/overlap warnings are surfaced in
  `run()`'s returned summary text. ICICI reuses `_read_canonical_csv()`
  verbatim (its per-file intermediates are already canonical); BoB builds
  its own group-construction block, since its intermediates are bank-native
  rather than canonical, though both go through the identical
  `StatementGroup` / `consolidate()` contract. A single-file batch — the
  dominant real-world case — remains a proven no-op for both banks.
  `BankSkill.parse()`, `bank_common/consolidate.py`, and every bank's
  single-statement extraction/parse/OCR path are untouched; HDFC and Kotak
  have no multi-file `run()` path and are unaffected.
- **BoB batch-mode line terminators normalized to CRLF.** The `_merge_csvs`
  helper that the change above replaced read part-CSVs with universal-newline
  translation and wrote with `newline=""`, emitting bare-LF output — the
  lone outlier in this codebase. Every other CSV writer, including BoB's own
  single-file fast path (`extract_bob_statement.write_csv`), both ICICI
  paths, and `canonical_io.write_canonical_csv` (which backs the pipeline's
  `parse()` output), already defaults to `csv.DictWriter`'s standard CRLF.
  BoB batch output now matches them. No production code change was required
  for this — the new `run()` code already emitted CRLF; what changed is that
  the single-file no-op tests for both banks now assert via `read_bytes()`
  against a committed pre-#92 golden
  (`tests/skill_bob/golden_single_file_run.csv`,
  `tests/skill_icici/golden_single_file_run.csv`, captured from each bank's
  direct single-file path and unchanged since `main`). The previous
  assertion compared two post-change paths via `read_text()`, silently
  folding CRLF into LF on read, and so could not have caught this in either
  direction. No row or value content changed for either bank. (PR #92,
  `48cc688`.)

## [2.11.0] — 2026-07-18

### Added
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

## [2.10.0] — 2026-07-18

### Added
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
- **Kotak Mahindra Bank onboarded as the 5th bank (`skill_kotak`).** New
  `src/agents/skill_kotak/` implements the `BankSkill` protocol
  (`detect`/`parse`/`formats`) for Kotak's ruled-table PDF statements:
  7 columns with separate Withdrawal (Dr.)/Deposit (Cr.) columns, `DD Mon
  YYYY` dates, Indian-grouped amounts, an "Opening Balance" pseudo-row
  excluded from canonical rows (mirroring BoB), multi-page overflow with no
  repeated header, and a trailing abbreviation-legend page rejected purely
  by column count (2 vs. 7) rather than a keyword blocklist. Sweep transfers
  to and from a linked FD are kept as real transactions. Adds a fully
  synthetic fixture family (`tests/skill_kotak/kotak_fixture_gen.py`)
  covering the golden path, legend exclusion, multi-page continuation,
  password-protected PDFs, and garbled-text rejection, plus registry
  round-trip and `discover() == 5` coverage in `tests/test_banks_registry.py`.
  The only pipeline edit is declarative: "Kotak" added to the bank dropdown
  and help text in `skill_gnucash_pipeline/skill.yaml`. (PR #89, `8c85f4e`.)

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

## [2.9.0] — 2026-07-17

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

### Pending
- **Frozen-build UI smoke test** (Harshal-side, PortableApps install) not
  run as part of this release — flagged pending, not blocking.

## [2.8.0] — 2026-07-16

### Added
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
