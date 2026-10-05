[CmdletBinding(SupportsShouldProcess)]
param([string]$Root = '')
$ErrorActionPreference = 'Stop'
$taskRoot = if ($Root) { (Resolve-Path -LiteralPath $Root).Path } else { Split-Path -Parent $PSScriptRoot }
$pidFile = Join-Path $taskRoot '_local\trial-processes.json'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Host 'No trial process manifest.'; return }
# Windows PowerShell 5.1 emits a JSON array as one object; @() would wrap it once more.
$entries = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
$snapshot = @(Get-CimInstance Win32_Process)
function Stop-TrialTree([int]$processId) {
    foreach ($child in @($snapshot | Where-Object { $_.ParentProcessId -eq $processId })) { Stop-TrialTree ([int]$child.ProcessId) }
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}
foreach ($entry in $entries) {
    $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
    if (-not $process) { continue }
    $info = $snapshot | Where-Object { $_.ProcessId -eq $entry.pid }
    # PowerShell 7 converts ISO JSON dates to DateTime; parsing its localized
    # string a second time can swap day/month. Casting handles both PS5 and PS7.
    $startMatches = [Math]::Abs(($process.StartTime.ToUniversalTime() - ([datetimeoffset]$entry.started).UtcDateTime).TotalSeconds) -lt 1
    $commandMatches = $info -and ([string]$info.CommandLine).Contains((Join-Path $entry.release 'main.py'))
    if (-not ($startMatches -and $commandMatches)) { throw "PID $($entry.pid) no longer matches this trial; left untouched." }
    if ($PSCmdlet.ShouldProcess("$($entry.service) PID $($entry.pid)", 'Stop trial process tree')) { Stop-TrialTree $entry.pid }
}
if ($PSCmdlet.ShouldProcess($pidFile, 'Remove stopped trial process manifest')) { Remove-Item -LiteralPath $pidFile }
Write-Host 'Trial stop completed. Data retained in _local/data.'
