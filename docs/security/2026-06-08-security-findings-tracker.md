# PA Skills Portable — Security Findings Tracker

**Created:** 2026-06-08
**Scope:** Static security review of `platform-agnostic-skills-portable` (UI, runner/dispatch, skill registry, LLM config, CSV analyzer, subprocess usage, PDF decryption, build/bundling pipeline, dependency pinning).
**Threat model:** Local, single-user portable Windows app (Gradio bound to `127.0.0.1`). Primary adversary inputs are (a) untrusted documents fed to skills, (b) LLM-generated tool arguments influenced by those documents (indirect prompt injection), and (c) the build/supply chain.

## Status legend

`OPEN` not started · `IN PROGRESS` fix underway · `FIXED` merged + verified · `WONTFIX` accepted risk

## Findings

| # | Severity | Finding | Affected location | Mini-project | Status |
|---|----------|---------|-------------------|--------------|--------|
| 1 | **High** | `eval()` sandbox escape — LLM-generated expressions run via `eval(expr, {"__builtins__": {}}, {df, pd})`; regex blocklist is bypassable (string-concat, `__getattribute__`, `__reduce__`, `vars()`). Untrusted CSV → indirect prompt injection → RCE / arbitrary file read-write. | `src/agents/skill_csv_analyzer/tools.py:149` (`query_csv`, `_validate_expression`, `_BLOCKED_PATTERNS`) | [MP-01](./2026-06-08-miniproject-01-csv-eval-rce.md) | **FIXED** 2026-06-09 — see [Fix notes](#fix-notes) |
| 2 | **High** | Frozen-mode arbitrary-script dispatcher — `pa_skills.exe <anything>.py` is executed via `runpy.run_path(..., "__main__")` with no allowlist. Attacker-influenced argv (shortcut, file association, sibling process) = code execution. | `ui/webui.py` `_maybe_dispatch_script()` / `main()` | [MP-02](./2026-06-08-miniproject-02-frozen-script-dispatcher.md) | OPEN |
| 3 | Medium | Dependency integrity not enforced — `requirements.txt` uses loose ranges (`gradio>=6,<7`, `langchain>=0.3`); `requirements-lock.txt` pins versions but carries **no hashes** (`--require-hashes` absent). Frozen build can pull a newer/tampered wheel. | `requirements.txt`, `requirements-lock.txt`, `bundling/build.py` | [MP-03](./2026-06-08-miniproject-03-dependency-hashing.md) | OPEN |
| 4 | Medium | Plaintext API keys + unmasked UI field — keys stored cleartext in `Data\settings\config.yaml`, copied to an uncleaned temp `config.yaml` by `materialize_legacy_config()`, and rendered in a plain `gr.Textbox` (not password type). | `ui/_config.py` `materialize_legacy_config()`; `ui/tabs/settings.py` `ep_api_key` | — | OPEN |
| 5 | Medium | Gradio file exposure — `allowed_paths=[output_dir]` serves the entire outputs folder over the local HTTP server; DownloadButton handed a resolved absolute path. Binding is `127.0.0.1` (good) but any local process/tab can reach served files. | `ui/webui.py` `build_app()` (`allowed_paths`); `ui/tabs/_generic.py` download wiring | [MP-05](./2026-06-08-miniproject-05-gradio-file-exposure.md) | OPEN |
| 6 | Medium | Zip Slip in build pipeline — `zf.extractall()` in three spots with no path-traversal guard. URLs are SHA-256 pinned, so build-time only, but a compromised mirror could write outside `vendor/`. | `bundling/refresh_binaries.py` (3× `extractall`) | — | OPEN |
| 7 | Low | Untrusted document parsing — uploads `shutil.copy2`'d with no size/type/count limits, then fed to Poppler/Tesseract/pypdf/qpdf. Native-parser bugs are the real attack surface. Needs input caps + a patch-cadence note. | `ui/tabs/_generic.py` (`files` staging); native binaries | — | OPEN |
| 8 | Low | Plaintext PDF passwords — `find_passwords()` reads passwords from `.txt` files on disk and tries them via qpdf; passwords sit in cleartext beside the data. | `src/agents/skill_cc_sort/scripts/extract_sort_cc_pdfs.py` `find_passwords()` | — | OPEN |
| 9 | Low | Broad exception swallowing — many `except BaseException/Exception` blocks (health, update check, native setup) can mask security-relevant failures (e.g. TLS errors). | `ui/_health.py`, `ui/_update.py`, `ui/_native.py`, `src/agents/base_agent.py` | — | OPEN |
| 10 | Low | MD5 for file identity — used for dedup/identity in cc_sort. Not a security boundary today; flag so it is never promoted to one. | `src/agents/skill_cc_sort/scripts/extract_sort_cc_pdfs.py` `calculate_md5()` | — | OPEN |

## Fix notes

### Finding #1 — FIXED 2026-06-09 (MP-01)

`query_csv`, `_validate_expression`, `_BLOCKED_PATTERNS`, and the `eval()` call have been removed from `src/agents/skill_csv_analyzer/tools.py`. The module now exposes five parameterized, allowlisted tools (`describe_csv`, `aggregate_csv`, `value_counts_csv`, `filter_count_csv`, `sort_head_csv`). The LLM supplies an operation name and typed arguments; Python validates every argument against the DataFrame's real columns and a closed enum before calling pandas directly. No string supplied by the LLM is ever interpreted as code.

Files changed in `platform-agnostic-skills-portable`:

- `src/agents/skill_csv_analyzer/tools.py` — complete rewrite (415 lines; `eval()` / `query_csv` / `_validate_expression` / `_BLOCKED_PATTERNS` gone; five safe tools added)
- `src/agents/skill_csv_analyzer/AGENT.md` — updated tool documentation; removed `query_csv` reference; explicit note that there is no free-form query tool
- `tests/test_phase4c_skills.py` — `TestCSVAnalyzerSafety` class replaced with tests for all five new tools + AST-based static check
- `tests/test_csv_analyzer_security.py` — new file; 271 checks covering static analysis, 13 attack strings × 10 argument positions, and functional parity

Verification: 271/271 checks pass (standalone script; pytest run requires `langchain_core` which is build-time only).

**Upstream note:** `src/agents/**` is mirrored verbatim from `platform-agnostic-skills` at build time (`bundling/sources.toml`). The equivalent fix must also land in `harshalambani/platform-agnostic-skills` (local copy at `../platform-agnostic-skills/agents/skill_csv_analyzer/`) before the next build pull. Finding #1 is tracked as FIXED here because the portable-repo copy is remediated and verified; the upstream sync is a follow-on action.

## Open questions (need dynamic verification)

1. Gradio 6 file router behavior — confirm `allowed_paths` does not permit `..` traversal out of `output_dir` at runtime (relates to #5).
2. Update banner link — `download_url` comes from the GitHub API response and is rendered as Markdown; low risk since it is the project's own repo, but confirm no attacker-controlled link injection (relates to #9/update path).

## Suggested sequencing

1. **MP-01** and **MP-02** first (both High, both code-execution).
2. Then #3 and #5 (medium, ship-blocking for a distributed binary).
3. #4, #6, #7 as a hardening batch.
4. #8, #9, #10 as cleanup.
