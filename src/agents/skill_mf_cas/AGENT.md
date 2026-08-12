# MF CAS Agent (DIRECT mode, no LLM)

## Role
Parses a CAMS/KFintech Consolidated Account Statement (CAS) PDF for mutual
funds and derives FIFO realised capital gains, per folio+scheme, purely from
the statement's own transaction rows. Read-only against the input PDF;
writes only new, standalone artifact files.

## N1 assumption -- standalone artifacts only
This skill's output lands as **standalone artifacts only**. It does **not**:
- write into any `.gnucash` book,
- extend `skill_gnucash_import`,
- auto-inject anything into the ITR Workbook.

This is a deliberate v1 boundary, not an oversight. GnuCash books in this
codebase hold mutual funds only as **cash transfers to the AMC** (K2) --
there is no commodity-lot data inside GnuCash for capital gains to be
reconciled against automatically, and `skill_itr_workbook/scripts/lots.py`'s
FIFO logic never sees MF activity for the same reason. Closing that gap (a
book-aware reconciliation, or a workbook auto-injection) is a distinct
future change with its own design questions about where derived tax facts
should live; this skill's job is limited to turning the CAS PDF into a
clean, reviewable feed a person (or a future skill) can consume manually.

## Inputs
1. **cas_path** -- a single CAS PDF (K4: single entity per run, no
   multi-select).
2. **cas_password** -- optional; required only if the PDF is
   password-protected (the common case for CAMS/KFintech email delivery).
   Never logged, never echoed in any exception or output file -- reuses
   `agents.bank_common.password.is_password_error` /
   `password_error_message` exactly, the same contract `skill_hdfc` uses.

## Process
1. **PDF boundary** (`agent._extract_pdf_text`) -- the only function in this
   package that touches `pdfplumber`, a password, or the filesystem. Opens
   the PDF via `pdfplumber.open(path, password=...)`, extracts page text,
   and returns plain text lines. No OCR in v1 -- an image-only/scanned PDF
   with no extractable text fails loud with a clear error rather than
   silently producing an empty report.
2. **Pure parse** (`parser.parse_cas_text`) -- takes plain text lines (no
   I/O) and returns one `SchemeBlock` per folio+scheme: AMC, RTA, scheme
   name, ISIN, scheme type (where stated), opening/closing unit balance, and
   every transaction row (date, description, amount, units, NAV, running
   balance), each classified `ACQUISITION` / `DISPOSAL` / `NEITHER`.
   Switch-out and redemption are disposals; switch-in, purchase, SIP, and
   dividend/IDCW reinvestment are acquisitions at the stated NAV. Any line
   that doesn't match a recognised pattern -- including any PAN, investor
   name, address, email, or mobile number a real statement's text may carry
   -- is simply unmatched and dropped; nothing of that shape is ever parsed
   into a field in the first place, so there is nothing to filter later.
3. **Pure FIFO derivation** (`lots.derive_all`) -- per folio+scheme, replays
   the transaction sequence in order, consuming acquisition lots
   FIFO. A nonzero Opening Unit Balance seeds a synthetic lot with an
   **unknown** buy date/cost; any disposal that has to dip into it is
   flagged `UNATTRIBUTED` ("unattributed -- review") rather than guessed. A
   disposal spanning multiple lots is split into **one output row per
   consumed lot** (never merged). A buy dated on or before 2018-01-31 gets
   `GRANDFATHER_FLAG` ("REVIEW - FMV 31-01-2018 needed") -- this skill does
   **not** look up `Data/itr/scrips.yaml` or invent an FMV; that lookup is
   forbidden here (PII/data-access boundary) and left to the consumer.
4. **Reconciliation, checked and reported, never silently trusted**: for
   each scheme, `opening + acquisitions - disposals == closing` (tolerance
   `UNITS_RECONCILIATION_TOLERANCE` units) and `sum(matched lot units) ==
   disposed units`. Every breach is reported on the Exceptions sheet, not
   swallowed.
5. **RTA cross-check**: if the statement text carries the RTA's own realised
   capital-gains figure for a scheme, the FIFO-derived gain is compared
   against it and any variance is reported. If the statement doesn't carry
   one, the report says so -- it never fabricates a value to compare
   against.
6. **Excel/CSV writer** (`excel_writer.write_report_workbook`) -- writes the
   four-sheet workbook (Transactions / Holdings / RealisedGains /
   Exceptions) plus CSV siblings for Transactions and RealisedGains,
   following `skill_gnucash_coverage/excel_writer.py`'s conventions
   (`FONT_NAME = "Arial"`, grey header fill, red/yellow flag fills,
   autosize, freeze panes).

## Output
`...-MF-CAS.xlsx` in `Data/outputs/`, plus `..._transactions.csv` and
`..._realised_gains.csv` siblings (this repo's `Output` manifest field only
carries one extension, so sidecar CSVs are written manually alongside the
XLSX, the same pattern other multi-artifact skills in this repo use):
- **Transactions** -- every parsed transaction row, as-is.
- **Holdings** -- one row per folio+scheme: expected vs statement closing
  units, diff, OK/BREACH.
- **RealisedGains** -- one row per consumed FIFO lot: folio, scheme, ISIN,
  scheme_type, units, buy_date, buy_nav, buy_cost, sell_date, sell_nav,
  sale_proceeds, holding_days, gain, flags. **Raw facts only** -- no tax
  rate/slab/exemption/indexation/LT-ST classification.
- **Exceptions** -- every units-reconciliation breach, matched-vs-disposed
  breach, RTA-realised-gain variance, and per-lot flag, collected in one
  place for review.

Artifacts may carry folio numbers, scheme names, and ISINs, but **never**
PAN, investor name, address, email, or mobile number -- the parser never
captures those fields in the first place (see step 2 above), so there is
nothing to redact downstream.

## Reuse / relationship
- `agents.bank_common.password` -- exact same `is_password_error` /
  `password_error_message` contract `skill_hdfc` uses for its
  password-protected-PDF handling.
- Mirrors the **philosophy** (never guess, flag for review, check
  reconciliation invariants explicitly, named module-level constants for
  thresholds) of `skill_itr_workbook/scripts/lots.py`'s FIFO lot
  reconstruction -- but does not import or reuse its code, since that
  module solves a materially different problem (reverse-engineering
  buy/sell matching from a GnuCash book's opaque splits, with straddle
  handling and Tier 1/2/3 matching). This skill's FIFO is a direct queue
  consumption over an already-ordered, explicit CAS transaction sequence --
  no equivalent ambiguity exists here except the opening-balance case
  handled in step 3 above.
- `excel_writer.py` follows `skill_gnucash_coverage/excel_writer.py`'s
  styling conventions so this skill's output looks like the rest of the
  GnuCash/ITR-family skills.

## v1 non-goals (explicit, not deferred silently)
- **No tax computation** -- no rate, slab, exemption, or indexation logic.
  `holding_days` is emitted; the consumer applies its own LT/ST threshold.
- **No LT/ST classification** -- deliberately left to the consumer, per the
  same reasoning as above.
- **No book writes** -- see N1 assumption above; this skill never touches a
  `.gnucash` file.
- **No OCR** -- a scanned/image-only CAS PDF fails loud rather than falling
  back to OCR (unlike `skill_hdfc`, which does OCR-fallback for bank
  statements); a future version could add it following that skill's
  pattern.
- **No multi-entity/multi-CAS batching** -- one CAS PDF per run (K4).
- **No corporate-action modelling** (mergers/splits/scheme renames) -- a
  scheme affected by one will likely show as a reconciliation BREACH on the
  Holdings/Exceptions sheets rather than being silently absorbed.

## Safety
- Read-only against the input PDF; only ever writes new artifact files
  under the caller-supplied `output_path`.
- The password is never logged, never included in any exception message,
  and never written to any output file.
- Never reads `Data/itr/scrips.yaml` or any other FMV/grandfathering data
  source -- the grandfathering flag is a pointer for the reviewer, not an
  auto-resolved value.
