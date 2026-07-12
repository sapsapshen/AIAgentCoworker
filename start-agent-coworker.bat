@echo off
REM Agent Coworker (Agent Relay) - double-click launcher (web UI).
REM Starts a local web server in the background and opens it in your browser.
REM   start-agent-coworker.bat            -> web UI (default)
REM   start-agent-coworker.bat console    -> legacy console mode
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if /i "%~1"=="console" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-agent-coworker.ps1" %2 %3 %4 %5 %6 %7 %8 %9
) else (
    start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0start-web.ps1"
)

endlocal
