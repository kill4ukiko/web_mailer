@echo off
rem =====================================================================
rem  setup-local.bat - portable one-shot setup for the webmailer stack
rem
rem  Safe to re-run. Installs the venv + requirements, downloads the
rem  portable tools (Redis, cloudflared), prepares .env, applies the DB
rem  migrations and sets up the Cloudflare tunnel. Run this once after
rem  cloning the project onto a new computer.
rem
rem  Usage:  setup-local
rem =====================================================================
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-local.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo setup FAILED ^(exit code %RC%^).
) else (
  echo setup finished.
)
pause
exit /b %RC%
