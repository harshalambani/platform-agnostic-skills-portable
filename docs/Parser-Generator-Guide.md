# Parser Generator - Usage Guide

A **dev-time** tool that helps you create, correct, and edit the project's
embedded *fuzzy deterministic parsers* - the coordinate-and-regex statement
parsers under `src/agents/skill_<X>/scripts/parse_<format>.py`. It is
LLM-supported but tightly constrained: the model may only fill the
**format-specific blanks**; it can never rewrite a parser's balance oracle or
its exit-code contract.

> It is a developer tool, not an end-user feature. It appears as its own
> top-level **Parser Generator** tab in the UI, and is also runnable from the
> command line.

---

## 1. When do you use it?

Every statement parser ends with a **self-verifying oracle** (the "tie-out"):
it recomputes the running balance and checks it against the statement's own
printed closing balance. The parser then exits with a fixed code:

| Exit code | Meaning |
|-----------|---------|
| `0` | Success - the recomputed closing balance reconciles. |
| `1` | Bad arguments / file not found / decryption failed. |
| `2` | Parsed, but the recomputed closing balance does **not** match the printed one. Output is still written and flagged. |

**A tie-out failure (exit `2`) is the trigger to reach for this tool.** It
almost always means one *blank* drifted - a column's `x0` band shifted, a new
boilerplate line needs skipping, or an internal-transfer marker changed.

You also use it to **scaffold a brand-new parser** for a bank/format you don't
have yet, starting from the shared template.

---

## 2. The core idea: blanks vs. the fixed skeleton

A parser is ~90% fixed machinery and ~10% format-specific constants. Only those
constants - the **blanks** - are editable here. They are the module-level
`UPPER_CASE` assignments, e.g.:

```python
BALANCE_X0 = (525, 555)                 # column geometry (the usual culprit)
DEBIT_X0   = (410, 440)
INTERNAL_TRANSFER_MARKER = "INTER EXCHANGE SETL"
BOILERPLATE_MARKERS = ("Closing Balance", "Print Date", ...)
```

Everything else - the line clustering, the balance oracle
(`verify_balance_invariant`), `recompute_closing`, and `main()`'s `0/1/2` exit
contract - is **off-limits**. This is enforced in code, not just by instruction:
the only edit primitive can locate and replace the *value* of an existing
`UPPER_CASE` constant and nothing else, and it rejects any change that breaks the
file's syntax. So a "fix" can never silently weaken the thing that proves the
parser is correct.

---

## 3. The quality gate

Nothing is trusted until it passes, in order:

1. **AST validate** - the file still parses (no syntax errors).
2. **Lint** - `ruff check` (advisory if ruff isn't available in your env).
3. **Re-run the tie-out** - run the parser against the failing sample and
   require **exit 0**.

If any step fails, you do **not** commit - you get the diff and the failing
step. A green gate is a *precondition* for your commit, not an automatic commit.
**The tool never commits for you.**

---

## 4. Using the UI tab

Open the **Parser Generator** tab. Fields:

| Field | What to enter |
|-------|---------------|
| **Task** | `Fix a failing parser` or `Create a new parser`. |
| **Parser** | A dropdown of the project's known parsers (with a ↻ refresh). Fix: pick the failing `parse_<format>.py`. Create: type a brand-new path (must be `src/agents/skill_<X>/scripts/parse_<format>.py`) - the field accepts custom values. |
| **Tie-out args** | *(Fix only)* the parser's own CLI arguments, used to re-run the tie-out - e.g. `statement.pdf MYPASSWORD out.xlsx`. |
| **Notes** | Fix: the symptom or the expected closing balance. Create: the format / `FORMAT_NAME`. |
| **Model** | Your configured LLM (Ollama or OpenAI-compatible). Required - this tab uses the agent. |

Click **Run**. The agent inspects the blanks, makes a minimal edit (or fills the
template), runs the gate, and reports back. The full report is shown inline and
also saved as `parser-generator-report.md` in the run's output folder (use
**Open output folder**). Review the diff, then commit yourself if you're happy.

### Worked example (Fix)

- **Task:** `Fix a failing parser`
- **Parser path:** `src/agents/skill_krc/scripts/parse_krc_ledger.py`
- **Tie-out args:** `AC109.pdf hunter2 ac109-out.xlsx`
- **Notes:** `recomputed closing 101234.50 vs printed 101230.50`

The agent will typically find a shifted `BALANCE_X0`/`CREDIT_X0` band, edit just
that constant, re-run the tie-out, and report exit `0`.

---

## 5. Using the command line

The CLI is handy for the deterministic parts (no LLM needed) and for scripting.
All commands run from the `src/` directory.

```
cd "C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable\src"
```

**List a parser's editable blanks:**
```
..\.venv\Scripts\python.exe -m agents.skill_parser_generator blanks agents/skill_krc/scripts/parse_krc_ledger.py
```

**Run the quality gate (validate + tie-out) - exits with the tie-out code:**
```
..\.venv\Scripts\python.exe -m agents.skill_parser_generator gate agents/skill_krc/scripts/parse_krc_ledger.py -- AC109.pdf hunter2 out.xlsx
```

**LLM-driven fix / create** (needs a configured model in `config.yaml`):
```
..\.venv\Scripts\python.exe -m agents.skill_parser_generator fix agents/skill_krc/scripts/parse_krc_ledger.py --note "recomputed 101234.50 vs printed 101230.50" -- AC109.pdf hunter2 out.xlsx
..\.venv\Scripts\python.exe -m agents.skill_parser_generator create agents/skill_demo/scripts/parse_demo.py --format demo
```

**See everything:**
```
..\.venv\Scripts\python.exe -m agents.skill_parser_generator --help
```

---

## 6. Creating a new parser - what you still do by hand

`Create` fills the template's blanks and gives you a syntactically valid file
with the **oracle and exit contract already correct**. But two function bodies
are deliberately left as stubs, because they are genuinely format-specific:

- `extract_rows(...)` - reconstruct rows from the PDF's word coordinates.
- `write_output(...)` - write the rows in whatever shape the skill needs.

Implement those, then run the gate. The tool gets you a correct *skeleton*, not
a finished parser for an unseen layout.

---

## 7. Safety notes

- **No silent commits.** The tool edits files and reports; you commit.
- **Blanks only.** The oracle and exit contract cannot be edited through this
  tool - by construction.
- **Locked layout.** New parsers must be written under
  `src/agents/skill_<X>/scripts/parse_<format>.py`; other paths are refused.
- **No overwrite.** `Create` refuses to clobber an existing file.

---

## 8. Tools under the hood (for reference)

| Tool | Role |
|------|------|
| `extract_blanks` | List the editable blank constants and their current values. |
| `validate_parser` | Gate steps 1-2: AST parse + `ruff check`. |
| `apply_template_edit` | Rewrite named blank constants of an existing parser, then re-validate. |
| `create_parser_from_template` | Fill blanks into the shared skeleton and write a new parser. |
| `run_tieout` | Gate step 3: run the parser, map the exit code, surface the balance lines. |

See `src/agents/skill_parser_generator/AGENT.md` for the agent's full operating
contract.
