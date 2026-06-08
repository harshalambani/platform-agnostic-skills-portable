# Phase 4C Plan — New skill types (non-financial)

> Written 2026-05-28 after Phase 4B landed.
> Phase 4B commit: `Phase 4B: wire cc_sort/cc_transactions, multi-file BoB, HSBC dir input, frozen-mode fixes, cleanup`

## Context

Phases 4A+4B delivered a fully pluggable skill architecture:
- `agents/registry.py` auto-discovers `agents/*/skill.yaml`
- `ui/tabs/_generic.py` renders tabs dynamically from manifests
- Supported input types: `file`, `files` (multi-upload), `directory`, `text`
- Supported modes: `agent` (LangGraph ReAct with tools), `direct` (prompt → LLM → response)
- `base_agent.py` exposes both `build_agent()` and `run_direct()`
- All 5 financial skills are wired and tested

**Gap addressed:** B5 — all existing skills are financial/document-specific.
The project goal is "skills that can work on any LLM including local ones."
Phase 4C adds 3 non-financial skills to demonstrate breadth.

## Architecture refresher

To add a new skill, create `src/agents/skill_<name>/` containing:

```
skill.yaml       — manifest (name, inputs, run_args, output, mode)
agent.py          — run() function
AGENT.md          — system prompt for the LLM
```

For `mode: "agent"` skills, also add:
```
tools.py          — @tool-decorated LangChain tools
scripts/          — deterministic extraction scripts (optional)
```

For `mode: "direct"` skills, `agent.py::run()` calls `base_agent.run_direct()`
with a user message constructed from the inputs. No tools or scripts needed.

### Key signatures

```python
# base_agent.py
def run_direct(
    user_message: str,
    system_prompt: str | None = None,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """Simple prompt → LLM → response. No tools, no agent loop."""

def build_agent(tools, system_prompt, config_path, model_override):
    """Full LangGraph ReAct agent with tool-calling."""
```

### Generic runner flow

1. Reads `skill.yaml` → builds Gradio input components
2. On Run click: validates inputs, checks dependencies, checks LLM endpoint
3. Builds `kwargs` from `run_args` template (token substitution)
4. Calls the skill's `run(**kwargs)` in a background thread
5. Shows elapsed-time ticks, then displays the result + download button

### run_args template tokens

| Token | Resolves to |
|---|---|
| `{inputs.<name>}` | Value from the input field named `<name>` |
| `{output_path}` | Full path for the output file (timestamped) |
| `{output_path_dir}` | Same as `{output_path}` — used when output is a directory |
| `{config_path}` | Path to materialised legacy config.yaml |
| `{model_override}` | Selected model name (or `None` if empty) |
| `{work_dir}` | Fresh temp directory for scratch files |

### Input types

| Type | Gradio component | Value passed to run_args |
|---|---|---|
| `file` | `gr.File` (single) | File path string |
| `files` | `gr.File` (multiple) | Temp directory path containing all uploads |
| `directory` | `gr.Textbox` | User-pasted folder path string |
| `text` | `gr.Textbox` | Text string (may be empty if not required) |

---

## 4C scope — 3 new skills

### 1. Document Summarizer (`skill_summarize`)

**Why:** Simplest possible skill. First real use of `mode: "direct"`.
Proves the architecture works beyond financial docs and beyond agent mode.

- **Input:** Single file upload (PDF or text)
- **Mode:** `direct`
- **Behaviour:** Read the file content, send it to the LLM with a summarisation
  prompt, return a markdown summary
- **Output:** `.md` file containing the summary
- **LLM requirement:** Any model that supports chat completions — no function
  calling needed. Works with Ollama, OpenAI, Anthropic, etc.

**Implementation notes:**
- `agent.py::run()` reads the file (use `pdfplumber` for PDFs, plain `open()`
  for text), constructs a user message with the content, calls `run_direct()`
- `AGENT.md` system prompt instructs the model to produce a structured summary
  with sections: Key Points, Detailed Summary, Conclusions
- For large files, chunk the content and summarise in passes (map-reduce)
  or truncate with a warning — don't let the prompt exceed the model's context
- The generic tab renders: file upload + model dropdown + run button
- `skill.yaml` should declare `requires: native_binaries: []` (pdfplumber
  handles PDF reading without Tesseract for text-extractable PDFs)

**Files to create:**
```
src/agents/skill_summarize/
  skill.yaml
  agent.py
  AGENT.md
```

### 2. Text Translator (`skill_translate`)

**Why:** Shows local LLMs can do translation without cloud APIs. Introduces
a new UX pattern: text input + dropdown selectors.

- **Input:** Text (paste or type) + source language + target language
- **Mode:** `direct`
- **Behaviour:** Send the text with translation instructions to the LLM,
  return the translated text
- **Output:** `.txt` file containing the translation
- **LLM requirement:** Same as summarizer — any chat model

**Implementation notes:**
- Three inputs in `skill.yaml`: `text` (type: text, required), `source_lang`
  (type: text, label includes common options), `target_lang` (type: text)
- `agent.py::run()` constructs a translation prompt and calls `run_direct()`
- `AGENT.md` instructs the model to translate accurately, preserve formatting,
  and note any ambiguities
- Note: the generic tab doesn't support dropdowns yet — use `type: "text"`
  with a label that lists common language codes. A future 4D enhancement
  could add a `type: "select"` input type with predefined choices.

**Files to create:**
```
src/agents/skill_translate/
  skill.yaml
  agent.py
  AGENT.md
```

### 3. CSV Data Analyzer (`skill_csv_analyzer`)

**Why:** Demonstrates tool-calling with a non-financial, non-PDF use case.
First `agent` mode skill that isn't about Indian bank statements.

- **Input:** CSV file upload + natural-language question (text)
- **Mode:** `agent` (needs pandas-based tools)
- **Behaviour:** The LLM reads the CSV structure, decides what pandas operations
  to run (via tool calls), and returns a text answer
- **Output:** `.md` file with the analysis results
- **LLM requirement:** Needs a model that supports function calling (tool use).
  Ollama models like `llama3.1`, `qwen3`, `gemma4` support this.

**Implementation notes:**
- `tools.py` provides 3–4 LangChain tools:
  - `describe_csv(csv_path)` — returns shape, columns, dtypes, head(5)
  - `query_csv(csv_path, pandas_expression)` — evaluates a pandas expression
    and returns the result (with safety guards — no `exec`/`eval` of arbitrary code;
    use a restricted subset like `df.groupby(...)`, `df.describe()`, `df[...]`)
  - `plot_csv(csv_path, plot_spec)` — optional; generates a matplotlib chart
    and saves it as PNG (stretch goal)
- `agent.py::run()` builds the agent with these tools, sends the user's question
  along with the CSV path, returns the LLM's final answer
- `AGENT.md` instructs the model to: first describe the data, then answer the
  question step by step, cite specific numbers, and flag assumptions
- Safety: the `query_csv` tool should sanitise the pandas expression to prevent
  code injection. Use an allowlist approach (permitted methods on DataFrame)
  rather than a blocklist.

**Files to create:**
```
src/agents/skill_csv_analyzer/
  skill.yaml
  agent.py
  tools.py
  AGENT.md
```

---

## Suggested execution order

1. **Summarizer** — proves `direct` mode works end-to-end (simplest)
2. **Translator** — second `direct` skill, adds multi-input text pattern
3. **CSV Analyzer** — `agent` mode with new tools (most complex)

Each skill is independent — test after completing each one before moving
to the next.

## Verification checklist (per skill)

- [ ] `skill.yaml` is valid YAML and parsed by registry (`python -c "from agents.registry import discover; print(discover())"`)
- [ ] Tab renders in the UI with correct input components
- [ ] Run completes with a local Ollama model (e.g., `qwen3`, `llama3.1`)
- [ ] Output file is produced and downloadable
- [ ] Error handling: missing file, empty input, LLM timeout — all show clear messages
- [ ] `python -m pytest tests/ -v` still passes

## What comes after 4C

- **Phase 4D** — Settings tab + agent progress streaming (see roadmap)
- **Quick wins** — CI Python version fix, build.py --clean flag, CHANGELOG catch-up
- **Testing** — unit tests for _config, _runner, _health, registry; end-to-end skill tests with synthetic data

---

## Memory files to load on start

All indexed in MEMORY.md:
- `project_paskills_portable_decisions` — 5 baseline decisions
- `project_paskills_frozen_pitfalls` — 11 PyInstaller/Gradio/Launcher landmines
- `cowork_mount_gotchas` — 3 mount failure modes + reboot fix
- `feedback_powershell_cd_prefix` — always prepend project cd to PS blocks
- `feedback_no_browser_default` — don't suggest --no-browser flag

## Also read at start (in the project root)

- `2026-05-28-PHASE-4C-PLAN.md` (this document)
- `2026-05-27-GAP-TRACKER.md` (gap tracker — update as items close)

## Key files for reference

| File | Purpose |
|---|---|
| `src/agents/registry.py` | Skill auto-discovery, SkillInfo dataclass |
| `src/agents/base_agent.py` | `build_agent()` + `run_direct()` |
| `ui/tabs/_generic.py` | Generic tab rendering + run handler |
| `ui/_runner.py` | Background-thread executor with progress ticks |
| `ui/_config.py` | Config loading, legacy materialisation |
| `ui/_health.py` | LLM endpoint health check |
| `src/agents/skill_26as/` | Reference: `agent` mode skill (file input) |
| `src/agents/skill_bob/` | Reference: `agent` mode skill (multi-file input) |

Last commit: Phase 4B
Project root: C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable

Per CLAUDE.md: plan first, wait for approval, then execute with checkpoints.
Don't delete/overwrite/rename existing files without showing the diff first.
