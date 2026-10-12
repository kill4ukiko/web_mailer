@echo off
rem =====================================================================
rem  run-local.bat - start the whole webmailer stack with one command
rem
rem  Launches (each in its own window):
rem    1. Redis            (skipped if already listening on :6379)
rem    2. Web / API        (Flask via waitress)  http://127.0.0.1:5000
rem    3. Celery worker
rem    4. Celery beat
rem    5. Cloudflare tunnel  ->  https://track.goprilad.com
rem
rem  All paths are relative to this file, so the project can be moved to
rem  another computer. Run setup-local.bat first on a fresh machine.
rem
rem  Usage:  run-local
rem  Stop a component by closing its window (or run stop-local.bat).
rem =====================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY=%~dp0env\Scripts"
set "WAITRESS=%PY%\waitress-serve.exe"
set "CELERY=%PY%\celery.exe"
set "CLOUDFLARED=%~dp0.tools\cloudflared\cloudflared.exe"
set "CF_CONFIG=%~dp0.tools\cloudflared\config.yml"
set "PUBLIC_URL=https://track.goprilad.com"

rem --- locate redis-server.exe anywhere under .tools\redis ----------------
set "REDIS_EXE="
for /f "delims=" %%f in ('dir /b /s "%~dp0.tools\redis\redis-server.exe" 2^>nul') do set "REDIS_EXE=%%f"

echo == webmailer local launcher ==

if not exist "%WAITRESS%" (
  echo ERROR: virtualenv not found at "%~dp0env".
  echo        Run setup-local.bat first.
  pause
  exit /b 1
)

rem ---- 1. Redis -------------------------------------------------------
if not defined REDIS_EXE (
  echo WARNING: redis-server.exe not found under .tools\redis.
  echo          Run setup-local.bat, or start Redis yourself on :6379.
) else (
  netstat -ano | findstr ":6379" | findstr "LISTENING" >nul 2>&1
  if errorlevel 1 (
    echo Starting Redis...
    start "Redis" /min "%REDIS_EXE%" --port 6379
    timeout /t 3 >nul
  ) else (
    echo Redis already running on :6379.
  )
)

rem ---- 2. Web / API ---------------------------------------------------
echo Starting web on http://127.0.0.1:5000 ...
start "Web" /min "%WAITRESS%" --call --host=127.0.0.1 --port=5000 app:create_app

rem ---- 3. Celery worker ----------------------------------------------
echo Starting Celery worker...
start "Worker" /min "%CELERY%" -A app.workers.celery_app:celery worker -Q celery,monitor --loglevel=info --pool=solo

rem ---- 4. Celery beat -------------------------------------------------
echo Starting Celery beat...
start "Beat" /min "%CELERY%" -A app.workers.celery_app:celery beat --loglevel=info

rem ---- 5. Cloudflare tunnel -------------------------------------------
if not exist "%CLOUDFLARED%" (
  echo WARNING: cloudflared not found. Run setup-local.bat for a public URL.
) else (
  if not exist "%CF_CONFIG%" (
    echo WARNING: %CF_CONFIG% missing ^(run setup-local.bat^). Skipping tunnel.
  ) else (
    echo Starting Cloudflare tunnel ^(%PUBLIC_URL%^) ...
    start "Tunnel" /min "%CLOUDFLARED%" tunnel --config "%CF_CONFIG%" run
  )
)

echo.
echo Waiting for the API to come up...
set "API_UP="
for /l %%i in (1,1,20) do (
  if not defined API_UP (
    powershell -NoProfile -Command "try{ $r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:5000/api/health; if($r.StatusCode -eq 200){exit 0} }catch{}; exit 1" >nul 2>&1
    if !errorlevel! equ 0 set "API_UP=1"
    if not defined API_UP timeout /t 1 >nul
  )
)

echo.
if defined API_UP (
  echo All components launched. API is healthy.
) else (
  echo Components launched, but the API did not answer within ~20s.
  echo Check the "Web" window for errors.
)
echo   Web:      http://127.0.0.1:5000
echo   API:      http://127.0.0.1:5000/api/health
echo   Public:   %PUBLIC_URL%
echo   Redis:    localhost:6379
echo.
echo Close a component's window to stop it, or run stop-local.bat.
pause
