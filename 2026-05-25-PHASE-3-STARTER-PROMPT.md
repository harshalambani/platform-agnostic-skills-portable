# Phase 3 starter prompt

> Drop the fenced block below into a fresh Cowork / Claude Code session to
> begin Phase 3. The prompt is self-contained — it does not depend on the
> Phase 2b chat history, only on memory files + the v0.2 spec + the
> Phase 2b handoff notes already in this repo.

---

```
Begin Phase 3 of the PA Skills Portable project. Phase 2b is complete and
tagged v0.2.0 (commit 0d33916). The full pipeline works end-to-end:
`python bundling\build.py` produces both staging/PASkillsPortable.exe and
dist/PASkillsPortable_<version>.zip with placeholder icons, console=False
on the frozen exe, and a working stdout/stderr shim for uvicorn.

Memory files to load on start (all indexed in MEMORY.md):
  - project_paskills_portable_decisions  (5 baseline decisions locked 2026-05-01)
  - project_paskills_frozen_pitfalls     (11 PyInstaller/Gradio/Launcher landmines)
  - cowork_mount_gotchas                 (3 mount failure modes + reboot fix)
  - feedback_powershell_cd_prefix        (always prepend project cd to PS blocks)
  - feedback_no_browser_default          (don't suggest --no-browser flag)
  - user_packaging_preference            (PortableApps Format zip, no installer)

Also read at start (in the project root):
  - 2026-05-24-PHASE-2B-NOTES.md         (the handoff that closed out Phase 2b,
                                          including the "Open Follow-ups" section)

Phase 3 scope, in priority order. Discuss with me before picking which
to land first — I may want to scope down.

  1. Real icon artwork. Replace the Pillow-generated placeholder dark-square-
     plus-blue-circle in staging/App/AppInfo/ with proper art. Drop final
     files into bundling/icons/ (appicon.ico + appicon_{16,32,75,128}.png);
     _ensure_appinfo_icons() already prefers real artwork over placeholders.
     The blocker is the artwork itself, not the wiring.

  2. Switch agents/ source from "already mirrored" to a real
     `git clone --depth 1` against the upstream URL from sources.toml,
     per locked decision 1. Currently step7_pull_agents just reports the
     in-tree count of .py files. Needs caching, a --skip-pull escape,
     and a sane behaviour when upstream is unreachable.

  3. Code-sign pa_skills.exe and PASkillsPortable.exe. Requires a real
     code-signing certificate, which I need to source. The PortableApps
     publishing flow recommends but doesn't require it.

  4. CI release pipeline. GitHub Actions workflow that on tag push runs
     `python bundling\build.py --version <tag>` inside a Windows runner,
     attaches dist/PASkillsPortable_<version>.zip to a GitHub release.
     Needs the upstream agents/ pull to work without local mirrors first
     (depends on item 2).

Project root: C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable

Per CLAUDE.md: plan first, wait for my approval, then execute with
checkpoints. Don't delete/overwrite/rename existing files without showing
me the diff first.
```

---

## Sequencing notes (not part of the prompt itself)

1. Items 1 and 3 are externally blocked (artwork, certificate) — they're tracked here so they don't get forgotten, but they can't be started by Claude alone.
2. **Item 2 (upstream agent pull) is the natural starting point.** It unblocks item 4 (CI) and matches locked decision 1 from `project_paskills_portable_decisions`.
3. Item 4 (CI) should land after item 2 — CI can't `git clone` from upstream if `build.py` still expects a local mirror.

## Cross-references

1. v0.2 spec: §10.2 (build steps), §12 (agent source pull), §10.4–10.5 (LFS-tracked binaries).
2. Phase 2b handoff: [2026-05-24-PHASE-2B-NOTES.md](./2026-05-24-PHASE-2B-NOTES.md), specifically section 5 "Open follow-ups for Phase 3".
3. Last commit: `0d33916` — Phase 2b fixes: placeholder icons, stdout/stderr shim for console=False.
4. Last tag: `v0.2.0`.
