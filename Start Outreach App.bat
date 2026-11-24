@echo off
rem Starts the Outreach dashboard in the background and opens it in your browser.
rem Logs: data\server.err.log. To stop it, run "Stop Outreach App.bat".
cd /d "%~dp0"
powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8000/ | Out-Null; exit 0 } catch { exit 1 }"
if %errorlevel%==0 (
  echo The app is already running.
) else (
  echo Starting the app...
  powershell -NoProfile -Command "Start-Process -FilePath '.\.venv\Scripts\python.exe' -ArgumentList '-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000' -WindowStyle Hidden -RedirectStandardOutput 'data\server.log' -RedirectStandardError 'data\server.err.log'"
  timeout /t 5 /nobreak >nul
)
start "" http://127.0.0.1:8000/
