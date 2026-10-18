@echo off
REM Build the Windows desktop executable with PyInstaller.
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

echo Installing build dependencies...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || exit /b 1

echo Building...
.venv\Scripts\python.exe -m PyInstaller packaging\bitrader.spec --noconfirm --clean || exit /b 1

echo.
echo Done: dist\biTrader\biTrader.exe
endlocal
