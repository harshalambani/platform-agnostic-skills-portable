# Partner Compensation Reconciliation

## Role

A partner in an Indian LLP is paid through several parallel streams that
no single document reconciles: remuneration (taxable as PGBP u/s 28(v)),
share of profit (exempt u/s 10(2A)), a performance incentive awarded for
one financial year but paid out in instalments across the next one or two,
a compulsory capital contribution deducted out of those instalments,
one-off incentives (shown net of the firm's tax on a single month's
advice), and -- where part of the year was spent as an employee -- a
payroll stream with its own Form 16.

This skill takes one financial year's figures for those streams and
reports where they agree and where they do not, across seven independent
"legs":

1. Year-end position -- Compensation Advisory + the LLP capital/current account statement.
2. Incentive schedule -- the firm's payment-schedule PDF (per-month grid).
3. Monthly payouts -- 12 monthly payout advices, plus payslips for any payroll months.
4. Capital contribution -- derived by rule, cross-checked to the Advisory's stated closing.
5. Bank -- the partner's bank statement credits.
6. One-offs -- special incentive / ex-gratia, appearing on a single month's advice.
7. The return -- filed ITR + computation + Form 26AS + Form 16.

Each pairing is reported agree / variance / cannot-reconcile, in the same
Reconciliation/Exceptions sheet idiom as `skill_mf_cas`: a missing source
is reported explicitly with the reason, never silently treated as a match.

## N1 -- every rate, percentage and period is an input, never a constant

This is the single most load-bearing design rule in this package. The
firm's-tax rate, the capital-contribution rate, the capital accretion
period (months), and the remuneration-TDS section/rate/start-date all
change by financial year -- the capital rate has changed *mid-year* at
least once -- so none of them may ever be a module-level constant, a
default parameter, or an `or 0.40`-style fallback anywhere in `engine.py`
or `writer.py`. Every one of them is read out of the `drivers` block of
that financial year's structured input. A missing value produces an
explicit `CANNOT RECONCILE -- <thing> not supplied for FY<x>` result, not
a computed guess -- see `engine.driver()` and `engine.field_or_reason()`,
which every computation in this package routes through for exactly this
reason. `tests/test_skill_partner_comp_recon.py` has a dedicated test that
constructs an input with a driver missing and asserts the result is a
cannot-reconcile row, not a number.

## Stage 1 (this build) vs Stage 2 (not this build)

**Stage 1**, shipped in this PR: the computation engine (`engine.py`), the
workbook writer (`writer.py`), the manifest (`skill.yaml`), and the tests.
`run()` takes a path to a structured YAML or JSON file for one financial
year (see skill.yaml's help text for the exact shape) -- the same shape a
Stage 2 parser would eventually produce.

**Stage 2, NOT this build**: the PDF parsers under `parsers/`
(`advisory.py`, `payment_schedule.py`, `payout_advice.py`,
`llp_statement.py`). Every source document for this skill is a free-form,
unpublished layout and a personal financial record -- none are in this
repo, and only one partner's one (incomplete) year of them has ever been
seen. Writing layout/regex parsing logic from a prose description of a
document instead of a real specimen produces code that passes its own
tests and fails on the real document -- that exact failure mode has
already shipped once in this codebase (see the MF CAS skill's history).
So every `parse()` function in `parsers/` is a guarded placeholder that
raises `NotImplementedError` naming the missing specimen; each has a guard
test asserting it raises. **Do not implement these against an invented
fixture. This is deliberate and must not be "finished" opportunistically.**

## Inputs

`input_path` -- a `.yaml`/`.yml` or `.json` file for one financial year,
with these top-level keys (see `tests/fixtures/` for a full worked
example and `skill.yaml`'s `help.inputs` for the user-facing description):

- `financial_year` -- e.g. `"2025-26"`.
- `drivers` -- the per-FY rates/periods described in N1 above.
- `monthly` -- 12 entries, one per calendar month, each with remuneration,
  share_of_profit_gross, additional_share_of_profit (the one-off, net),
  firms_tax, tds, capital_transferred, and total_paid. `misc` is never
  read from here -- it is always derived (see `engine.derive_misc()`).
- `cohorts` -- the incentive cohort ledger: each cohort has an award FY,
  a gross award, and a list of instalments (`date`, `gross`, `firms_tax`,
  `capital`, `net`). Every instalment is assigned to the FY of its
  *payment* date, never its award FY -- see `engine.classify_cohort_instalments()`.
- `advisory` -- the Advisory's own stated figures for the year (currently
  `stated_closing_capital`).
- `external` -- the other four legs' totals to reconcile against:
  `bank_credits_total`, `form_26as_total_credit`,
  `return_exempt_share_of_profit`, `return_closing_capital`.
- `payroll` -- optional; a list of `{month, gross_salary, tds, net_paid}`
  rows for any months spent as a payroll employee. Not present in the
  spec's own illustrative example, but the workbook layout calls for a
  conditional "Payroll stream" sheet (written only when the input
  supplies this list), and there was no other field this could plausibly
  come from -- flagged here as an addition to the input shape rather than
  left undocumented.

## Process

1. `agent._load_input()` (the only filesystem-touching function in this
   package besides the output write) reads and parses the structured
   input file.
2. `engine.build_report()` (pure, no I/O) computes every derived quantity:
   the monthly `misc` line, the one-off gross-ups (with the
   CONFIRMED/SUSPECT roundness check), the capital rule and its mid-year
   rate-change detector, the cohort FY-straddle classification, the
   remuneration-TDS-applicability check, the firm's-tax-not-in-26AS check,
   and the leg-vs-leg reconciliation categories.
3. `writer.write_report_workbook()` (the only openpyxl-touching function)
   writes the 10-sheet workbook.

## Output

A single `.xlsx` workbook under `Data/outputs/`, with ten sheets: Logic,
Drivers, Monthly grid, Payroll stream (only if payroll rows were
supplied), One-offs, Cohorts, Capital, Reconciliation, Exceptions, and
Open items. Every rate/period/date is a literal on the Drivers sheet only;
every other sheet either reproduces a leaf figure from the input as-is or
computes a live Excel formula off Drivers (see `writer.py`'s module
docstring for the exact style vocabulary and the "=" trap it guards
against).

## Non-goals (explicit, not deferred silently)

- **No tax computation.** No slab, surcharge, cess, or exemption
  arithmetic is performed anywhere in this package -- only the derivations
  the spec this package was built from explicitly names (misc, gross-up,
  the capital rule) are computed.
- **No `.gnucash` writes.** This skill never opens or modifies a GnuCash
  book.
- **No ITR workbook injection.** Output is a standalone workbook only.
- **The PDF parsers are placeholders pending real specimens** (see Stage
  1/Stage 2 above) -- they are not a partially-done feature, they are an
  intentional stopping point.
- **Every rate, percentage and period is an input** (see N1) -- there is
  no scenario in which a later change should reintroduce a constant or a
  fallback default for one of these values.

## Safety

No PII of any kind appears in this package's code, tests, or fixtures --
every figure in `tests/fixtures/` is invented and self-consistent, chosen
to exercise every arithmetic rule in this package's design spec, not
copied or derived from any real document.
