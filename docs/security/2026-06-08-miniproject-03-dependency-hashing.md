# Mini-Project 03 (draft) — Enforce hashed, pinned dependencies

**Finding:** Tracker #3 (Medium)
**Created:** 2026-06-08
**Affected:** `requirements.txt`, `requirements-lock.txt`, `bundling/build.py`
**Owner:** _unassigned_
**Status:** OPEN

## Problem

`requirements.txt` uses loose ranges (`gradio>=6,<7`, `langchain>=0.3`, etc.) and
`requirements-lock.txt` pins exact versions but carries **no hashes** — `--require-hashes` is not
used. The frozen build can therefore resolve to a newer or substituted wheel than was reviewed,
and there is no integrity check that the downloaded wheel matches a known-good artifact. (Native
binaries are already SHA-256 pinned in `binaries.toml` — this brings Python deps up to the same
bar.)

## Goal

The build installs the **exact** reviewed dependency set, with hash verification, and fails closed
if any artifact does not match.

## Approach

1. Generate a fully pinned, hashed lockfile with a hash-capable tool (`pip-compile --generate-hashes`
   from pip-tools, or `uv pip compile --generate-hashes`). Output `requirements-lock.txt` with
   `==` pins **plus** `--hash=sha256:...` for every package (including transitive deps).
2. In `bundling/build.py`, install with `pip install --require-hashes -r requirements-lock.txt`
   (and `--no-deps` so only locked entries install). Build must abort on any hash mismatch.
3. Keep `requirements.txt` as the human-edited top-level intent; regenerate the lock from it.
   Document the regen command in `BUILDING.md`.
4. Fix the lockfile encoding while here — current `requirements-lock.txt` is UTF-16; emit UTF-8
   so tooling/grep/CI handle it cleanly.
5. Add a CI step that fails if `requirements-lock.txt` is missing hashes or is out of date relative
   to `requirements.txt`.

## Files to change

- `requirements-lock.txt` — regenerate with hashes, UTF-8.
- `bundling/build.py` — switch the pip install invocation to `--require-hashes` (+ `--no-deps`).
- `BUILDING.md` — document the lock regen workflow.
- `.github/workflows/*` — add lock-freshness / hash-presence check.

## Test / verification

- Tamper test: alter one hash → build aborts with a mismatch error.
- Clean build from the hashed lock succeeds and the app launches.
- CI catches an out-of-date lock (bump a version in `requirements.txt` without regenerating).

## Acceptance criteria

- `requirements-lock.txt` is fully pinned **and** hashed, UTF-8 encoded.
- Build uses `--require-hashes` and fails closed on mismatch.
- CI enforces lock freshness + hash presence.
- Tracker #3 marked FIXED.

## Effort

~0.5 day.
