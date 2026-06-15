# Project — uv-preferred lock generator with pip-tools fallback

**Created:** 2026-06-09 by Claude (Cowork session)
**Spun off from:** `2026-06-09-MP-03-DEPENDENCY-HASHING-PLAN.md` §4.1
**Status:** ✅ complete (confirmed 2026-06-11). Script implemented,
BUILDING.md updated, all acceptance criteria met.
**Priority:** low. MP-03 ships with pip-tools-only; this is a speed/UX
upgrade, not a security fix.

---

## 1. Goal

Wrap `scripts/regen_lock.py` so it prefers `uv pip compile` when `uv` is
on PATH, and falls back to `pip-compile` (pip-tools) when it isn't.
Output format and acceptance criteria stay identical — the existing
`requirements-lock.txt` shape is unchanged.

The reason for the wrapper rather than a flat uv migration: uv produces
~10× faster resolutions and a more reliable resolver, but adds a Rust
binary that every contributor and CI would otherwise need to install
out of band. The fallback lets contributors with uv get the speedup
automatically and contributors without it keep the current path.

## 2. What changes

1. `scripts/regen_lock.py`
   1. Detect uv on PATH via `shutil.which("uv")`.
   2. If found, run `uv pip compile --generate-hashes --resolver
      backtracking --annotate --output-file requirements-lock.txt
      requirements.txt`.
   3. If not found, fall back to the current pip-tools path. Print a
      one-line note suggesting `pipx install uv` for the speedup.
   4. Verification step (UTF-8 + hashes present) runs unchanged after
      either generator.
2. `BUILDING.md` "Dependency lock file" section — add a one-line note
   that uv is optional but recommended.
3. CI: nothing. The lock-check job validates the *output file*, not
   the generator that produced it. Speed-up only matters on
   contributor laptops.

## 3. Non-goals

1. Migrating to uv-only. Keep the fallback indefinitely. uv hasn't
   reached the maturity bar where requiring it on every contributor
   is reasonable yet.
2. Switching to `uv pip install` at build time. `bundling/build.py`'s
   `--require-hashes` flow with stock pip is fine; uv at build time is
   a separate, larger change.
3. Adopting uv's own lock format (`uv.lock`). Out of scope; would
   break the MP-03 contract.

## 4. Acceptance criteria

1. `python scripts/regen_lock.py` with uv on PATH produces a lock byte-
   for-byte equivalent to the pip-tools output (modulo whitespace /
   comment ordering, which can differ — acceptable as long as
   `pip install --require-hashes` is happy with both).
2. `python scripts/regen_lock.py` without uv on PATH still works via
   pip-tools (unchanged behaviour).
3. Manual smoke: regenerate with each generator, diff the locks, run
   `python bundling/build.py --clean` against each — both succeed.

## 5. Effort

1–2 hours, including the smoke comparison. Real risk is in step 4.1 —
if uv and pip-tools resolve to materially different transitive sets,
the wrapper has to pick one and accept the implications. Quick check
before committing.

## 6. Open questions

1. **Tie-break policy if uv and pip-tools disagree on transitives.**
   Default: prefer uv's resolution. Document in `BUILDING.md`. (Should
   be rare; the resolvers converge for most real-world inputs.)
2. **Pinning uv versions.** uv evolves fast. If someone with an older
   uv produces a different lock than someone with newer uv, that's
   churn for no security gain. Mitigation: document a minimum uv
   version in `BUILDING.md`. **TBD** which floor.
