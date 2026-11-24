<#
  OPTIONAL: run website discovery once a day even when VS Code and the dashboard are closed.

  Creates a Windows Task Scheduler task for YOUR user account that runs:
      .venv\Scripts\python.exe -m app.discovery run --if-not-run-today --trigger scheduled
  If discovery already completed today (for example when you opened VS Code), it does nothing.
  If the computer was off at the scheduled time, it runs as soon as possible afterwards.
  The task only runs while you are logged in. It discovers websites only; it never sends email.

  Usage (from the project folder, in PowerShell):
      .\scripts\register_daily_discovery_task.ps1            # daily at 09:00
      .\scripts\register_daily_discovery_task.ps1 -Time 14:30
  Remove it again with .\scripts\unregister_daily_discovery_task.ps1
  Results are appended to data\discovery.log and shown on the Website discovery page.
#>
param([string]$Time = "09:00")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python virtual environment not found at $python. Follow the README setup first."
}

$action = New-ScheduledTaskAction -Execute $python `
    -Argument "-m app.discovery run --if-not-run-today --trigger scheduled" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName "Outreach Website Discovery" -Action $action -Trigger $trigger -Settings $settings `
    -Description "Daily website discovery for the outreach dashboard (never sends email)." -Force | Out-Null

Write-Host "Scheduled 'Outreach Website Discovery' daily at $Time for $env:USERNAME."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName 'Outreach Website Discovery'"
