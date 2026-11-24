@echo off
rem Stops the Outreach dashboard (whatever is serving http://127.0.0.1:8000).
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; if ($c) { $c | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }; 'Stopped.' } else { 'The app was not running.' }"
timeout /t 3 >nul
