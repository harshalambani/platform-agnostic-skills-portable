# Mini-Project 02 — Harden the frozen-mode script dispatcher

**Finding:** Tracker #2 (High)
**Created:** 2026-06-08
**Affected:** `ui/webui.py` — `_maybe_dispatch_script()` / `main()`
**Owner:** _unassigned_
**Status:** OPEN

## Problem

In the PyInstaller build, `sys.executable` is `pa_skills.exe`, not `python.exe`. The skills run
their deterministic extraction scripts via `subprocess.run([sys.executable, script, ...])`, which
would otherwise just relaunch the UI. To support that, `main()` calls `_maybe_dispatch_script()`,
which treats the first CLI argument as a script path and runs it:

```python
def _maybe_dispatch_script(argv):
    first = argv[0]
    if not first.lower().endswith(".py"):
        return None
    if not Path(first).is_file():
        return None
    import runpy
    sys.argv = [first] + list(argv[1:])
    runpy.run_path(first, run_name="__main__")   # executes ANY .py on disk
```

There is **no allowlist**. Any `.py` file path passed as the first argument is executed as
`__main__` inside the app's interpreter, with the app's privileges and bundled dependencies. The
intended caller is the app's own skills, but the gate (`endswith('.py')` + `is_file()`) does not
distinguish "our bundled script" from "any file an attacker can point at."

### Why it matters (local threat model)

- **File association / shortcut / "Open with"**: if `pa_skills.exe` is ever invoked with a path
  argument (Windows file associations, a crafted `.lnk`, a dropped shortcut), an attacker-supplied
  `.py` runs.
- **Sibling process**: any process able to spawn `pa_skills.exe argv` gains code execution through
  the app's trusted, signed binary (a living-off-the-land primitive — bypasses app-allowlisting
  that trusts `pa_skills.exe`).
- The executed script runs with full bundled libs (pandas, langchain, subprocess), so it is a
  complete execution primitive, not a constrained one.

## Goal

Preserve the legitimate internal use (skills invoking their own bundled scripts) while making it
impossible to execute an arbitrary, non-bundled `.py` through the frozen executable.

## Approach

Constrain dispatch to **known, bundled** scripts only. Layer the following:

1. **Path containment (primary).** Resolve the requested path and require it to live inside the
   bundled scripts root. In frozen mode that is under `sys._MEIPASS`
   (e.g. `Path(sys._MEIPASS)/"agents"`); in source mode under the project's `src/agents`. Reject
   anything whose `Path(first).resolve()` is not a child of that root. Use
   `resolved.is_relative_to(scripts_root_resolved)` (Py3.9+) after `realpath`, to defeat `..`
   and symlink traversal.

2. **Explicit opt-in token (defense in depth).** Have the skills' subprocess callers pass a
   sentinel first arg (e.g. `--pa-internal-script`) ahead of the script path, and only enter the
   dispatch branch when that sentinel is present. Update the skill `tools.py` callers accordingly.
   This means a bare `pa_skills.exe whatever.py` no longer dispatches at all.

3. **Manifest allowlist (optional, strongest).** At build time, record the set of bundled script
   paths (or their hashes) and check membership before `runpy`. Highest assurance; more build
   wiring.

Recommended minimum: **(1) + (2)**. Add (3) if a hardened release is required.

Also: when dispatch is rejected, do **not** silently fall through to launching the UI on an
attacker-chosen argv. Log and exit non-zero.

## Files to change

- `ui/webui.py` — rewrite `_maybe_dispatch_script()` to enforce containment + sentinel; add a
  helper to resolve the bundled scripts root for source vs frozen.
- `src/agents/*/tools.py` (and `agent.py` where they shell out, e.g. `skill_hsbc/tools.py`,
  `skill_bob/tools.py`, `skill_26as/tools.py`, `skill_cc_sort`, `skill_cc_transactions`) — prepend
  the `--pa-internal-script` sentinel to the `subprocess.run([sys.executable, ...])` calls.
  - Cross-check against the upstream-mirror constraint (these live under `src/agents/**`); if the
    sentinel must be added upstream, coordinate as in MP-01. If only `webui.py` (portable-side)
    changes, prefer approach (1) containment alone so no upstream edit is needed, and treat the
    sentinel as a follow-up.
- `tests/` — new dispatcher tests.

## Test plan

1. **Reject external script:** `main(["/tmp/evil.py"])` (a real file outside the bundle) returns
   non-zero and does **not** execute it (monkeypatch `runpy.run_path` to flag if called).
2. **Reject traversal:** `main(["<scripts_root>/../../evil.py"])` and symlink-into-bundle cases
   are rejected after realpath.
3. **Accept bundled script:** a genuine bundled script path (with sentinel, if adopted) dispatches
   and returns its exit code.
4. **No-arg / UI path unchanged:** `main([])` and `main(["--no-browser"])` still launch the UI.
5. **Source vs frozen:** containment root resolves correctly with and without `sys._MEIPASS`
   (simulate by setting the attribute in the test).

## Acceptance criteria

- Arbitrary/external `.py` paths cannot be executed via `pa_skills.exe`.
- All bundled skills still run their scripts in both source and frozen builds (smoke test the
  HSBC/BoB/26AS pipelines).
- Rejection path exits non-zero and never launches the UI on the supplied argv.
- Tracker #2 marked FIXED with the merge reference.

## Watch-outs (cross-ref `project_paskills_frozen_pitfalls`)

- The PyInstaller bootloader sets `sys.stdout/stderr` to `None` in GUI builds — keep the existing
  devnull redirect ahead of any import; logging in the reject path must tolerate that.
- `sys._MEIPASS` only exists in frozen mode — guard with `getattr(sys, "_MEIPASS", None)`.
- Verify the subprocess round-trip (`pa_skills.exe -> shim`) still works after adding the sentinel;
  this path is load-bearing for every script-backed skill.

## Effort

~0.5 day for (1)+(2) including tests; add ~0.5 day if implementing the (3) manifest allowlist.
