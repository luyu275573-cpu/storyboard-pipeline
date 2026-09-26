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
$Skipped = @()
foreach ($Record in $Records) {
    $ProcessInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$($Record.processId)" -ErrorAction SilentlyContinue
    if (-not $ProcessInfo) { continue }
    # PowerShell 7 may deserialize ISO timestamps as DateTime. Compare ticks, not formatted strings.
    if ($Record.createdTicks) { $CreatedTicks = [long]$Record.createdTicks }
    elseif ($Record.created -is [datetime]) { $CreatedTicks = $Record.created.ToUniversalTime().Ticks }
    else { $CreatedTicks = [DateTimeOffset]::Parse([string]$Record.created).UtcDateTime.Ticks }
    if ($ProcessInfo.CommandLine -ne $Record.command -or $ProcessInfo.CreationDate.ToUniversalTime().Ticks -ne $CreatedTicks) {
        Write-Warning "Process $($Record.processId) identity changed; skipped."
        $Skipped += $Record
        continue
    }
    if ($ProcessInfo.CommandLine.IndexOf($PSScriptRoot, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        Write-Warning 'Process is outside this workspace; skipped.'
        $Skipped += $Record
        continue
    }
    Stop-OwnedTree -TaskProcessId $Record.processId
}
if ($Skipped.Count -gt 0) {
    ConvertTo-Json -InputObject $Skipped | Set-Content -LiteralPath $RecordFile -Encoding UTF8
    Write-Warning 'Some processes were skipped; their records have been retained for inspection.'
} else {
    Remove-Item -LiteralPath $RecordFile
    Write-Host 'Recorded API/frontend processes stopped. Docker services and data were retained.'
}
