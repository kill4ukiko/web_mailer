@echo off
rem =====================================================================
rem  run-local.bat - start the whole webmailer stack with one command
rem
rem  Launches (each in its own window):
rem    1. Redis            (skipped if already listening on :6379)
rem    2. Web / API        (Flask via waitress)  http://127.0.0.1:5000
rem    3. Celery worker
rem    4. Celery beat
rem
rem  Usage:  run-local
rem  Stop a component by closing its window (or Ctrl+C in it).
rem =====================================================================
setlocal
cd /d "%~dp0"

set "REDIS_EXE=C:\Users\kiko\Documents\projects\webemailer\.tools\redis\Redis-8.10.2-Windows-x64-msys2\redis-server.exe"
set "PY=%~dp0env\Scripts"
set "WAITRESS=%PY%\waitress-serve.exe"
set "CELERY=%PY%\celery.exe"

echo == webmailer local launcher ==

rem ---- 1. Redis -------------------------------------------------------
netstat -ano | findstr ":6379" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
  echo Starting Redis...
  start "Redis" /min "%REDIS_EXE%" --port 6379
  timeout /t 3 >nul
) else (
  echo Redis already running on :6379.
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

echo.
echo All components launched.
echo   Web:    http://127.0.0.1:5000
echo   API:    http://127.0.0.1:5000/api/health
echo   Redis:  localhost:6379
echo.
echo Close a component's window to stop it.
pause
