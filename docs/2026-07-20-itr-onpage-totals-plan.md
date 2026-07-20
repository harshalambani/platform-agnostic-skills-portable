# DESIGN — ITR workbook: on-page totals & calcs (2026-07-20)

Status: **DESIGN / PLAN. No build yet.** Author: Claude (autonomous, DAMAYC window).
Change owner: Harshal. This lands as a PR only — **not** auto-merged/released.

---

## 1. What Harshal asked for

> "on page totals and calcs on page - only individual items to come from source
> sheets. This helps introduce new items - like b/f losses calcs don't work -
> even if manual override is done - since other totals come from underlying
> sheets."

Concrete pain: a preparer types a **brought-forward-loss set-off** amount onto
the deliverable statement, and nothing downstream moves — Total Income, tax,
refund are unaffected.

## 2. Correction to the earlier framing (important)

The pre-build memory note said "subtotals, totals AND the tax computation are
computed in Python and written as VALUES." **That is not what the code does
today.** The code is already heavily formula-driven:

- [`write_computation_sheet`](src/agents/skill_itr_workbook/scripts/write_workbook.py#L611)
  builds GTI, Total Income, slab tax, surcharge, 87A rebate, marginal relief,
  cess and refund as **live Excel formulas** (`_slab_tax_formula`,
  `_surcharge_formula`, `_rebate_formula`), keyed off the `Rules`/`Entity`/source
  sheets. The module header literally says *"Maximally formula-driven."*
- [`write_statement_of_income`](src/agents/skill_itr_workbook/scripts/presentation.py#L311)
  renders section **sub-totals** as on-page `=SUM(...)` formulas
  ([`close_section`](src/agents/skill_itr_workbook/scripts/presentation.py#L423)).

So memory design-decision **(a)** ("push formulas all the way through the tax
calc so overrides flow end-to-end") is **already satisfied at the formula level**
— the tax calc is not frozen Python.

**The actual gap** is *where* the aggregation lives, not *whether* it is a
formula:

1. The cross-section ladder — **GTI, Total Income, tax, cess, refund** — is
   computed on the **hidden `Computation` working sheet**, and the deliverable
   `Statement of Income` page only **mirrors** it via
   `comp("gti") == ='Computation'!Bxx`
   ([presentation.py:484](src/agents/skill_itr_workbook/scripts/presentation.py#L484),
   [:494](src/agents/skill_itr_workbook/scripts/presentation.py#L494),
   [:503](src/agents/skill_itr_workbook/scripts/presentation.py#L503),
   [:528](src/agents/skill_itr_workbook/scripts/presentation.py#L528)).
   Editing a visible cell on the deliverable page therefore cannot move the
   totals — they read a *different, hidden* sheet whose leaf inputs are the
   source schedules, not the page.
2. The **b/f-loss set-off row is fully parked** — no formula, not wired into
   anything
   ([presentation.py:487-492](src/agents/skill_itr_workbook/scripts/presentation.py#L487-L492)),
   and `Computation`'s Total Income = GTI − Chapter-VI-A only (the engine has no
   b/f-loss handling at all).
3. Vestigial hook already present: `write_statement_of_income` **collects**
   `subtotal_cells` for every section but **never consumes them**
   ([presentation.py:421](src/agents/skill_itr_workbook/scripts/presentation.py#L421),
   [:432](src/agents/skill_itr_workbook/scripts/presentation.py#L432)) — this is
   exactly the anchor for an on-page `GTI = SUM(subtotals)`.

Net: the deliverable page is a **read-only mirror of a hidden calc sheet**. The
change is to make the deliverable page the **master** for the income ladder so
on-page overrides propagate.

## 3. Goal (v1)

On the `Statement of Income` deliverable page, the **income ladder** becomes
self-computing from its own on-page cells:

```
section sub-totals (=SUM of on-page leaf items)      [already on-page]
  -> Gross Total Income        = SUM(subtotal cells)           [NEW: on-page]
  -> less Chapter VI-A deduc.  = ='Deductions'!...  (leaf ref) [leaf from source]
  -> less b/f-loss set-off     = editable input cell           [NEW: real cell]
  -> Total Income              = GTI - VI-A - b/f              [NEW: on-page]
```

Everything downstream (tax, surcharge, rebate, cess, refund) **continues to be a
live formula** and must now be **driven by the page's Total Income cell**, so an
override to any leaf, to Chapter-VI-A, or to the b/f-loss cell flows end-to-end.

Leaf line-items keep coming from the source schedules by reference — unchanged.
This matches Harshal's phrase exactly: "only individual items come from source
sheets; totals/calcs are on the page."

## 4. Recommended approach — Option C (re-anchor, don't relocate)

Three options were considered:

- **A — full on-page:** inline slab tax / surcharge bands / rebate / marginal
  relief / cess directly onto the deliverable page. Rejected: turns the
  CA-facing statement into an unreadable wall of nested `IF`/slab formulas, and
  duplicates the tax machinery in a second place (divergence risk). Contradicts
  the v2.13.0 goal — "a workbook you can hand to a CA."
- **B — page aggregates, tax fully on Computation:** move only the income ladder
  on-page; leave tax on Computation reading the page's TI. Close to C.
- **C — RECOMMENDED:** move the **income ladder** (sub-totals → GTI → VI-A → b/f
  → Total Income) onto the deliverable page; **keep the tax-slab machinery on the
  `Computation` working sheet** but **re-anchor its entry point** so its slab
  base reads the **page's** normal-income cell instead of recomputing TI
  internally. The page shows `Tax on total income = ='Computation'!Bxx` as
  today, but `Computation` now consumes the page's numbers.

Why C: delivers Harshal's end-to-end propagation goal, keeps the deliverable
clean, **reuses the existing tested tax formulas** rather than rewriting slab
logic, and keeps the diff reviewable. It fully honours memory-decision (a)'s
*intent* (overrides flow through tax) — the only thing it declines is physically
relocating slab formulas onto the deliverable, which was never the point.

## 5. THE correctness trap — special-rate CG must stay carved out of the slab base

This is the one thing the build must not get wrong.

LTCG/STCG taxed at **special rates** are **not** part of slab income. Today
`Computation` keeps them separate: slab tax applies to *normal* income; CG
special-rate tax is added separately
([write_workbook.py:666](src/agents/skill_itr_workbook/scripts/write_workbook.py#L666),
`cg_layout['special_tax_cell']`). When the income ladder moves on-page:

- The page's **Total Income** includes special-rate CG (it must — the statement
  shows total income).
- But the **slab base** that `Computation` reads from the page must be
  **Total Income − special-rate-CG − (b/f-loss applied to normal income)**, i.e.
  *normal* income only. Special-rate CG continues to be taxed by the existing CG
  formula.

So the page needs (at least internally) two derived cells: **normal-income base**
and **special-rate-CG base**, and `Computation`'s slab formula must key off the
former. Getting this wrong silently taxes CG at slab rates or drops it — a
material mis-computation. **This is the primary review checkpoint and needs a
dedicated regression test.**

## 6. b/f-loss set-off — scope carefully

Harshal wants the **override to propagate**, not a full loss-set-off engine.
v1 delivers:

- A real, editable **"Less — Brought forward losses set off"** cell on the page
  (replacing the parked cell), defaulting to `0`.
- It reduces the **normal-income base** first (the common case: business/HP
  losses set off against normal income). This ordering is a **documented default**
  written as an on-sheet note; the preparer owns the number.
- It flows: `Total Income` and the slab base both drop by the entered amount;
  tax/cess/refund recompute.

**Out of scope for v1** (note in the doc, not built): head-wise set-off ordering
rules, the LTCG-loss-only-against-LTCG restriction, carry-forward schedule, and
CYLA/BFLA schedules. Those are a tax-law engine, a separate workstream. v1 is the
plumbing that makes a manual set-off actually move the numbers.

## 7. Build plan (for the Sonnet subagent)

Branch inside the target repo (non-isolated). Steps:

1. **Page income ladder** in `write_statement_of_income`
   ([presentation.py:311](src/agents/skill_itr_workbook/scripts/presentation.py#L311)):
   consume `subtotal_cells` → `GTI = SUM(subtotal cells)` on-page; add the
   on-page **Chapter VI-A** line (leaf ref to `Deductions`); make the **b/f-loss**
   row a real editable cell; `Total Income = GTI − VI-A − b/f` on-page. Derive the
   on-page **normal-income base** and **special-rate-CG base** cells.
2. **Re-anchor Computation** in `write_computation_sheet`
   ([write_workbook.py:611](src/agents/skill_itr_workbook/scripts/write_workbook.py#L611)):
   the slab/rebate/surcharge/cess formulas read the **page's** normal-income base
   and the page's Total Income, instead of recomputing GTI/TI from source leaves.
   CG special-rate tax stays as-is. Keep `comp_layout` keys stable so the page's
   `comp(...)` refs (tax/cess/refund) keep resolving.
   - Ordering note: `Statement of Income` currently writes **after** `Computation`
     ([write_workbook.py:1056-1078](src/agents/skill_itr_workbook/scripts/write_workbook.py#L1056-L1078)).
     Cross-sheet formula refs don't need creation order, but the **cell
     coordinates** the page exposes must be known to `Computation`. Cleanest:
     compute the page's ladder cell coordinates deterministically (or write the
     page first and pass its layout into `write_computation_sheet`, mirroring how
     `comp_layout` is passed the other way). Pick one and keep it explicit.
3. **Guard rails:** the existing CG reconciliation control and the v2.13.2
   fail-loud banner must still fire. Overriding cells is a preparer action; the
   generated (un-overridden) workbook must reconcile exactly as before —
   i.e. **for the default (no-override) case, every total must equal today's
   value to the paisa.** That equality is the strongest regression guarantee.

## 8. Testing (memory decision (b): formula strings + Python shadow calc)

openpyxl does not evaluate formulas, so:

1. **Formula-string assertions:** assert the on-page GTI cell is
   `=SUM(<subtotal range/cells>)`, Total Income is `=GTI−VIA−bf`, and that
   `Computation`'s slab base references the page's normal-income cell — pin the
   wiring, not just the shape.
2. **Python shadow-calc reconciliation:** a small evaluator (or reuse the
   existing model computation) computes the expected ladder for a synthetic
   fixture; assert the *default* (no-override) workbook's frozen model values
   still match today's numbers exactly (no regression), and that a simulated
   override (subtract a b/f amount in the shadow calc) yields the expected TI and
   tax — proving the wiring is arithmetically sound even though Excel isn't run.
3. **Special-rate-CG regression (section 5):** a fixture with both normal income
   and special-rate CG; assert slab base excludes CG and CG special tax is
   unchanged. This is the must-have test.
4. Full `pytest tests` green; synthetic fixtures only, **no PII**.

Optional, not required: a single opt-in headless-LibreOffice recalculation smoke
check if it's cheap; do **not** stand it up in CI for v1.

## 9. Rollout

- One PR against `main`, self-reviewed, with this doc included.
- CHANGELOG entry under a new `Unreleased`/next-version heading.
- **STOP at PR.** Do not merge or tag/release — only v2.13.2 was authorized for
  autonomous release. This change is bigger and preparer-facing; Harshal reviews.

## 10. Open questions for Harshal (non-blocking; defaults chosen)

1. **Option C vs full on-page (A).** Default: C (keep tax machinery on the
   working sheet, re-anchored). If he actually wants the slab math visible on the
   deliverable, that's A — bigger, uglier, later.
2. **b/f-loss default bucket.** Default: reduces normal (slab) income first.
   If he wants per-bucket control (normal vs CG) in v1, that's a small add.
3. **Editability UX.** Overrides are just typing into an unlocked cell. Do we
   want sheet protection with only the ladder-input cells unlocked, to prevent
   accidental edits to formula cells? Default: no protection in v1 (matches
   current workbook), revisit if preparers clobber formulas.
