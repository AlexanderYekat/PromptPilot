"""Run the real updater with fake services; never touch a live checkout."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('timeout', [False, True])
def test_updater_waits_for_gate_receipt_or_retains_old_processes(tmp_path, timeout):
    shell = shutil.which('pwsh') or shutil.which('powershell')
    if not shell:
        pytest.skip('PowerShell is unavailable')
    root = tmp_path / 'trial'
    (root / 'tools').mkdir(parents=True)
    (root / '_local').mkdir()
    shutil.copyfile(Path(__file__).parents[1] / 'tools/update-custom.ps1', root / 'tools/update-custom.ps1')
    sha = 'a' * 40
    (root / 'receipt.json').write_text(json.dumps({'result': 'passed', 'commit': sha}))
    old_release = str(root / 'old')
    (root / '_local/trial-release.json').write_text(json.dumps({'commit': 'b' * 40, 'release': old_release}))
    (root / '_local/trial-processes.json').write_text(json.dumps([
        {'service': 'server', 'pid': 123, 'started': '2026-10-15T00:00:00Z', 'release': old_release},
        {'service': 'worker', 'pid': 124, 'started': '2026-10-15T00:00:00Z', 'release': old_release},
    ]))
    (root / 'tools/stop-custom.ps1').write_text('''
if ($global:gateActive) { throw 'Stopped before the gate receipt was applied' }
$global:stopCount++
Remove-Item -LiteralPath (Join-Path $PSScriptRoot '../_local/trial-processes.json')
''')
    (root / 'tools/start-custom.ps1').write_text('''
param([string]$Ref, [switch]$Worker)
if (-not $Worker) { throw 'Existing worker was not preserved' }
@{commit=$Ref;release='new'} | ConvertTo-Json | Set-Content (Join-Path $PSScriptRoot '../_local/trial-release.json')
@() | ConvertTo-Json | Set-Content (Join-Path $PSScriptRoot '../_local/trial-processes.json')
''')
    harness = root / 'harness.ps1'
    harness.write_text('''
param([string]$Root, [int]$DrainSeconds)
$ErrorActionPreference='Stop'
$global:gateActive=$true
$global:stopCount=0
$global:polls=0
$global:finished=$false
$global:drain=$null
function global:git {
    $global:LASTEXITCODE=0
    if ($args -contains 'ls-remote') { return ('a' * 40) + "`trefs/heads/custom" }
    return 'a' * 40
}
function global:Start-Sleep {}
function global:Get-Process { return @{StartTime=([datetimeoffset]'2026-10-15T00:00:00Z').UtcDateTime} }
function global:Get-CimInstance {
    return @(123,124) | ForEach-Object { @{ProcessId=$_;CommandLine=(Join-Path $Root 'old/main.py')} }
}
function global:Invoke-RestMethod {
    param([string]$Uri, [string]$Method, [string]$ContentType, [string]$Body, [int]$TimeoutSec)
    if ($Uri.EndsWith('/worker/status')) { return @{state='online';paused=$false} }
    if ($Uri.EndsWith('/maintenance/begin')) {
        $global:drain=@{token=($Body | ConvertFrom-Json).token}
    } elseif ($Uri.EndsWith('/maintenance/finish')) {
        $global:finished=$true
        $global:drain=$null
    } elseif ($global:drain) {
        $global:polls++
        if ($DrainSeconds -ge 0 -and $global:polls -ge 3) { $global:gateActive=$false }
    }
    return @{drain=$global:drain;running_tasks=@();active_gates=@('gate');safe_to_restart=(!$global:gateActive -and !!$global:drain)}
}
$failed=$false
try { & (Join-Path $Root 'tools/update-custom.ps1') -Ref ('a' * 40) -Receipt (Join-Path $Root 'receipt.json') -DrainSeconds $DrainSeconds }
catch { $failed=$true }
@{failed=$failed;stopped=$global:stopCount;gate_active=$global:gateActive;finished=$global:finished;polls=$global:polls} | ConvertTo-Json -Compress
''')
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(harness),
                             '-Root', str(root), '-DrainSeconds', '-1' if timeout else '30'],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = json.loads(result.stdout.strip().splitlines()[-1])
    assert actual['finished'], result.stdout + result.stderr
    if timeout:
        assert actual['failed']
        assert actual['stopped'] == 0
        assert actual['gate_active']
    else:
        assert not actual['failed'], result.stdout + result.stderr
        assert actual['polls'] >= 3
        assert actual['stopped'] == 1
        assert not actual['gate_active']
