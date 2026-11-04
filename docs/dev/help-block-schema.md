# The `help:` block — schema reference

Every skill's user-facing help comes from one place: an optional `help:` block in
its `src/agents/skill_<name>/skill.yaml`. That single block is the source of
truth for **all four** help surfaces:

- the per-skill Markdown guides in `docs/user-guide/`,
- the standalone `docs/USER-GUIDE.html` (bundled with the app),
- the in-app **inline panel** on each skill tab, and
- the in-app **Help tab**, plus the input tooltips.

Facts already declared elsewhere in the manifest are **never repeated** in
`help:` — the renderers read them directly:

| Fact | Read from |
|------|-----------|
| Accepted file extensions | `inputs[].file_types` |
| Output filename (suffix + extension, or directory) | `output.suffix`, `output.extension`, `output.type` |
| Output folder + timestamp pattern | fixed `Data/outputs/YYYY-MM-DD-HHMMSS-…` |
| Offline vs needs-LLM | `requires.llm` |
| Native/external dependencies | `requires.native_binaries`, `requires.external_tools` |

## Schema

```yaml
help:
  overview: >              # 1 short paragraph: what it does.
    ...
  when_to_use: >           # optional: when to pick this skill vs. another.
    ...
  inputs:                  # one entry per declared input (match names to inputs[].name)
    - name: pdf_path       # REQUIRED — must equal an inputs[].name
      tooltip: "..."       # short hover / info= text under the field
      accepts: "..."       # human phrasing of accepted formats (overrides the
                           #   auto file_types string in the guide)
      gotchas: "..."       # caveats (passwords, scans, must-be-closed, etc.)
  steps:                   # numbered how-to
    - "Step one."
    - "Step two."
  outputs:
    folder: "Data/outputs/"        # optional; defaults to Data/outputs/
    files:
      - name: "…-BoB.csv"          # filename or pattern
        tooltip: "How to read it — columns, sheets, meaning."
  tips: >                  # optional closing tips.
    ...
  troubleshooting:         # optional problem -> fix table.
    - problem: "Symptom the user sees."
      fix: "What to do about it."
```

All fields are optional except each `inputs[].name` and each
`outputs.files[].name`. An entirely empty/absent `help:` block is fine — that
skill falls back to its `description` plus manifest-derived facts.

## How it is parsed

`agents/registry.py` parses the block into `SkillHelp` (with `SkillHelpInput`,
`SkillHelpOutputFile`, `SkillHelpFix`) and hangs it off `SkillInfo.help`
(`None` when empty). Both the app (`ui/_help.py`) and the doc generator
(`scripts/gen_docs.py`) read `SkillInfo.help` — no other parser exists.

## Tooltips (in-app hover)

Gradio has no native hover-tooltip API, so `ui/_help.py` uses two tiers:

1. **Tier 1 — `info=`**: always-visible helper text under each input, from
   `inputs[].tooltip` (falls back to `accepts`). Applied only to components that
   accept an `info=` kwarg (feature-detected in `maybe_info()`), so it is safe
   across Gradio versions and component types.
2. **Tier 2 — native `title=` hover**: the Outputs block renders each output
   file name with an `<abbr title="…">ⓘ</abbr>` so hovering shows the "how to
   read it" text. Styled by `HELP_CSS`.

See also [editing-help.md](editing-help.md) and the generated
[skills-reference.md](skills-reference.md).
