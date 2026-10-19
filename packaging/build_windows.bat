@echo off
REM Build the Windows desktop executable and the installer.
REM
REM   packaging\build_windows.bat            exe + installer
REM   packaging\build_windows.bat --exe-only skip the installer step
setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

echo Installing build dependencies...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || exit /b 1

echo Building executable...
.venv\Scripts\python.exe -m PyInstaller packaging\bitrader.spec --noconfirm --clean || exit /b 1
echo Done: dist\biTrader\biTrader.exe

if /i "%~1"=="--exe-only" goto end

REM NSIS is not bundled with the project. Point NSIS_DIR at a makensis.exe
REM installation, or at the portable NSIS zip extracted somewhere.
if not defined NSIS_DIR (
    if exist "%ProgramFiles(x86)%\NSIS\makensis.exe" set "NSIS_DIR=%ProgramFiles(x86)%\NSIS"
    if exist "%ProgramFiles%\NSIS\makensis.exe" set "NSIS_DIR=%ProgramFiles%\NSIS"
)
if not defined NSIS_DIR (
    echo.
    echo [SKIP] NSIS not found. Set NSIS_DIR to the folder containing makensis.exe
    echo        to also build dist\biTrader-Setup-*.exe
    goto end
)

echo Building installer...
pushd packaging
"%NSIS_DIR%\makensis.exe" installer.nsi || (popd & exit /b 1)
popd
echo Done: dist\biTrader-Setup-*.exe

:end
endlocal
