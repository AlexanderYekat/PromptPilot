param(
    [Parameter(Mandatory=$true)][string]$Ref,
    [Parameter(Mandatory=$true)][string]$Receipt,
    [int]$DrainSeconds = 1800
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$api = 'http://127.0.0.1:8421/api'
$manifestPath = Join-Path $taskRoot '_local\trial-processes.json'
$pointerPath = Join-Path $taskRoot '_local\trial-release.json'
$servicesBefore = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$previous = Get-Content -LiteralPath $pointerPath -Raw | ConvertFrom-Json
$sha = & git -C $taskRoot rev-parse --verify "$Ref^{commit}"
if ($LASTEXITCODE) { throw 'Cannot resolve candidate.' }
$check = Get-Content -LiteralPath $Receipt -Raw | ConvertFrom-Json
if ($check.result -ne 'passed' -or $check.commit -ne $sha) { throw 'Exact local receipt is missing.' }
$remote = & git -C $taskRoot ls-remote origin refs/heads/custom
if ($LASTEXITCODE -or -not $remote.StartsWith($sha)) { throw 'Candidate must be promoted with tools/promote_custom.py first.' }
$workerBefore = Invoke-RestMethod "$api/worker/status" -TimeoutSec 10
$hadWorker = @($servicesBefore | Where-Object { $_.service -eq 'worker' }).Count -eq 1
$token = [guid]::NewGuid().ToString('N')
$body = @{token=$token} | ConvertTo-Json -Compress
$evidence = Join-Path $taskRoot ('_local\evidence\update-' + [datetime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
New-Item -ItemType Directory -Path $evidence | Out-Null
@{services=$servicesBefore;release=$previous;worker=$workerBefore;token=$token} |
    ConvertTo-Json -Depth 12 | Set-Content -LiteralPath "$evidence\before.json" -Encoding utf8

function Get-DrainStatus {
    $result = Invoke-RestMethod "$api/maintenance" -TimeoutSec 10
    if ($result.drain.token -ne $token) { throw 'Deployment lost its drain ownership.' }
    return $result
}
function Assert-LegacyIdle {
    # First upgrade from a version without the admission barrier: never stop
    # an old worker. It must already be offline, with no gate or handoff active.
    if ($hadWorker -or (Invoke-RestMethod "$api/worker/status").state -ne 'offline') {
        throw 'Old release has no maintenance barrier. Retain it until its worker is stopped safely.'
    }
    if ((Invoke-RestMethod "$api/stats").running) { throw 'Old release still has running tasks.' }
    $offset = 0
    do {
        $items = Invoke-RestMethod "$api/workflows?limit=100&offset=$offset" -TimeoutSec 10
        $items = @($items)
        if (@($items | Where-Object { $_.status -in @('planning','queued','executing','gating','reviewing','revision_required') }).Count) {
            throw 'Old release still has a workflow handoff or gate; it cannot be restarted safely.'
        }
        $offset += $items.Count
    } while ($items.Count -eq 100)
}
$draining = $false
$stopped = $false
$restarted = $false
$legacy = $false
try {
    try {
        $null = Invoke-RestMethod "$api/maintenance" -TimeoutSec 10
    } catch {
        if ([int]$_.Exception.Response.StatusCode -ne 404) { throw }
        Assert-LegacyIdle
        $legacy = $true
    }
    if (-not $legacy) {
        $null = Invoke-RestMethod -Method Post "$api/maintenance/begin" -ContentType 'application/json' -Body $body -TimeoutSec 10
        $draining = $true
        $deadline = [datetime]::UtcNow.AddSeconds($DrainSeconds)
        do {
            $state = Get-DrainStatus
            $state | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath "$evidence\drain.json" -Encoding utf8
            if ($state.safe_to_restart) { break }
            if ([datetime]::UtcNow -ge $deadline) {
                throw 'Drain timed out. Existing tasks and gates are retained; no services were stopped.'
            }
            Start-Sleep -Seconds 2
        } while ($true)
    }
    # Validate process identity and take the last drain snapshot immediately
    # before stopping. The durable barrier blocks new task and gate claims.
    $snapshot = @(Get-CimInstance Win32_Process)
    foreach ($entry in $servicesBefore) {
        $process = Get-Process -Id $entry.pid -ErrorAction Stop
        $info = $snapshot | Where-Object { $_.ProcessId -eq $entry.pid }
        if ([Math]::Abs(($process.StartTime.ToUniversalTime() - ([datetimeoffset]$entry.started).UtcDateTime).TotalSeconds) -ge 1 -or
            -not ([string]$info.CommandLine).Contains((Join-Path $entry.release 'main.py'))) {
            throw 'Service identity changed; old processes are retained.'
        }
    }
    if ($legacy) { Assert-LegacyIdle }
    elseif (-not (Get-DrainStatus).safe_to_restart) { throw 'Drain is no longer idle.' }
    foreach ($name in @('server.log','server.err','worker.log','worker.err','flows.log','flows.err')) {
        $log = Join-Path $taskRoot "_local\logs\$name"
        if (Test-Path -LiteralPath $log) { Copy-Item -LiteralPath $log -Destination (Join-Path $evidence "before-$name") }
    }
    $stopped = $true
    & "$PSScriptRoot\stop-custom.ps1"
    if (Test-Path -LiteralPath $manifestPath) { throw 'Stop did not finish.' }
    & "$PSScriptRoot\start-custom.ps1" -Ref $sha -Worker:$hadWorker
    $release = Get-Content -LiteralPath $pointerPath -Raw | ConvertFrom-Json
    if ($release.commit -ne $sha) { throw 'Unexpected release pointer.' }
    $null = Invoke-RestMethod "$api/maintenance" -TimeoutSec 10
    if ($hadWorker) {
        for ($attempt=0; $attempt -lt 20; $attempt++) {
            $workerReady = Invoke-RestMethod "$api/worker/status" -TimeoutSec 10
            if ($workerReady.state -eq 'online' -and $workerReady.pid -ne $workerBefore.pid) { break }
            Start-Sleep -Seconds 1
        }
        if ($workerReady.state -ne 'online' -or $workerReady.pid -eq $workerBefore.pid) {
            throw 'New worker heartbeat is missing; retain admission barrier and roll back.'
        }
    }
    # From here, a finish request can succeed even if its HTTP response is lost.
    # Never roll back by killing a worker that may have resumed admission.
    $restarted = $true
    if ($draining) {
        $null = Invoke-RestMethod -Method Post "$api/maintenance/finish" -ContentType 'application/json' -Body $body -TimeoutSec 10
        $draining = $false
    }
    @{commit=$sha;worker_restored=$hadWorker;at=[datetime]::UtcNow.ToString('o')} |
        ConvertTo-Json | Set-Content -LiteralPath "$evidence\after.json" -Encoding utf8
    Write-Host "Updated to $sha; evidence $evidence"
} catch {
    $failure = $_
    if ($stopped -and -not $restarted) {
        try {
            if (Test-Path -LiteralPath $manifestPath) { & "$PSScriptRoot\stop-custom.ps1" }
            & "$PSScriptRoot\start-custom.ps1" -Ref $previous.commit -Worker:$hadWorker
        } catch { Write-Warning "Rollback needs attention; drain remains set: $_" }
    }
    throw $failure
} finally {
    if ($draining) {
        # Restoring admission after a timeout lets in-flight work finish. Never
        # clear someone else's lock or silently discard unresolved activity.
        try {
            $null = Invoke-RestMethod -Method Post "$api/maintenance/finish" -ContentType 'application/json' -Body $body -TimeoutSec 10
        } catch { Write-Warning "Drain retained; inspect $evidence before resuming: $_" }
    }
}
