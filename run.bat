@echo off
REM Double-click launcher for Haven. Runs run.ps1 with PowerShell 7 (pwsh).
cd /d "%~dp0"
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
echo.
echo Haven has stopped. Press any key to close this window.
pause >nul
