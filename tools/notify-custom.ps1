param([Parameter(Mandatory=$true)][string]$MessageFile, [int]$DisplaySeconds = 60)
$ErrorActionPreference = 'Stop'
$message = Get-Content -LiteralPath $MessageFile -Raw -Encoding utf8 | ConvertFrom-Json
# Works without a packaged app or cloud account. The report remains on disk.
$shell = New-Object -ComObject WScript.Shell
try {
    $result = $shell.Popup([string]$message.body, $DisplaySeconds, [string]$message.title, 64)
    if ($result -notin @(-1,1)) { throw "Unexpected notification result: $result" }
    Write-Output "Desktop notification displayed (result=$result)."
} finally {
    [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) | Out-Null
}
