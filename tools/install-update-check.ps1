param([switch]$RunNow)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskName = 'PromptPilot-CheckUpdates'
$python = Join-Path $taskRoot '.venv312\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Prepare .venv312 first.' }
if ((Get-TimeZone).Id -ne 'Ekaterinburg Standard Time') { throw 'Set the intended Windows time zone (Asia/Yekaterinburg) before registering this local-time schedule.' }
$stateDir = Join-Path $taskRoot '_local\maintenance'
$runtime = Join-Path $stateDir 'runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
# A reviewed, pinned checker survives checkout changes and never calls Codex.
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'check_updates.py') -Destination $runtime
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'notify-custom.ps1') -Destination $runtime
$action = New-ScheduledTaskAction -Execute $python -Argument ('"{0}" --state-dir "{1}"' -f (Join-Path $runtime 'check_updates.py'), $stateDir) -WorkingDirectory $taskRoot
$trigger = New-ScheduledTaskTrigger -Daily -At '10:00'
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Description -ne 'PromptPilot: read-only GitHub check; notifications only; no agents or deployment.')) {
    throw 'A different task already uses this name; refusing to replace it.'
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'PromptPilot: read-only GitHub check; notifications only; no agents or deployment.' -Force | Out-Null
Export-ScheduledTask -TaskName $taskName | Set-Content -LiteralPath (Join-Path $stateDir 'scheduled-task.xml') -Encoding utf8
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $runtime 'check_updates.py'), (Join-Path $runtime 'notify-custom.ps1') | Select-Object Path,Hash | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateDir 'runtime-hashes.json') -Encoding utf8
if ($RunNow) { Start-ScheduledTask -TaskName $taskName }
Get-ScheduledTaskInfo -TaskName $taskName | Select-Object TaskName,NextRunTime,LastRunTime,LastTaskResult
