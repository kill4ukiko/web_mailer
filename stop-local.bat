@echo off
rem =====================================================================
rem  stop-local.bat - stop every component started by run-local.bat
rem
rem  Matches running processes by their command line (redis-server,
rem  cloudflared, waitress-serve, the Celery worker/beat) and stops them.
rem =====================================================================
setlocal

echo Stopping webmailer components...

powershell -NoProfile -Command ^
  "$me=$PID; Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $me -and $_.CommandLine -and ($_.CommandLine -match 'redis-server|cloudflared|tunnel_watch|waitress-serve|celery_app|celery.exe') } | ForEach-Object { Write-Host ('  stopping ' + $_.Name + ' (' + $_.ProcessId + ')'); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo Done.
pause
