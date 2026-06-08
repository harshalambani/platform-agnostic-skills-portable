# Phase 4B Plan — Wire remaining skills + cleanup

> Written 2026-05-27 after Phase 4A landed (pluggable skill architecture).
> Phase 4A commit: `Phase 4A: pluggable skill architecture — registry, generic tabs, dynamic discovery`

## Context

Phase 4A delivered:
- `src/agents/registry.py` — auto-discovers `agents/*/skill.yaml`
- `ui/tabs/_generic.py` — builds Gradio tabs from skill manifests
- `base_agent.py` — added `run_direct()` for lightweight (non-agent) skills
- All 5 skills now have `skill.yaml` manifests and appear as dynamic tabs
- Old hand-coded tabs backed up as `.bak` files

All 5 tabs render in the UI. However, cc_sort and cc_transactions have
never been tested end-to-end in the portable build, and there are several
cleanup items from the 4A transition.

## 4B scope — 7 items

### 1. Validate cc_sort tab UX and fix input handling

cc_sort takes a **folder path** (not a file upload) + an optional password
string. The generic tab renders these as a `gr.Textbox` (paste folder path)
and a second `gr.Textbox` (password). Verify:

- The folder-path textbox works correctly when a Windows path is pasted
- The password field passes through comma-separated values correctly
- The `run_args` template substitution handles empty password gracefully
- Error messaging is clear when qpdf is missing (external tool check)

**Files:** `src/agents/skill_cc_sort/skill.yaml`, `ui/tabs/_generic.py`

### 2. Validate cc_transactions tab UX

cc_transactions also takes a **folder path** (the `Decrypted_PDFs_Correct/`
output from cc_sort). Verify:

- Folder path input works
- The `output_excel` run_arg maps to `{output_path}` correctly
- The `config_path` and `model_override` args are passed but unused (agent.py
  ignores them) — confirm no crash

**Files:** `src/agents/skill_cc_transactions/skill.yaml`, `ui/tabs/_generic.py`

### 3. Fix HSBC run_args — work_dir and pdf_dir semantics

HSBC's `agent.run()` signature is:

```python
def run(pdf_dir, work_dir, output_path, title="HSBC Statement", config_path=..., model_override=...)
```

But the generic tab sends a single file upload (not a directory). The current
`skill.yaml` maps `pdf_dir` to a file input. Two issues:

- `pdf_dir` expects a directory path, but the generic tab uploads a single
  file. Either: (a) change the input type to `directory` (textbox), or
  (b) have the generic runner extract the parent directory of the uploaded file.
- `work_dir` is mapped to `{work_dir}` which the generic runner creates as
  a temp dir — verify this works.
- `title` has a default — confirm it's not required in `run_args`.

**Decision needed:** Should HSBC accept a single PDF (extract dir from it)
or a folder path? The old hand-coded tab used a single file upload.

**Files:** `src/agents/skill_hsbc/skill.yaml`, `ui/tabs/_generic.py`

### 4. Handle subprocess calls in frozen mode

cc_sort and cc_transactions both use `subprocess.run([sys.executable, script, ...])`
which in frozen mode would relaunch `pa_skills.exe`. The `_maybe_dispatch_script()`
shim in `webui.py` handles this for the main entry point, but verify that the
agent's subprocess calls route through it correctly.

Pitfall #1 from `project_paskills_frozen_pitfalls` documents this exact issue.
The existing skills (26as, bob) work because their tools.py calls go through
the shim. Confirm cc_sort and cc_transactions follow the same pattern.

**Files:** `src/agents/skill_cc_sort/agent.py`, `src/agents/skill_cc_transactions/agent.py`

### 5. Delete old .bak tab files

Once the generic tabs are verified working for 26AS, BoB, and HSBC:

```powershell
Remove-Item ui/tabs/skill_26as.py, ui/tabs/skill_26as.py.bak
Remove-Item ui/tabs/skill_bob.py, ui/tabs/skill_bob.py.bak
Remove-Item ui/tabs/skill_hsbc.py, ui/tabs/skill_hsbc.py.bak
```

Also clean up the `__pycache__` directories under `ui/tabs/`.

### 6. Update gap tracker

Mark the following items as resolved in `2026-05-27-GAP-TRACKER.md`:

- B1 (cc_sort/cc_transactions unwired) — resolved by 4A + 4B
- B3 (skills not auto-discoverable) — resolved by 4A
- B4 (no lightweight execution path) — resolved by 4A (`run_direct`)
- C1 (stale Home tab text) — resolved by 4A
- C3 (no dynamic skill listing) — resolved by 4A

Add new status notes for items addressed partially.

### 7. Run full test suite + source-mode smoke test

```powershell
cd 'C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable'
.\.venv\Scripts\Activate.ps1
python -m pytest tests/ -v
python -m ui.webui
```

Verify all 5 tabs render and the three previously-working skills (26AS, BoB,
HSBC) still function correctly via the generic tab runner.

## Suggested order

Items 3 → 1 → 2 → 4 → 7 → 5 → 6

Fix HSBC first (known issue), then validate the two new skills, check frozen
mode compat, smoke test, clean up, update tracker.

## Files likely to be modified

| File | Change |
|---|---|
| `src/agents/skill_hsbc/skill.yaml` | Fix input type (file vs directory) |
| `ui/tabs/_generic.py` | Possibly add file→directory extraction logic |
| `src/agents/skill_cc_sort/agent.py` | Verify subprocess pattern |
| `src/agents/skill_cc_transactions/agent.py` | Verify subprocess pattern |
| `2026-05-27-GAP-TRACKER.md` | Mark resolved items |
| `ui/tabs/skill_*.py` + `.bak` | Delete |
| `tests/test_smoke.py` | Possibly add cc_sort/cc_transactions specific tests |

---

## What comes after 4B

See [2026-05-27-ROADMAP-BEYOND-4B.md](./2026-05-27-ROADMAP-BEYOND-4B.md) for
the full roadmap covering Phases 4C (new skill types), 4D (UI improvements),
5 (infrastructure hardening), 6 (testing & documentation), and 7 (publish &
distribute). That document also includes a priority table and quick-win items
that can land in any session.
