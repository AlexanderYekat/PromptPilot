param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$release = Get-Content -LiteralPath (Join-Path $taskRoot '_local\trial-release.json') -Raw | ConvertFrom-Json
$saved = @{}
Get-ChildItem Env: | Where-Object { $_.Name -like 'PP_*' } | ForEach-Object { $saved[$_.Name]=$_.Value; Remove-Item -LiteralPath "Env:$($_.Name)" }
try {
    $env:PP_ENV_FILE=$release.env_file
    & $release.python (Join-Path $release.release 'main.py') @Arguments
    $result=$LASTEXITCODE
} finally {
    Get-ChildItem Env: | Where-Object { $_.Name -like 'PP_*' } | ForEach-Object { Remove-Item -LiteralPath "Env:$($_.Name)" }
    foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key,$saved[$key],'Process') }
}
exit $result
