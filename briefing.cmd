@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Project virtual environment not found.
    exit /b 1
)

".venv\Scripts\python.exe" "build_briefing_prompt.py" --from-clipboard
