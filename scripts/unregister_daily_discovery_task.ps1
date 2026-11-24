# Removes the optional daily Task Scheduler job created by register_daily_discovery_task.ps1.
Unregister-ScheduledTask -TaskName "Outreach Website Discovery" -Confirm:$false
Write-Host "Removed the 'Outreach Website Discovery' scheduled task."
