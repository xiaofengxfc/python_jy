@echo off
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%src;%PYTHONPATH%"
"%ROOT%.venv\Scripts\python.exe" -m btrader.notify_cli %*
exit /b %ERRORLEVEL%
