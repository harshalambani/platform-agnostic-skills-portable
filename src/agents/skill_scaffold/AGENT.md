# Skill Scaffolder Agent (DEV-TIME)

> Status: v0.1.0. Deterministic, no LLM, no network, source-checkout-only.

## Role

Scaffolds a brand-new skill package under `src/agents/<skill_dir>/` --
`__init__.py`, a complete `skill.yaml` manifest (including a full `help:`
block), an `agent.py` stub, and an `AGENT.md` -- plus a starter test module
under `tests/test_<skill_dir>.py`. Everything is rendered from the fixed
template files in `templates/` by plain `{{TOKEN}}` string substitution:
there is no LLM anywhere in the generation path, no network access, and no
timestamp/version stamping, so the same inputs always produce byte-identical
output (see `tests/test_skill_scaffold.py`'s byte-stability test).

This is a dev-time tool only. It is exposed both as a top-level UI tab
(category `"dev"`, which `ui/webui.py` renders as a flat top-level tab
automatically -- the same precedent as the Parser Generator skill, no
`webui.py` change needed) and as a CLI: `python -m agents.skill_scaffold`.

## Why every generated prose field is a placeholder marker

Every prose field in the files this tool generates -- the manifest's
`description`, `overview`, `when_to_use`, per-input tooltips/accepts/gotchas,
`steps`, `tips`, `troubleshooting`, and the `AGENT.md` body -- carries a
fixed placeholder marker rather than plausible-sounding invented text. A
repo-wide guard test (`tests/test_help_coverage.py`) scans every
*registered* skill's help block for that literal marker and fails if it
finds one still there. The intent: a scaffolded skill that ships without
being filled in fails loudly and specifically, instead of shipping vague,
LLM-sounding filler prose that reads as finished but says nothing real.
Structural fields -- names, types, `entry_point`, `version` -- are always
real values, never placeholders.

The marker string itself is deliberately not spelled out in this file or in
`skill.yaml`'s own `description`/`help:` block, since this skill is itself
registered and would trip its own guard test. It appears literally only
inside `templates/` (where it belongs) and as a Python constant in the test
files that check for it.

## Engine (`scaffold.py`)

Pure filesystem operation, no side effects beyond writing files:

- Validates `skill_dir` against `^skill_[a-z0-9_]+$`, rejects path
  separators / `..` / a drive-letter prefix, and confines the resolved
  target to inside the agents root.
- Refuses outright (no writes at all) if the target skill directory, or the
  starter test module's destination, already exists -- never overwrites,
  never merges, never deletes.
- Reads the agents root from `agents.registry._AGENTS_ROOT` at call time
  (not at import time), so tests can `monkeypatch.setattr(registry,
  "_AGENTS_ROOT", tmp_path)` and the engine picks it up without any
  parameter threading beyond `agents_root=`.

The frozen-build guard (`sys.frozen` / `sys._MEIPASS`) and the
`scripts/gen_docs.py` subprocess call both live in `agent.py` and
`__main__.py`, not in `scaffold.py` itself -- that keeps `scaffold()`
hermetic for unit testing (no subprocess, no `sys` inspection) while both
real entry points still check the guard before touching the filesystem and
both still regenerate docs afterwards.

## Entry points

- `agent.py`: `run_ui(...)` (the UI's `entry_point: "agent:run_ui"`) and
  `run(...)` (an alias, for direct/programmatic callers). Both check the
  frozen guard first, then call `scaffold.scaffold(...)`, then (on success)
  run `scripts/gen_docs.py` and report its exit code, then write the report
  into `output_path` and return it as the tab's result string.
- `__main__.py`: `python -m agents.skill_scaffold --skill-dir ... --display-name ...
  [--category ...] [--mode ...] [--output-type ...] [--needs-llm ...] [--skip-gen-docs]`.
  Exit 0 on success, exit 2 under a frozen build (checked before any
  filesystem touch), exit 1 on a refusal.

## Restart requirement

`agents.registry.discover()` caches its scan of `src/agents/` for the life
of the process. A newly scaffolded skill's tab will not appear until the
app is restarted -- both the run report and the `help:` block say this
plainly so nobody goes looking for a tab that a stale cache is hiding.
