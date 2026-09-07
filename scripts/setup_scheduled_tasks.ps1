<#
  Registers the two Windows Scheduled Tasks the HIIT pipeline needs.

  1. HIIT-Pipeline-Poller: lightweight, no-AI Telegram poller (telegram_bot.py).
     Handles board-photo downloads and /run_now, /pause, /resume, /status.
     Runs hourly at :30, and catches up immediately if a run was missed
     (e.g. the PC was off) via StartWhenAvailable.

  2. HIIT-Pipeline-HeavyRun: the heavy pipeline (run_pipeline.py) -- Polar
     sync, photo matching, Claude-written recommendations for new workouts,
     log regen, git commit+push, Telegram completion message. Runs once
     per logon, 5 minutes after logon, per polar_telegram_plan.md step 5.
     (Also triggered on-demand by /run_now via the poller, independent of
     this scheduled trigger.)

  Usage:
    powershell -ExecutionPolicy Bypass -File "C:\Users\gurfi\hiit-pipeline\scripts\setup_scheduled_tasks.ps1"
#>

$ErrorActionPreference = "Stop"

$pythonExe = "C:\Users\gurfi\AppData\Local\Programs\Python\Python310\python.exe"
$projectDir = "C:\Users\gurfi\hiit-pipeline"
$currentUser = "$env:COMPUTERNAME\$env:USERNAME"

# --- Task 1: lightweight poller, hourly at :30, catch up if missed ---
$now = Get-Date
$nextHalfHour = Get-Date -Hour $now.Hour -Minute 30 -Second 0
if ($now.Minute -ge 30) { $nextHalfHour = $nextHalfHour.AddHours(1) }

$pollerAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "telegram_bot.py" -WorkingDirectory $projectDir
$pollerTrigger = New-ScheduledTaskTrigger -Once -At $nextHalfHour -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$pollerSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -DontStopOnIdleEnd
$pollerPrincipal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "HIIT-Pipeline-Poller" `
    -Action $pollerAction -Trigger $pollerTrigger -Settings $pollerSettings -Principal $pollerPrincipal `
    -Description "Lightweight Telegram poller (photos + bot commands), no AI. Runs hourly at :30, catches up if missed." `
    -Force | Out-Null

Write-Host "Registered HIIT-Pipeline-Poller. First run at: $nextHalfHour, then every hour."

# --- Task 2: heavy pipeline, at logon + 5 min delay ---
$heavyAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "run_pipeline.py" -WorkingDirectory $projectDir
$heavyTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$heavyTrigger.Delay = "PT5M"
$heavySettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -DontStopOnIdleEnd
$heavyPrincipal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "HIIT-Pipeline-HeavyRun" `
    -Action $heavyAction -Trigger $heavyTrigger -Settings $heavySettings -Principal $heavyPrincipal `
    -Description "Full pipeline run (polar_sync, match_photos, recommendations, dashboard update, git push). Runs 5 minutes after logon." `
    -Force | Out-Null

Write-Host "Registered HIIT-Pipeline-HeavyRun. Triggers 5 minutes after logon for $currentUser."

Write-Host ""
Write-Host "Verifying both tasks:"
Get-ScheduledTask -TaskName "HIIT-Pipeline-Poller", "HIIT-Pipeline-HeavyRun" | Format-Table TaskName, State
