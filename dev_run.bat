@echo off
REM Run straight from source — the fastest iteration loop.
REM Edit a file, close the window, run this again. No build step at all.
REM
REM   dev_run.bat
REM
REM The console stays attached on purpose so logging output and tracebacks
REM are visible while testing.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

.venv\Scripts\python.exe run.py %*
endlocal
