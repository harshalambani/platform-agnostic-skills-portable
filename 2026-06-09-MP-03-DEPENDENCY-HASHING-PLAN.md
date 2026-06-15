# MP-03 — Dependency hashing (execution plan)

**Created:** 2026-06-09 by Claude (Cowork session)
**Tracker finding:** #3 (Medium) — `docs/security/2026-06-08-security-findings-tracker.md`
**Source spec:** `docs/security/2026-06-08-miniproject-03-dependency-hashing.md`
**Status:** IN PROGRESS — code in working tree, lock regen pending
**Recommendation:** review this plan before running step 1 below.

---

## 1. What's already in the working tree (not yet committed)

These were drafted in a prior session and are sitting unstaged in the
working tree as of `8f00cc2` (MP-02 commit):

| File | State | What it does |
|---|---|---|
| `bundling/build.py` | modified | Adds `_lock_has_hashes()`; `step3_create_venv` installs with `--require-hashes --no-deps` when the lock has hashes; warns loudly + falls back to unhashed install when it doesn't. Build won't break in either state. |
| `BUILDING.md` | modified | New "Dependency lock file" section explaining the regen workflow; step 3 row updated. |
| `scripts/regen_lock.py` | new | One-command pip-compile wrapper: installs pip-tools if absent, runs `pip-compile --generate-hashes --resolver=backtracking --annotate`, verifies output. |
| `.github/workflows/ci.yml` | new | New CI workflow with a `lock-check` job that fails if `requirements-lock.txt` is missing, UTF-16, or missing hashes; second job runs lint + security tests. |
| `docs/security/2026-06-08-miniproject-03-dependency-hashing.md` | new (untracked) | The original MP-03 spec. |
| `docs/security/2026-06-08-security-findings-tracker.md` | already committed in MP-02 | Status note says MP-03 IN PROGRESS. |

I have **not** read these top-to-bottom yet beyond the bits surfaced in
the build.py / BUILDING.md diff and the regen_lock.py script content.
The author's intent (per the in-tree spec and the tracker note) is clear
and the wiring looks right; a full review pass is still pending.

## 2. What's left

### Step A — Run the lock regen on host (~5 min)

Requires internet from the user's Windows host (pip-compile resolves
PyPI to compute hashes).

```powershell
cd C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable
python scripts\regen_lock.py
```

Expected output:
* pip-tools installed if not present.
* New `requirements-lock.txt` written as UTF-8 with `--hash=sha256:` on
  every entry, including transitive deps.
* Console reports number of pinned packages and hash entries.

### Step B — Manual review of the generated lock (~10 min)

* Diff the new lock against current `requirements-lock.txt`. Most lines
  will turn over (UTF-16 → UTF-8 + hashes added).
* Spot-check 3–5 well-known packages (e.g. `gradio`, `langchain`,
  `pandas`) — their pinned versions should match what's in
  `bundling/binaries.toml` Python expectations and what the current
  build was tested against.
* If a version moved from what's in the current lock, **decide** whether
  to accept the new version or pin the older one in `requirements.txt`
  and rerun step A.

### Step C — Smoke test the build (~10–20 min)

* From a clean venv: `python bundling\build.py --clean`.
* Watch for `installing from hashed lock (--require-hashes)` in step 3
  output. If you see the warning banner about "NO hashes" — the regen
  failed somewhere.
* Build should run to completion. PyInstaller stage and PortableApps
  wrapper stage should not be touched by this change.

### Step D — Commit everything together (~2 min)

```powershell
git add requirements.txt requirements-lock.txt `
        bundling/build.py BUILDING.md `
        scripts/regen_lock.py `
        .github/workflows/ci.yml `
        docs/security/2026-06-08-miniproject-03-dependency-hashing.md `
        docs/security/2026-06-08-security-findings-tracker.md

git commit -m "security(mp-03): enforce hashed dependency lock + CI lock-check" `
           -m "<body — see suggested message in step E below>"
```

### Step E — Update tracker to FIXED (~1 min)

In `docs/security/2026-06-08-security-findings-tracker.md`:
1. Change row #3 status from `**IN PROGRESS**` to `**FIXED** 2026-06-XX`.
2. Move the contents of the existing "Pending (PS block on return)"
   subsection into a `Finding #3 — FIXED 2026-06-XX (MP-03)` block with
   the actual commit SHA from step D.
3. Amend or follow-up commit, your choice.

### Step F — Tamper test (~5 min, optional but recommended)

To prove `--require-hashes` is doing real work:
1. Hand-edit one hash in `requirements-lock.txt` to flip a single hex
   character.
2. Run `python bundling\build.py --clean`.
3. Expect: pip aborts with `THESE PACKAGES DO NOT MATCH THE HASHES` in
   step 3, build fails closed.
4. Revert the edit. (Do **not** commit the tampered lock.)

## 3. Risks & watch-outs

1. **pip-compile resolution may fail.** Loose pins in `requirements.txt`
   (`gradio>=6,<7`, `langchain>=0.3`) leave the resolver freedom — the
   first resolved set may surprise you. If anything looks off, tighten
   the upper bound in `requirements.txt` and rerun. Resolver thrash is
   the #1 thing that turns a 5-min step A into a 1-hour step A.

2. **Transitive churn.** Hashing pins every transitive. A `pandas` minor
   bump can cascade through `numpy` / `pyarrow` / `python-dateutil`. The
   build's actual behavioural surface only depends on the top-level
   pinned packages — but the lock will mention dozens you've never heard
   of. That's expected.

3. **CI Python version mismatch.** `.github/workflows/ci.yml` runs on
   `ubuntu-latest`. `pip-compile` resolution is Python-version-sensitive
   (different wheels per `cp310` / `cp311` / `cp313`). If the lock is
   generated on Windows / Python 3.13 and CI verifies on Linux / older
   Python, hashes may not match available wheels in CI. **Resolved**
   2026-06-09: pin CI lock-check to Python 3.13 (matches `build.py`).
   See §4.2.

4. **`pip install --no-deps` is strict.** If `requirements.txt`
   inadvertently omits a runtime-required package that pip would have
   pulled in transitively, the build venv install will succeed but the
   exe will crash at runtime. Mitigated by step C (smoke test); flagged
   so reviewers don't forget.

5. **PyInstaller binary downgrades.** PyInstaller itself is in the lock.
   A surprise downgrade can change frozen-build behaviour. Spot-check
   the PyInstaller pin in step B.

## 4. Open questions — RESOLVED 2026-06-09

User picks recorded below; this section is no longer blocking.

1. **uv vs pip-tools — DECIDED: ship MP-03 on pip-tools; track uv-preferred
   wrapper as a separate project.** Wired script in `scripts/regen_lock.py`
   stays pip-tools-only for the MP-03 commit (option 1). Long-term
   target is option 3 from the surface ("prefer uv if found, fall back
   to pip-tools"), captured as a follow-on project doc:
   `2026-06-09-PROJECT-regen-lock-uv-fallback.md`. Do not expand MP-03
   scope to include the wrapper.
2. **CI Python version — DECIDED: pin to Python 3.13.** Matches
   `bundling/build.py` exactly. Single-target portable build; matrix
   would add cost with no extra signal. Action item: confirm
   `.github/workflows/ci.yml` `lock-check` job has
   `python-version: "3.13"` set before the MP-03 commit; if not, add it
   in the same commit.
3. **Hash regen cadence — DECIDED: change-driven + advisory-driven,
   quarterly review without forced regen.** Regen on every
   `requirements.txt` edit (mandatory) and whenever a pinned package
   has a relevant security advisory (reactive). Quarterly: schedule a
   review of pinned versions; only regen if review surfaces something
   to bump. Pinning is meant to be stable — forcing periodic regens
   would defeat the purpose. Document the policy in `BUILDING.md`'s
   existing "Dependency lock file" section as part of the MP-03 commit.
4. **Dev-deps in scope — DECIDED: defer entirely.** Build deps only
   for MP-03. `requirements-dev.txt` (if it exists today; needs verifying
   before the commit) stays unhashed for now. If a separate dev-deps
   lockfile is wanted longer term, log it as its own MP rather than
   expanding MP-03.

### Follow-on projects spun off

* `2026-06-09-PROJECT-regen-lock-uv-fallback.md` — from TBD 1. Implement
  uv-preferred-with-pip-tools-fallback in `scripts/regen_lock.py`.
  Independent of MP-03; can land any time after.

## 5. Acceptance criteria

* `requirements-lock.txt` is UTF-8 and every entry has at least one
  `--hash=sha256:` line.
* `bundling/build.py` step 3 logs `installing from hashed lock` with
  no warning banner.
* `python bundling\build.py --clean` runs to completion against the
  new lock.
* Tamper test (step F) shows pip aborts on hash mismatch.
* CI lock-check job is green on a branch that contains the new lock.
* Tracker finding #3 is marked FIXED with the commit SHA.

## 6. Effort estimate

* Step A–E happy path: ~30 minutes.
* Step F (recommended): +5 minutes.
* If resolver thrash hits: +30–60 minutes.

## 7. Out of scope (defer)

* `--allow-unsafe` packages (pip, setuptools, wheel) — accept defaults.
* uv migration.
* dev-deps lockfile.
* signing the lock file itself.

## 8. Suggested commit message (for step D)

```
security(mp-03): enforce hashed dependency lock + CI lock-check

Closes Finding #3 (Medium) in
docs/security/2026-06-08-security-findings-tracker.md.

bundling/build.py step 3 now installs with --require-hashes --no-deps
when the lock contains hashes. Warn + fallback when it doesn't, so the
build does not break the first time a contributor regenerates the lock.

scripts/regen_lock.py: one-command pip-compile --generate-hashes regen.
  Installs pip-tools if absent; verifies UTF-8 + hash presence.

.github/workflows/ci.yml: new workflow.
  - lock-check: fail if lock missing, UTF-16, or has no hashes.
  - test: lint + security tests on every push/PR.

BUILDING.md: new Dependency lock file section.

requirements-lock.txt: regenerated as UTF-8 with full SHA-256 hashes
across N packages (top-level + transitive).

Verified: --require-hashes refuses install on hand-tampered hash
(tamper test, see plan §3.F).

Tracker finding #3 -> FIXED.
```
