<#
  Registers the Windows Scheduled Task the HIIT pipeline needs.

  HIIT-Pipeline-Poller: lightweight, no-AI Telegram poller (telegram_bot.py).
  Handles board-photo downloads, free-text queueing, and /start, /stop,
  /status. Runs hourly at :30, and catches up immediately if a run was
  missed (e.g. the PC was off) via StartWhenAvailable.

  Analysis itself (Garmin match + LLM insights) is no longer on a schedule --
  it only runs when the poller sees /start, which spawns analyze_workout.py
  on demand. There is deliberately no second "heavy" logon task anymore.

  Usage:
    powershell -ExecutionPolicy Bypass -File "C:\Users\gurfi\hiit-pipeline\scripts\setup_scheduled_tasks.ps1"
#>

$ErrorActionPreference = "Stop"

$pythonExe = "C:\Users\gurfi\AppData\Local\Programs\Python\Python310\python.exe"
$projectDir = "C:\Users\gurfi\hiit-pipeline"
$currentUser = "$env:COMPUTERNAME\$env:USERNAME"

# Lightweight poller, hourly at :30, catch up if missed.
$now = Get-Date
$nextHalfHour = Get-Date -Hour $now.Hour -Minute 30 -Second 0
if ($now.Minute -ge 30) { $nextHalfHour = $nextHalfHour.AddHours(1) }

$pollerAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "telegram_bot.py" -WorkingDirectory $projectDir
$pollerTrigger = New-ScheduledTaskTrigger -Once -At $nextHalfHour -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$pollerSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -DontStopOnIdleEnd
$pollerPrincipal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "HIIT-Pipeline-Poller" `
    -Action $pollerAction -Trigger $pollerTrigger -Settings $pollerSettings -Principal $pollerPrincipal `
    -Description "Lightweight Telegram poller (photos + free text + /start//stop//status), no AI. Runs hourly at :30, catches up if missed." `
    -Force | Out-Null

Write-Host "Registered HIIT-Pipeline-Poller. First run at: $nextHalfHour, then every hour."

# Remove the old HIIT-Pipeline-HeavyRun task if it still exists from the prior Polar-based setup.
if (Get-ScheduledTask -TaskName "HIIT-Pipeline-HeavyRun" -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName "HIIT-Pipeline-HeavyRun" -Confirm:$false
    Write-Host "Removed obsolete HIIT-Pipeline-HeavyRun task."
}

Write-Host ""
Write-Host "Verifying:"
Get-ScheduledTask -TaskName "HIIT-Pipeline-Poller" | Format-Table TaskName, State
