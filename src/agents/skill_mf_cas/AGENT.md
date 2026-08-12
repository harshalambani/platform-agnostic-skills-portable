# MF CAS Agent (DIRECT mode, no LLM)

## Directory name vs. document name -- read this first
This directory is still named `skill_mf_cas` and its manifest `name:` is
still `mf_cas`, but as of v2.0.0 it no longer parses a CAS (Consolidated
Account Statement). It parses KFintech's **Capital Gain / Loss Statement**,
a different RTA-issued document. The rename happened because v1's actual
target -- a raw, transaction-wise CAS PDF -- turned out not to match any
real document users receive for this purpose in practice: `parser.py`'s
regex-based `parse_cas_text` silently returned 0 schemes/0 transactions on
every real file, with no error, because real KFintech/CAMS deliveries for
this use case are the Capital Gain / Loss Statement, not the CAS. Renaming
the directory/skill `name` would break existing `Data/outputs/` filename
conventions and any external reference to `mf_cas`, so the mismatch is
documented here instead, per this repo's convention of not renaming a
skill's stable `name` lightly. `parser.py` and `lots.py` are kept fully
intact (see "Kept-but-unused modules" below) for a possible future skill
version that does target a real CAMS/MF-Central transaction-level
statement, where FIFO reconstruction of the kind `lots.py` performs would
actually be needed again.

## Role
Parses a KFintech Capital Gain / Loss Statement (encrypted .xlsx) for
mutual funds and cross-checks its three internally-redundant gain figures --
Trasaction_Details row sums, Scheme_Level_Summary totals, and Summary sheet
totals -- reporting agreement or variance per gain category. The RTA has
already matched every acquisition against its disposal and computed
short-term/long-term gains and grandfathered cost values; this skill does
**not** re-derive lots via FIFO (unlike the pre-v2.0.0 CAS-text path -- see
below). Read-only against the input xlsx; writes only new, standalone
artifact files.

## N1 assumption -- standalone artifacts only
Unchanged from v1. This skill's output lands as **standalone artifacts
only**. It does **not**:
- write into any `.gnucash` book,
- extend `skill_gnucash_import`,
- auto-inject anything into the ITR Workbook.

GnuCash books in this codebase hold mutual funds only as **cash transfers
to the AMC** (K2) -- there is no commodity-lot data inside GnuCash for
capital gains to be reconciled against automatically. This skill's job is
limited to turning the statement into a clean, reviewable feed a person (or
a future skill) can consume manually.

## Inputs
1. **cas_path** -- a single KFintech Capital Gain / Loss Statement, as an
   **.xlsx** file (K4: single entity per run, no multi-select). A `.pdf`
   input is rejected with a clear message pointing at the xlsx requirement
   -- depository `*_TXN.pdf` files and PDF parsing of the CG statement are
   explicitly out of scope. Parameter is still named `cas_path` for
   `skill.yaml` `run_args` continuity, not because the file is a CAS.
2. **cas_password** -- **required** (this statement is always
   password-protected, unlike v1's optional CAS PDF password). Never
   logged, never echoed in any exception or output file -- reuses
   `agents.bank_common.password.is_password_error` /
   `password_error_message` (now with a `doc_type="xlsx"` override for this
   skill's message wording, since every other caller of that shared helper
   is still a PDF parser).

## Process
1. **xlsx boundary** (`agent._load_workbook`) -- the only function in this
   package that touches `msoffcrypto`, `openpyxl`'s file-loading path, a
   password, or the filesystem. The file is OLE2-encrypted OOXML despite
   its `.xlsx` extension (confirmed against a real statement): opened via
   `msoffcrypto.OfficeFile(f)`, `.load_key(password=..., verify_password=True)`,
   `.decrypt(buf)` into an in-memory buffer, then `openpyxl.load_workbook(buf,
   data_only=True)`. A wrong password raises `msoffcrypto.exceptions.
   InvalidKeyError` (message mentions "password", caught by
   `is_password_error`) and is turned into a clean, password-never-echoed
   `ValueError`.
2. **Shape validation** (`cg_parser.validate_workbook_shape`) -- checks for
   all four required sheets (`Summary - Equity`, `Summary - NonEquity`,
   `Scheme_Level_Summary`, `Trasaction_Details` -- KFintech's own spelling;
   the correctly-spelled `Transaction_Details` is also tolerated) and raises
   `CGParseError` naming exactly what was expected vs. found if the
   workbook doesn't match. This is the fix for v1's silent-empty defect --
   an unrecognised workbook now fails loud instead of producing a
   0-schemes/0-transactions report with no error.
3. **Pure parse** (`cg_parser.parse_summary_sheet`,
   `parse_scheme_level_summary`, `parse_transaction_details`) -- all
   label/header-text matched, never hardcoded row/column numbers:
   - `parse_summary_sheet` walks `Summary - Equity`/`Summary - NonEquity`
     row by row, tracking which of the three gain sections (Short Term /
     Long Term With Indexation / Long Term Without Indexation) it is
     currently under from the section-header rows, and matches each field
     row's column-1 label text. The five s.234C period-bucket columns are
     matched by header text. A Total cell that is `None` (RTA leaves it
     blank when all five buckets are zero, rather than writing `0`) is
     coerced to `0.0` for arithmetic but the sheet's own value is what's
     stored -- never invented.
   - `parse_scheme_level_summary` matches the header row by column-1 text
     `"Scheme Name"`, then the fixed header set including KFintech's
     `"Outflow\nAmount"` embedded-newline label; numeric cells that arrive
     as text strings (confirmed on a real statement -- not every numeric
     cell is a native float) are coerced. The `Total` row is parsed
     separately and returned alongside the per-scheme rows, not merged into
     them.
   - `parse_transaction_details` never reads rows 1-2 (holder name, PAN --
     PII, deliberately skipped rather than parsed into any field and later
     filtered). Row 3 carries the `Section A/B/C` banner cells, used to
     segment the header row (row 4) into three column ranges so the
     duplicate `Trxn.Type`/`Date` columns that appear once under Section A
     (acquisition) and once under Section B (outflow) are matched
     correctly and never confused. Data starts row 5. ISIN is extracted
     from the trailing `(...)` in the scheme-name string
     (`cg_parser.extract_isin`).
4. **Three-way reconciliation** (`cg_parser.reconcile`) -- per gain
   category (Short Term, Long Term With Indexation, Long Term Without
   Indexation): (a) sum of that category's Section C figures across every
   `Trasaction_Details` row, (b) `Scheme_Level_Summary`'s `Total` row (or a
   summed fallback over its per-scheme rows if no Total row is present --
   explicitly noted either way), (c) `Summary - Equity`'s + `Summary -
   NonEquity`'s Total for that category's gain/loss row. If any leg cannot
   be located, the result is `agree=None` with an explicit
   `CANNOT_RECONCILE`-prefixed note (never a silently-passing blank) rather
   than a guessed match.
5. **No FIFO, no grandfathering flag, no tax computation on this path** --
   deliberately different from the old CAS-text path:
   - The RTA has already matched every buy against its sell (visible in
     `Trasaction_Details`' Section A + Section B column pairing per row),
     so there is no lot-matching left to do.
   - `GRANDFATHER_FLAG` (from `lots.py`) never fires here -- the statement's
     own `Grandfathered NAV`/`Grandfathered Cost Value` columns are
     RTA-supplied and carried straight through, not re-derived.
   - Rate/slab/exemption/indexation/234C-interest computation remains
     explicitly out of scope, same as v1 -- ST/LT figures are reported
     exactly as the statement states them.
6. **Excel/CSV writer** (`cg_writer.write_report_workbook`) -- writes the
   five-sheet workbook (MatchedLots / SchemeSummary / PeriodSummary /
   Reconciliation / Exceptions) plus a MatchedLots CSV sibling, following
   `excel_writer.py`'s styling conventions (`FONT_NAME = "Arial"`, grey
   header fill, red/yellow flag fills, autosize, freeze panes).

## Kept-but-unused modules
- `parser.py` -- the old CAS-text regex parser (`parse_cas_text`,
  `classify_row`, `SchemeBlock`, `TxnRow`). Not imported by `agent.py`'s
  `run()` on the xlsx path. Kept intact, with its existing tests unchanged,
  for a possible future CAMS/MF-Central transaction-level statement, whose
  raw-transaction shape this module was originally built for.
- `lots.py` -- the FIFO lot-derivation engine (`derive_scheme`,
  `derive_all`, `SchemeReconciliation`, `DisposalLot`). Not imported by
  `agent.py`'s `run()` on the xlsx path -- the KFintech statement needs no
  FIFO reconstruction (see Process step 5 above). Kept intact, with its
  existing tests unchanged, for the same future-statement reason as
  `parser.py`.
- `excel_writer.py` -- the old CAS-shaped report writer (Transactions/
  Holdings/RealisedGains/Exceptions, driven by `SchemeBlock`/
  `SchemeReconciliation`). Kept intact for the same reason; the new xlsx
  path uses `cg_writer.py` instead, a separate module with its own
  five-sheet shape driven by `cg_parser`'s dataclasses.

## Output
`...-MF-CAS.xlsx` in `Data/outputs/`, plus `..._matched_lots.csv` (this
repo's `Output` manifest field only carries one extension, so the sidecar
CSV is written manually alongside the XLSX, the same pattern other
multi-artifact skills in this repo use):
- **MatchedLots** -- every RTA-matched acquisition/outflow/gain row, as-is
  (one row per consumed buy lot per the RTA's own matching).
- **SchemeSummary** -- `Scheme_Level_Summary`, as given, plus its Total row.
- **PeriodSummary** -- the five s.234C bucket figures per gain section, from
  both `Summary - Equity` and `Summary - NonEquity`.
- **Reconciliation** -- the three-way agree/variance/cannot-reconcile result
  per gain category.
- **Exceptions** -- every reconciliation category that did not agree or
  could not be reconciled, collected in one place for a reviewer to work
  through.

Artifacts may carry folio numbers, scheme names, and ISINs, but **never**
PAN, investor name, address, email, or mobile number -- `parse_
transaction_details` never reads the PII rows (1-2) of `Trasaction_Details`
into any field in the first place, so there is nothing to redact
downstream.

## Reuse / relationship
- `agents.bank_common.password` -- same `is_password_error` contract every
  other password-protected-document skill in this repo uses;
  `password_error_message` gained an optional `doc_type` parameter
  (default `"PDF"`, unchanged for every other caller) so this skill's
  message correctly says "xlsx is password-protected" instead.
- `cg_writer.py` follows `excel_writer.py`'s styling conventions (same
  font, header fill, flag colours, autosize/freeze-pane helpers) so this
  skill's output continues to look like the rest of the GnuCash/ITR-family
  skills.

## v2 non-goals (explicit, not deferred silently)
- **No tax computation** -- no rate, slab, exemption, or indexation logic.
  ST/LT figures are reported exactly as the statement states them.
- **No FIFO reconstruction** -- the statement is already RTA-matched; see
  Process step 5.
- **No book writes** -- see N1 assumption above.
- **No PDF support** -- neither the depository `*_TXN.pdf` nor any PDF form
  of the Capital Gain / Loss Statement is parsed; a `.pdf` input fails loud.
- **No CAMS support** -- this skill covers KFINTECH-serviced funds only.
  CAMS issues its own, differently-shaped Capital Gain statement, not yet
  supported.
- **No multi-entity/multi-statement batching** -- one statement per run
  (K4), unchanged from v1.
- **No corporate-action modelling** (mergers/splits/scheme renames) -- a
  scheme affected by one will likely show up as a Reconciliation VARIANCE
  rather than being silently absorbed.

## Safety
- Read-only against the input xlsx; only ever writes new artifact files
  under the caller-supplied `output_path`.
- The password is never logged, never included in any exception message,
  and never written to any output file.
- Never reads `Data/itr/scrips.yaml` or any other FMV/grandfathering data
  source -- the statement's own grandfathered cost values are RTA-supplied
  and carried straight through (see Process step 5); there is nothing for
  this skill to look up.
