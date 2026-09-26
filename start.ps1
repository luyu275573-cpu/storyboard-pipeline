# Windows development launcher: only starts this project's local processes.
$ErrorActionPreference = 'Stop'
$ProjectRoot = $PSScriptRoot
$BackendDir = Join-Path $ProjectRoot 'backend'
$FrontendDir = Join-Path $ProjectRoot 'frontend'
$LogDir = Join-Path $ProjectRoot 'logs'
$RecordFile = Join-Path $LogDir 'dev-processes.json'
$PythonExe = Join-Path $BackendDir '.venv\Scripts\python.exe'
$ViteScript = Join-Path $FrontendDir 'node_modules\vite\bin\vite.js'
$NodeExe = (Get-Command node -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath $PythonExe)) { throw 'Install backend requirements in backend/.venv first.' }
if (-not (Test-Path -LiteralPath $ViteScript)) { throw 'Run npm ci in frontend first.' }
foreach ($Port in @(8100, 5173)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
        throw "Port $Port is already in use. Existing services were not changed."
    }
}
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Push-Location $ProjectRoot
try {
    docker compose --env-file backend/.env -f deploy/compose.dev.yml up -d --wait
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop and retry.' }
} finally { Pop-Location }
Push-Location $BackendDir
try {
    & $PythonExe -m alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
} finally { Pop-Location }
$Started = @()
$env:PYTHONUTF8 = '1'
function Save-StartedProcesses {
    $Records = @($Started | ForEach-Object {
        $Entry = Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)"
        if ($Entry) {
            @{ processId = $_.Id; created = $Entry.CreationDate.ToUniversalTime().ToString('o'); command = $Entry.CommandLine }
        }
    })
    ConvertTo-Json -InputObject $Records | Set-Content -LiteralPath $RecordFile -Encoding UTF8
}
try {
    $ApiProcess = Start-Process -FilePath $PythonExe -ArgumentList '-m uvicorn app.main:app --host 127.0.0.1 --port 8100' -WorkingDirectory $BackendDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'api.stdout.log') -RedirectStandardError (Join-Path $LogDir 'api.stderr.log')
    $Started += $ApiProcess
    Save-StartedProcesses
    $WebProcess = Start-Process -FilePath $NodeExe -ArgumentList @(('"' + $ViteScript + '"'), '--host', '127.0.0.1', '--port', '5173', '--strictPort') -WorkingDirectory $FrontendDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'web.stdout.log') -RedirectStandardError (Join-Path $LogDir 'web.stderr.log')
    $Started += $WebProcess
    Save-StartedProcesses
    $Ready = $false
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        try {
            $Health = Invoke-RestMethod 'http://127.0.0.1:8100/api/v1/health' -TimeoutSec 2
            $Web = Invoke-WebRequest 'http://127.0.0.1:5173/' -UseBasicParsing -TimeoutSec 2
            if ($Health.data.ready -and $Web.StatusCode -eq 200) { $Ready = $true; break }
        } catch { }
        if (@($Started | Where-Object { $_.HasExited }).Count -gt 0) { throw 'A service exited; inspect logs/.' }
        Start-Sleep -Seconds 1
    }
    if (-not $Ready) { throw 'Services did not become ready; inspect logs/.' }
    Write-Host 'Workbench: http://127.0.0.1:5173'
    Write-Host 'API docs:  http://127.0.0.1:8100/docs'
    Write-Host 'Stop: .\stop.ps1 (database volumes are retained)'
} catch {
    & (Join-Path $ProjectRoot 'stop.ps1')
    throw
}
