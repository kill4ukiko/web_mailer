@echo off
rem =====================================================================
rem  doctor.bat - check that this machine can run the webmailer stack
rem
rem  Run any time (also called at the end of setup-local.bat). Reports
rem  PASS/FAIL for the venv, portable tools, tunnel credentials and .env,
rem  with the exact fix for anything missing. Exits non-zero on failure.
rem
rem  Usage:  doctor
rem =====================================================================
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\doctor.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
  echo doctor found problems ^(exit code %RC%^).
) else (
  echo doctor: all checks passed.
)
pause
exit /b %RC%
