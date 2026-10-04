param([string]$Python = '', [string]$Receipt = '')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path $taskRoot '.venv312\Scripts\python.exe' }
if (-not $Receipt) { $Receipt = Join-Path $taskRoot '_local\evidence\local-check.json' }
$saved = @{}
Get-ChildItem Env: | Where-Object { $_.Name -like 'PP_*' -or $_.Name -eq 'PYTHONUTF8' } | ForEach-Object { $saved[$_.Name]=$_.Value }
Push-Location -LiteralPath $taskRoot
try {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Receipt) | Out-Null
    $commit = & git rev-parse HEAD
    if ($LASTEXITCODE) { throw 'Cannot identify commit.' }
    @{ commit=$commit; result='running' } | ConvertTo-Json | Set-Content -LiteralPath $Receipt -Encoding utf8
    Get-ChildItem Env: | Where-Object { $_.Name -like 'PP_*' } | ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }
    $env:PYTHONUTF8='1'
    # A fresh candidate worktree has no _local yet.
    New-Item -ItemType Directory -Force -Path (Join-Path $taskRoot '_local') | Out-Null
    $emptyEnv=Join-Path $taskRoot '_local\validation.env'
    Set-Content -LiteralPath $emptyEnv -Value ''
    $env:PP_ENV_FILE=$emptyEnv
    $env:PP_DATA_DIR=Join-Path $taskRoot '_local\validation'
    Write-Host '== check: ruff'
    & $Python -m ruff check promptpilot tests tools main.py
    if ($LASTEXITCODE) { throw 'Lint failed.' }
    Write-Host '== check: pytest'
    & $Python -m pytest -q -W error
    if ($LASTEXITCODE) { throw 'Tests failed.' }
    Write-Host '== check: javascript'
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Node.js is required.' }
    foreach ($taskTest in Get-ChildItem -LiteralPath tests -Filter '*.cjs') {
        & node $taskTest.FullName
        if ($LASTEXITCODE) { throw "JavaScript test failed: $($taskTest.Name)" }
    }
    Write-Host '== check: git diff'
    & git diff --check
    if ($LASTEXITCODE) { throw 'git diff --check failed.' }
    if (& git status --porcelain --untracked-files=no) { throw 'Commit tracked changes before producing a release receipt.' }
    if ((& git rev-parse HEAD) -ne $commit) { throw 'HEAD changed during validation; no release receipt can be issued.' }
    @{ commit=$commit; result='passed'; python=(& $Python --version); checked_at=[datetime]::UtcNow.ToString('o') } | ConvertTo-Json | Set-Content -LiteralPath $Receipt -Encoding utf8
} finally {
    Get-ChildItem Env: | Where-Object { $_.Name -like 'PP_*' -or $_.Name -eq 'PYTHONUTF8' } | ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }
    foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key,$saved[$key],'Process') }
    Pop-Location
}
