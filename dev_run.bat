@echo off
REM Run straight from source — the fastest iteration loop.
REM
REM   dev_run.bat
REM
REM Starts the local backend and opens the interface in the default browser.
REM The console stays attached on purpose so logging output and tracebacks are
REM visible while testing. Editing frontend files only needs a browser refresh;
REM editing Python needs this script to be restarted.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

.venv\Scripts\python.exe run.py %*
endlocal
