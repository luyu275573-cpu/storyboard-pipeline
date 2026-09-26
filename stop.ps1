# Stop only processes whose recorded identity still belongs to this launch.
$ErrorActionPreference = 'Stop'
$RecordFile = Join-Path $PSScriptRoot 'logs\dev-processes.json'
if (-not (Test-Path -LiteralPath $RecordFile)) { Write-Host 'No recorded development processes.'; exit 0 }
$Records = Get-Content -LiteralPath $RecordFile -Raw | ConvertFrom-Json
function Stop-OwnedTree([int]$TaskProcessId) {
    $Children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$TaskProcessId")
    foreach ($Child in $Children) { Stop-OwnedTree -TaskProcessId $Child.ProcessId }
    Stop-Process -Id $TaskProcessId -Force -ErrorAction SilentlyContinue
}
foreach ($Record in $Records) {
    $ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($Record.processId)" -ErrorAction SilentlyContinue
    if (-not $ProcessInfo) { continue }
    if ($ProcessInfo.CommandLine -ne $Record.command -or $ProcessInfo.CreationDate.ToUniversalTime().ToString('o') -ne $Record.created) {
        Write-Warning "Process $($Record.processId) identity changed; skipped."
        continue
    }
    if ($ProcessInfo.CommandLine.IndexOf($PSScriptRoot, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        Write-Warning 'Process is outside this workspace; skipped.'
        continue
    }
    Stop-OwnedTree -TaskProcessId $Record.processId
}
Remove-Item -LiteralPath $RecordFile
Write-Host 'Recorded API/frontend processes stopped. Docker services and data were retained.'
