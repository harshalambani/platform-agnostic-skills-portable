# Mini-Project 05 (draft) — Scope Gradio file serving to per-run outputs

**Finding:** Tracker #5 (Medium)
**Created:** 2026-06-08
**Affected:** `ui/webui.py` (`build_app` → `allowed_paths`), `ui/tabs/_generic.py` (download wiring)
**Owner:** _unassigned_
**Status:** OPEN

## Problem

`build_app()` launches Gradio with `allowed_paths=[str(_config.output_dir().resolve())]`, which
exposes the **entire** outputs folder through the local HTTP server, and `_generic.py` hands the
DownloadButton a resolved absolute path to the produced file. The server binds `127.0.0.1` (good),
so this is not remote-exploitable, but any local process or browser tab can fetch **any** file
under `outputs/` — not just the file from the current run — and the broad allow scope is a standing
liability if the bind address or a future feature changes.

Open question to resolve first: confirm whether Gradio 6's file route permits `..` traversal out
of an `allowed_paths` entry at runtime (Tracker open question #1). If it does, severity rises.

## Goal

The UI can serve only the specific output file(s) a run just created, not the whole outputs tree,
and no path-traversal escape is possible.

## Approach

1. **Narrow the allow scope.** Prefer not allow-listing all of `outputs/`. Options, best first:
   - Write each run's output into a fresh per-run subdirectory and allow-list only that directory
     for the lifetime of the result, or
   - Rely on Gradio's own temp-file serving for `DownloadButton`/`gr.File` (copy the result into
     the component's managed temp area) rather than serving from `outputs/` directly.
2. **Validate the download path.** Before exposing `out_abs`, assert it is the file this run
   produced (exact path match), and that its realpath stays within the intended run dir
   (`is_relative_to`), defeating traversal/symlink tricks.
3. **Keep the user's saved copy separate.** Users still get the durable file in `outputs/`; the
   *served* path is the scoped/temp one. Document that distinction.
4. **Re-verify the bind + share settings** stay `server_name="127.0.0.1"`, `share=False`
   (already correct) — add a test so a regression can't flip them.

## Files to change

- `ui/webui.py` — replace the blanket `allowed_paths=[outputs]` with per-run scoping (or remove in
  favor of component-managed temp files).
- `ui/tabs/_generic.py` — route downloads through the scoped/temp path; add the realpath-containment
  assertion.
- `tests/` — add: (a) served file is reachable; (b) a sibling file in `outputs/` is **not** served;
  (c) traversal path is rejected; (d) bind=127.0.0.1 / share=False asserted.

## Acceptance criteria

- Only the current run's output is fetchable via the UI; other `outputs/` files are not served.
- Traversal / symlink escape attempts are rejected.
- Downloads still work end-to-end for file- and directory-output skills.
- Bind address and `share=False` covered by a regression test.
- Tracker #5 marked FIXED (and open question #1 answered).

## Effort

~0.5–1 day (depends on which serving model is chosen + the Gradio 6 traversal check).
