@echo off
REM Build the local test executable (a folder, not an installer).
REM
REM   packaging\build_dev.bat
REM
REM Incremental on purpose: no --clean, so rebuilding after a code change is
REM much faster than a release build. Output goes to dist\biTrader-dev\ and
REM never touches the release build in dist\biTrader\.
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

echo Building test executable...
.venv\Scripts\python.exe -m PyInstaller packaging\bitrader_dev.spec --noconfirm || exit /b 1

echo.
echo Done: dist\biTrader-dev\biTrader-dev.exe
echo       (console window stays open so tracebacks stay visible)
endlocal
