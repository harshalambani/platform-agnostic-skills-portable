# Documentation index

Map of everything under `docs/`. End-user help is generated from the `help:`
block in each skill's `skill.yaml` (see [dev/editing-help.md](dev/editing-help.md)) —
don't hand-edit the generated guides.

## For end users

- **[USER-GUIDE.html](USER-GUIDE.html)** — single self-contained guide bundled
  with the app. Open it in a browser; no server needed.
- **[user-guide/](user-guide/)** — one Markdown guide per skill (generated from
  `skill.yaml` help blocks). Same content as the HTML, split per skill.

## Guides (hand-written, task-oriented)

- **[guides/KRChoksey-GnuCash-Usage-Guide.md](guides/KRChoksey-GnuCash-Usage-Guide.md)**
  — end-to-end KR Choksey broker statement -> GnuCash import walkthrough.
- **[guides/Parser-Generator-Guide.md](guides/Parser-Generator-Guide.md)**
  — dev-time tool for creating/fixing statement parsers.

## For developers

- **[dev/editing-help.md](dev/editing-help.md)** — how to edit help content and
  regenerate the docs (`python scripts/gen_docs.py`).
- **[dev/help-block-schema.md](dev/help-block-schema.md)** — schema for the
  `help:` block in `skill.yaml`.
- **[dev/skills-reference.md](dev/skills-reference.md)** — generated developer
  inventory of every skill.

## Security

- **[security/](security/)** — security review miniprojects and the
  [findings tracker](security/2026-06-08-security-findings-tracker.md).

## History

- **[history/](history/)** — dated build notes, phase plans, and design
  documents kept for the record. Newest of note:
  [2026-07-20-itr-onpage-totals-plan.md](history/2026-07-20-itr-onpage-totals-plan.md)
  (ITR on-page totals design) and
  [2026-07-06-PHASE-V2-NATIVE-WINDOW-PLAN.md](history/2026-07-06-PHASE-V2-NATIVE-WINDOW-PLAN.md)
  (native-window shift). These are point-in-time records, not current docs.
