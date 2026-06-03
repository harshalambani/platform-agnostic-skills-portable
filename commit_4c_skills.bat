@echo off
cd /d "%~dp0"

echo === Commit 1: Document Summarizer ===
git add src/agents/skill_summarize/skill.yaml src/agents/skill_summarize/AGENT.md src/agents/skill_summarize/agent.py
git commit -m "feat(skill_summarize): add Document Summarizer skill (direct mode, PDF/text input, .md output)"
echo.

echo === Commit 2: Text Translator (agent.py + AGENT.md + updated skill.yaml) ===
git add src/agents/skill_translate/AGENT.md src/agents/skill_translate/agent.py src/agents/skill_translate/skill.yaml
git commit -m "feat(skill_translate): add Text Translator skill (direct mode, select inputs, .txt output)"
echo.

echo === Commit 3: CSV Data Analyzer ===
git add src/agents/skill_csv_analyzer/skill.yaml src/agents/skill_csv_analyzer/AGENT.md src/agents/skill_csv_analyzer/agent.py src/agents/skill_csv_analyzer/tools.py
git commit -m "feat(skill_csv_analyzer): add CSV Data Analyzer skill (agent mode, pandas tools, safety guards)"
echo.

echo === Commit 4: Phase 4C test suite + fixtures ===
git add tests/test_phase4c_skills.py tests/fixtures/sales.csv tests/fixtures/sample_doc.txt tests/fixtures/empty.txt
git commit -m "test: add 60-test suite for Phase 4C skills with synthetic fixtures"
echo.

echo === Commit 5: Gap tracker update ===
git add 2026-05-27-GAP-TRACKER.md
git commit -m "docs: update gap tracker for Phase 4C completion (B5 resolved, E1/E2 partial)"
echo.

echo === Commit 6: Phase 4C plan ===
git add 2026-05-28-PHASE-4C-PLAN.md
git commit -m "docs: add Phase 4C plan (non-financial skills)"
echo.

echo === Done. Log: ===
git log --oneline -8
echo.
pause
