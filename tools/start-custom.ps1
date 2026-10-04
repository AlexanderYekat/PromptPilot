param([string]$Ref = 'custom', [switch]$Worker)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskLocal = Join-Path $taskRoot '_local'
$taskPython = Join-Path $taskRoot '.venv312\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Install Python 3.12 environment .venv312 first; see CUSTOM_SETUP.md.' }
if (Test-Path -LiteralPath (Join-Path $taskLocal 'trial-processes.json')) { throw 'Run tools/stop-custom.ps1 before starting again.' }
if (Get-NetTCPConnection -State Listen -LocalPort 8421 -ErrorAction SilentlyContinue) { throw 'Port 8421 is already in use.' }
& $taskPython (Join-Path $PSScriptRoot 'custom_runtime.py') prepare --ref $Ref
if ($LASTEXITCODE) { throw 'Release preparation failed.' }
$release = Get-Content -LiteralPath (Join-Path $taskLocal 'trial-release.json') -Raw | ConvertFrom-Json
$saved = @{}
Get-ChildItem Env: | Where-Object { $_.Name -like 'PP_*' -or $_.Name -in @('PYTHONUTF8','PYTHONIOENCODING') } | ForEach-Object { $saved[$_.Name] = $_.Value }
$started = @()
try {
    Get-ChildItem Env: | Where-Object { $_.Name -like 'PP_*' } | ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }
    $env:PP_ENV_FILE = $release.env_file
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    & $taskPython (Join-Path $release.release 'tools\custom_runtime.py') seed
    if ($LASTEXITCODE) { throw 'Demo initialization failed.' }
    $logDir = Join-Path $taskLocal 'logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $services = @('server', 'flows')
    if ($Worker) { $services += 'worker' }
    foreach ($service in $services) {
        $arguments = @(('"' + (Join-Path $release.release 'main.py') + '"'), $service)
        if ($service -eq 'flows') { $arguments += 'run' }
        $process = Start-Process -FilePath $taskPython -ArgumentList $arguments -WorkingDirectory $release.release -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDir "$service.log") -RedirectStandardError (Join-Path $logDir "$service.err")
        $started += [ordered]@{ service=$service; pid=$process.Id; started=$process.StartTime.ToUniversalTime().ToString('o'); release=$release.release }
    }
    $started | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $taskLocal 'trial-processes.json') -Encoding utf8
    $ready = $false
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        try { $null = Invoke-RestMethod 'http://127.0.0.1:8421/api/stats' -TimeoutSec 1; $ready=$true; break } catch { Start-Sleep -Milliseconds 300 }
    }
    if (-not $ready) { throw 'Trial server failed to become ready. See _local/logs.' }
    Write-Host "Trial ready: http://127.0.0.1:8421 | commit $($release.commit)"
    Write-Host 'Worker starts only with -Worker. The initial queue is paused; Resume explicitly enables agent calls.'
} catch {
    foreach ($entry in $started) { Stop-Process -Id $entry.pid -Force -ErrorAction SilentlyContinue }
    $pidFile = Join-Path $taskLocal 'trial-processes.json'
    if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile }
    throw
} finally {
    Get-ChildItem Env: | Where-Object { $_.Name -like 'PP_*' -or $_.Name -in @('PYTHONUTF8','PYTHONIOENCODING') } | ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }
    foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process') }
}
