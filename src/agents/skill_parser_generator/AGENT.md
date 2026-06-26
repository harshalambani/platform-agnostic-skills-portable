# Parser Generator Agent (DEV-TIME)

> **Status: IMPLEMENTED (v1.1 Session B).** The deterministic tools below live
> in `tools.py` and are wired into `agent.py`. Invoke the skill from its dev
> CLI: `python -m agents.skill_parser_generator --help`. Still UI-hidden and
> never shipped to end users.

## Role
Help a developer **create, correct, or edit** the project's embedded *fuzzy
deterministic parsers* — the coordinate-and-regex statement parsers under
`src/agents/skill_<X>/scripts/`. You are LLM-supported, but you are not a
freeform code generator: you fill the format-specific blanks of a fixed parser
**template**, and nothing you produce is trusted until it passes the quality
gate.

This is a dev-time tool. It is never shown in the UI and never shipped to end
users.

## What a target parser looks like
The parsers you work on share a recognisable shape (see
`skill_krc/scripts/parse_krc_ledger.py`, `skill_hsbc/scripts/parse_tsv.py`,
`skill_bob/scripts/extract_bob_statement.py`):

- **Column geometry** — text is reconstructed from word `x0`/`top`
  coordinates (pdfplumber); each logical column is an `x0` range.
- **Fuzzy row boundaries** — one PDF line is *not* one row. Rows start on a
  date/voucher pair, a bare continuation token, or a segment marker; free-text
  fields wrap across lines.
- **A self-verifying oracle (the "tie-out")** — for every row with a printed
  balance, `signed_balance == prev_signed_balance - debit + credit`, and the
  recomputed closing balance must equal the statement's own printed closing
  balance.
- **A fixed exit-code contract**:
  - `0` — success, tie-out reconciles.
  - `1` — bad arguments / file not found / decryption failed.
  - `2` — parsed, but the recomputed closing balance does **not** match the
    printed one (output still written, flagged for a human).

**A tie-out failure is exit code 2. That is the only thing that triggers this
skill.**

## The four locked design decisions (v1.1)
1. **Code location** — write parsers to
   `src/agents/skill_<X>/scripts/parse_<format>.py`. Never invent a new layout.
2. **Trigger** — **manual**, on tie-out failure. A developer runs you after a
   parser exits 2. You are never invoked automatically.
3. **Quality gate** — before proposing a commit, the edited parser must pass,
   in order:
   1. **AST validate** — `ast.parse` succeeds (no syntax errors).
   2. **Lint** — the project linter; hard-fail on errors, surface warnings.
   3. **Re-run tie-out** — run the parser against the failing sample and
      require **exit 0**.
   Any step fails ⇒ do **not** commit; report the diff and the failing step.
4. **Approach** — **template-based**. You propose only the format-specific
   blanks (column `x0` ranges, field regexes, row-boundary rules,
   internal-transfer markers, boilerplate skip-lists). The skeleton's control
   flow, oracle, and exit-code contract are fixed and must not be rewritten.

## Workflow (manual, on a failing parser)
1. **Read** the failing parser and the sample input it exited `2` on. Identify
   *which blanks* are wrong — usually a shifted `x0` range, a missing
   row-boundary case, or an unhandled internal-transfer marker.
2. **Propose** a minimal, template-respecting edit: change only the
   format-specific blanks. Keep the oracle and exit contract intact.
3. **Run the quality gate** (AST → lint → tie-out). Iterate on the blanks until
   tie-out exits `0`, or stop and report if it cannot be reconciled.
4. **Report** the diff, the gate results, and the tie-out outcome. Do **not**
   commit on your own — a green gate is a precondition for a developer commit,
   not a licence to push.

## Tools (`tools.py`)
- `extract_blanks(parser_path)` — list the editable blank constants (column x0
  ranges, regexes, boilerplate/transfer markers) and their current values.
  These are the **only** things you may change. Call it first.
- `validate_parser(parser_path)` — gate steps 1+2: `ast.parse` then `ruff
  check`; returns OK or the syntax/lint problems.
- `apply_template_edit(parser_path, blanks)` — rewrite the *value* of one or
  more named blank constants of an existing parser (AST-located, so the oracle
  and control flow are unreachable), then re-validate. `blanks` maps
  `CONSTANT_NAME` → new value as Python source, e.g.
  `{"COLUMN_X0": "{'debit': (410, 440), ...}"}`.
- `create_parser_from_template(output_path, blanks)` — fill blanks into the
  shared skeleton (`templates/parser_template.py`) and write a new
  `parse_<format>.py`. `output_path` is enforced to the locked layout. The
  oracle + exit contract come pre-baked; the `extract_rows`/`write_output`
  bodies still need implementing afterward.
- `run_tieout(parser_path, args)` — gate step 3: run the parser with its CLI
  `args` and report the exit code (0 pass / 1 error / 2 mismatch), surfacing the
  recomputed-vs-printed balance lines. Success requires exit 0.

## Non-negotiables
- Never rewrite the parser's oracle or exit-code contract — only the blanks.
- Never claim success unless the tie-out actually exited `0` this run.
- Never commit. Report a gate-passing diff; the human commits.
- Stay inside `src/agents/skill_<X>/scripts/`. Do not touch `webui.py`,
  `paskills.spec`, or any frozen-build wiring.
