@echo off
cd /d "%~dp0\src"
echo === PA Skills Phase 4C — End-to-End Tests (requires Ollama running) ===
echo.

python run_4c_e2e.py
echo.
pause
