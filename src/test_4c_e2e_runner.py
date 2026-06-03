"""
test_4c_e2e_runner.py — End-to-end tests for Phase 4C skills.

Requires:
  - Ollama running on localhost:11434
  - A model pulled (gemma4 or similar)
  - Run from src/ directory

Usage:
  cd src && python test_4c_e2e_runner.py
"""
import json
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FIXTURES = ROOT / "tests" / "fixtures"
CONFIG_CANDIDATES = [
    ROOT / "staging" / "Data" / "settings" / "config.yaml",
    ROOT / "Data" / "settings" / "config.yaml",
    ROOT / "bundling" / "templates" / "DefaultData" / "settings" / "config.yaml",
]

sys.path.insert(0, str(SRC))

passed = failed = skipped = 0
results = []


def log_result(name, status, detail=""):
    global passed, failed, skipped
    if status == "PASS":
        passed += 1
        print(f"  PASS  {name}")
    elif status == "SKIP":
        skipped += 1
        print(f"  SKIP  {name} — {detail}")
    else:
        failed += 1
        print(f"  FAIL  {name} — {detail}")
    results.append((name, status, detail))


# ---------------------------------------------------------------------------
# Pre-checks
# ---------------------------------------------------------------------------
print("\n=== Pre-checks ===\n")

# 1. Find config
config_path = None
for c in CONFIG_CANDIDATES:
    if c.is_file():
        config_path = str(c)
        break
if config_path:
    log_result("config.yaml found", "PASS")
    print(f"         Using: {config_path}")
else:
    log_result("config.yaml found", "FAIL", "No config.yaml found in expected locations")
    print("Cannot proceed without config.yaml. Exiting.")
    sys.exit(1)

# 2. Check Ollama is running
try:
    req = urllib.request.Request("http://localhost:11434/api/tags")
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    models = [m["name"] for m in data.get("models", [])]
    log_result("Ollama reachable", "PASS")
    print(f"         Models: {models[:5]}{'...' if len(models) > 5 else ''}")
    if not models:
        log_result("Models available", "FAIL", "No models pulled. Run: ollama pull gemma4")
        sys.exit(1)
    else:
        log_result("Models available", "PASS")
except Exception as e:
    log_result("Ollama reachable", "FAIL", f"Cannot reach localhost:11434 — {e}")
    print("Start Ollama first. Exiting.")
    sys.exit(1)

# Pick a model (prefer gemma4, fall back to first available)
model = None
for preferred in ["gemma4", "gemma4:latest", "qwen3", "qwen3:latest", "llama3.1", "llama3.1:latest"]:
    if preferred in models:
        model = preferred
        break
if model is None:
    model = models[0]
print(f"         Using model: {model}")


# ---------------------------------------------------------------------------
# Test 1: Document Summarizer
# ---------------------------------------------------------------------------
print("\n=== Test 1: Document Summarizer (direct mode) ===\n")

try:
    from agents.skill_summarize.agent import run as summarize_run

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, dir=tempfile.gettempdir()) as f:
        out_path = f.name

    print("  Running summarizer on sample_doc.txt ...")
    t0 = time.time()
    result = summarize_run(
        file_path=str(FIXTURES / "sample_doc.txt"),
        output_path=out_path,
        config_path=config_path,
        model_override=model,
    )
    elapsed = time.time() - t0
    print(f"  Completed in {elapsed:.1f}s")

    # Verify output
    out_content = Path(out_path).read_text(encoding="utf-8")
    log_result("Summarizer: returns non-empty", "PASS" if len(result.strip()) > 50 else "FAIL",
               f"Got {len(result)} chars")
    log_result("Summarizer: output file written", "PASS" if Path(out_path).is_file() else "FAIL")
    log_result("Summarizer: contains 'Summary'",
               "PASS" if "summary" in result.lower() else "FAIL",
               "Expected 'Summary' heading in output")

    # Check for expected sections (flexible — LLM might vary)
    has_sections = sum(1 for kw in ["key point", "detail", "conclusion"]
                       if kw in result.lower())
    log_result("Summarizer: has structured sections",
               "PASS" if has_sections >= 2 else "FAIL",
               f"Found {has_sections}/3 expected section keywords")

    print(f"\n  --- Output preview (first 500 chars) ---")
    print(f"  {result[:500]}")
    print(f"  ---\n")

except Exception as e:
    tb = traceback.format_exc()
    log_result("Summarizer: execution", "FAIL", f"{e}\n{tb}")


# ---------------------------------------------------------------------------
# Test 2: Text Translator
# ---------------------------------------------------------------------------
print("\n=== Test 2: Text Translator (direct mode) ===\n")

try:
    from agents.skill_translate.agent import run as translate_run

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, dir=tempfile.gettempdir()) as f:
        out_path = f.name

    test_text = "The quick brown fox jumps over the lazy dog. This is a simple test of the translation system."
    print(f"  Translating English → Hindi ...")
    t0 = time.time()
    result = translate_run(
        text=test_text,
        source_lang="English",
        target_lang="Hindi",
        output_path=out_path,
        config_path=config_path,
        model_override=model,
    )
    elapsed = time.time() - t0
    print(f"  Completed in {elapsed:.1f}s")

    log_result("Translator: returns non-empty", "PASS" if len(result.strip()) > 10 else "FAIL",
               f"Got {len(result)} chars")
    log_result("Translator: output file written", "PASS" if Path(out_path).is_file() else "FAIL")
    # Check it's not just echoing back the English
    log_result("Translator: output differs from input",
               "PASS" if result.strip() != test_text.strip() else "FAIL",
               "Output should be in Hindi, not English")

    print(f"\n  --- Translation output ---")
    print(f"  Input:  {test_text}")
    print(f"  Output: {result[:300]}")
    print(f"  ---\n")

except Exception as e:
    tb = traceback.format_exc()
    log_result("Translator: execution", "FAIL", f"{e}\n{tb}")


# ---------------------------------------------------------------------------
# Test 3: CSV Data Analyzer
# ---------------------------------------------------------------------------
print("\n=== Test 3: CSV Data Analyzer (agent mode) ===\n")

try:
    from agents.skill_csv_analyzer.agent import run as csv_run

    with tempfile.NamedTemporaryFile(suffix=".md", delete=False, dir=tempfile.gettempdir()) as f:
        out_path = f.name

    question = "What is the total revenue by region? Which region has the highest revenue?"
    print(f"  Analyzing sales.csv: '{question}' ...")
    t0 = time.time()
    result = csv_run(
        csv_path=str(FIXTURES / "sales.csv"),
        question=question,
        output_path=out_path,
        config_path=config_path,
        model_override=model,
    )
    elapsed = time.time() - t0
    print(f"  Completed in {elapsed:.1f}s")

    log_result("CSV Analyzer: returns non-empty", "PASS" if len(result.strip()) > 50 else "FAIL",
               f"Got {len(result)} chars")
    log_result("CSV Analyzer: output file written", "PASS" if Path(out_path).is_file() else "FAIL")

    # Check for expected numbers (North=3945, South=3790, East=3750, West=2227.5)
    has_north = "3945" in result or "3,945" in result
    has_region = "north" in result.lower()
    log_result("CSV Analyzer: mentions 'North' region",
               "PASS" if has_region else "FAIL")
    log_result("CSV Analyzer: cites North revenue (3945)",
               "PASS" if has_north else "FAIL",
               "Expected '3945' or '3,945' in output")

    print(f"\n  --- Analysis output preview (first 800 chars) ---")
    print(f"  {result[:800]}")
    print(f"  ---\n")

except Exception as e:
    tb = traceback.format_exc()
    log_result("CSV Analyzer: execution", "FAIL", f"{e}\n{tb}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*60}")
print(f"E2E Results: {passed} passed, {failed} failed, {skipped} skipped")
print(f"{'='*60}")

# Write results to file for later reference
results_path = ROOT / "tests" / "e2e_results.txt"
with open(results_path, "w", encoding="utf-8") as f:
    f.write(f"Phase 4C E2E Test Results — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    f.write(f"Model: {model}\n")
    f.write(f"Config: {config_path}\n\n")
    for name, status, detail in results:
        f.write(f"[{status}] {name}")
        if detail:
            f.write(f" — {detail}")
        f.write("\n")
    f.write(f"\nTotal: {passed} passed, {failed} failed, {skipped} skipped\n")
print(f"Results saved to: {results_path}")

sys.exit(1 if failed else 0)
