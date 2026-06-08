# Phase 3 — reconnaissance notes (2026-05-25)

> Written by a fresh Cowork session that was asked to "begin Phase 3" and
> push autonomously on Item 2 (real `git clone --depth 1` for `agents/`).
> Stopped immediately because the work appears to already be in the repo.
> Recording what I found so the next session has the full picture.

## 1. State described in the Phase 3 starter prompt

The starter prompt at `2026-05-25-PHASE-3-STARTER-PROMPT.md` says:

- Last commit: `0d33916` — Phase 2b fixes
- Last tag: `v0.2.0`
- `step7_pull_agents` currently just reports the in-tree `.py` count
- Phase 3 items 1, 2, 4 are open

## 2. Actual state at session start

`git log --oneline -10` on `main` (up-to-date with `origin/main`):

```
b156cdc feat: populate vendor/ with Tesseract 5.4.0 and Poppler 24.07.0
0791ac3 CI: fix Launcher Generator download (curl fallback + skip-launcher)
3c90d43 CI: GitHub Actions release pipeline on tag push
c1ff29f Phase 3: real agents/ pull from sources.toml (local + git clone with cache)
54d4f15 Phase 3: real icon artwork (gear + sparkle, rounded container)
0d33916 Phase 2b fixes: placeholder icons, stdout/stderr shim for console=False  ← starter's baseline
0c51191 Phase 2b: INI rendering, launcher generator, deterministic zip; console=False
...
```

Tags: `v0.1.0`, `v0.2.0`, `v0.3.0` (→ `0791ac3`), `v0.3.1` (→ `b156cdc`).
HEAD: `b156cdc`.

Map to Phase 3 scope:

| Item | Status on disk | Evidence |
|---|---|---|
| 1. Real icon artwork | **Already landed** | `54d4f15`. `bundling/icons/appicon.ico` plus four PNGs are present (16/32/75/128 px) and >0 bytes (8.9 KB for 128 px). |
| 2. Real `git clone --depth 1` for `agents/` | **Already landed** | `c1ff29f` — `bundling/build.py:450-512` implements `_pull_agents_git` with sha256-keyed cache at `build_pyinstaller/.agents_cache/<hash>/`, fetch-then-checkout updates, stale-cache fallback. |
| 3. Code-signing | Not started | Externally blocked on certificate, as expected. |
| 4. CI release pipeline | **Already landed** | `3c90d43` + `0791ac3` (curl fallback fix). Check `.github/workflows/` for the exact file. |

## 3. Decisions stated by the user today vs what `c1ff29f` shipped

The user's prompt today picked:

- **B2**: warn and fall back to existing `src/agents/` tree if present, hard-fail if not
- **C1**: cache at `build_pyinstaller/agents_src/`, cleared by `--clean`
- **D1**: `git_url` / `git_ref` left empty in `sources.toml`

The implementation in `c1ff29f` (current `build.py`) differs on B2 and C1:

| Decision | What `c1ff29f` does | What today's decision says |
|---|---|---|
| Cache path | `build_pyinstaller/.agents_cache/<sha256(url@ref)[:12]>/` | `build_pyinstaller/agents_src/` (flat, single dir) |
| `--clean` flag | Not present | Should exist and nuke the cache |
| Upstream unreachable, cache present | Use stale cache, log warning | (same — fine) |
| Upstream unreachable, no cache | Hard-fail | Warn and use whatever is currently in `src/agents/`; hard-fail only if `src/agents/` is empty too |
| `git_url` / `git_ref` empty | Hard-fail with "kind='git' but git_url is empty" | Same code path is fine; testability is the new concern |

`sources.toml` itself already matches D1 — `kind = "local"`, both git fields empty.

## 4. Uncommitted local changes at session start

`git diff --stat HEAD`:

```
 .gitignore                                     |  3 +++
 ui/_buildinfo.py                               |  6 ++---
 vendor/README.md                               | 33 +++++++++++++++++++++---
 vendor/poppler/bin/Lerc.dll                    | Bin 131 -> 519680 bytes
 vendor/poppler/bin/cairo.dll                   | Bin 132 -> 1016320 bytes
 ... (~110 more binary files in vendor/poppler/bin/ and vendor/tesseract/)
 117 files changed, 36 insertions(+), 6 deletions(-)
```

The binary size jumps (131 bytes → real sizes) look like LFS smudges or a
`refresh_binaries.py` run that wasn't committed. Worth deciding what to do
with this before any further work.

## 5. What this session did NOT do

- No files modified, no commits, no tags, no pushes.
- Did not run `bundling/build.py`.
- Did not touch the uncommitted vendor changes.
- Did not edit `build.py`, `sources.toml`, `bundling/icons/`, `.github/workflows/`.
- Did not write any new memory entries (memory system not found in this session — no `MEMORY.md` at any of the obvious paths).

## 6. Decisions needed before this session (or the next) can proceed

1. **Reconcile the starter prompt with the repo.** Was the Phase 3 work in
   `c1ff29f` / `54d4f15` / `3c90d43` / `0791ac3` done by a separate session
   you've forgotten, or by you in a non-Cowork tool? Should it stay, or be
   reverted to `v0.2.0` for a clean re-run?

2. **If the existing implementation stays**, do you want it adjusted to
   match today's B2 / C1 decisions (cache path rename, `--clean` flag,
   fallback to `src/agents/` instead of stale cache)? I can do that edit
   if you confirm — it's a refactor of `build.py:444-535`, not a rewrite.

3. **What to do with the 117 uncommitted vendor changes** before any
   further commits land on `main`.

4. **Item 3 (signing) and Item 1 (artwork)** — item 1 may already be
   "done" per `54d4f15`; confirm the gear-and-sparkle art is what you
   want, or this is still open. Item 3 stays parked on the certificate.

## 7. Files created this session

- `2026-05-25-PHASE-3-RECON-NOTES.md` (this file).
