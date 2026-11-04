# Editing help content

All user-facing help is generated from the `help:` block in each skill's
`skill.yaml`. To change any wording, formats, steps, output explanations, tips,
or troubleshooting:

1. Open `src/agents/skill_<name>/skill.yaml`.
2. Edit the `help:` block (schema: [help-block-schema.md](help-block-schema.md)).
3. Regenerate the docs:

   ```powershell
   python scripts\gen_docs.py
   ```

That one command rewrites:

- `docs/user-guide/<skill>.md` (per-skill end-user guide),
- `docs/USER-GUIDE.html` (the bundled standalone guide), and
- `docs/dev/skills-reference.md` (the developer inventory).

The **in-app** inline panel and Help tab read the `help:` block live from the
registry at startup, so they update the next time the app launches — no
regeneration needed for those.

## Adding help to a new skill

A new skill works without a `help:` block (it falls back to `description` plus
manifest facts), but it will fail the coverage test below until you add one.
Copy the block from a similar skill and adjust. Match each `help.inputs[].name`
and `help.outputs.files[].name` to reality.

## Keeping docs in sync (CI)

`tests/test_help_coverage.py` enforces two things:

- every **UI** skill has a non-empty `help:` block, and
- `python scripts/gen_docs.py --check` reports no stale output (i.e. someone
  edited a `help:` block but forgot to regenerate).

Run locally before committing:

```powershell
cd src
python -m pytest ..\tests\test_help_coverage.py -v
```
