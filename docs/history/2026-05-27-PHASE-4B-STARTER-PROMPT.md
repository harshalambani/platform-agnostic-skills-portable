# Phase 4B starter prompt

> Drop the fenced block below into a fresh Cowork / Claude Code session to
> begin Phase 4B. The prompt is self-contained — it depends on memory files
> + the plan doc already in this repo.

---

```
Begin Phase 4B of the PA Skills Portable project. Phase 4A is complete and
committed. The pluggable skill architecture is live — all 5 skills have
skill.yaml manifests, a registry auto-discovers them, and dynamic Gradio
tabs render for each one.

Memory files to load on start (all indexed in MEMORY.md):
  - project_paskills_portable_decisions  (5 baseline decisions locked 2026-05-01)
  - project_paskills_frozen_pitfalls     (11 PyInstaller/Gradio/Launcher landmines)
  - cowork_mount_gotchas                 (3 mount failure modes + reboot fix)
  - feedback_powershell_cd_prefix        (always prepend project cd to PS blocks)
  - feedback_no_browser_default          (don't suggest --no-browser flag)

Also read at start (in the project root):
  - 2026-05-27-PHASE-4B-PLAN.md          (the detailed 7-item plan for this session)
  - 2026-05-27-GAP-TRACKER.md            (full project gap tracker — update as items close)

Phase 4B scope: wire cc_sort + cc_transactions properly, fix HSBC input
semantics, verify frozen-mode subprocess compatibility, clean up old
hand-coded tab files, update the gap tracker. Full details in the plan doc.

Key architecture context:
  - src/agents/registry.py scans agents/*/skill.yaml and exposes SkillInfo objects
  - ui/tabs/_generic.py builds Gradio tabs from SkillInfo (file upload, textbox,
    model dropdown, run button, download button)
  - base_agent.py has both build_agent() (LangGraph ReAct) and run_direct()
    (simple prompt → LLM → response)
  - The generic runner in _generic.py handles run_args template substitution:
    {inputs.<name>}, {output_path}, {config_path}, {model_override}, {work_dir}
  - Old hand-coded tabs (skill_26as.py, skill_bob.py, skill_hsbc.py) are backed
    up as .bak files and no longer imported — delete after verifying generic tabs

Last commit: Phase 4A: pluggable skill architecture
Last tag: v0.3.1 (Phase 3 — tag not yet updated for 4A)

Project root: C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable

Per CLAUDE.md: plan first, wait for my approval, then execute with
checkpoints. Don't delete/overwrite/rename existing files without showing
me the diff first.
```
