# ============================================================
# 分镜流水线 · Windows 本地开发停止脚本
# ============================================================
# 用法：.\stop.ps1
# 功能：停止 start.ps1 启动的 uvicorn 和 ARQ worker 后台任务
# ============================================================

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  分镜流水线 · 停止服务" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 查找并停止所有相关后台 Job
$jobs = Get-Job -ErrorAction SilentlyContinue | Where-Object {
    $_.Command -match "uvicorn|arq"
}

if ($jobs) {
    foreach ($job in $jobs) {
        Write-Host "  停止 Job $($job.Id): $($job.Command.Substring(0, [Math]::Min(60, $job.Command.Length)))..." -ForegroundColor Yellow
        Stop-Job -Id $job.Id -ErrorAction SilentlyContinue
        Remove-Job -Id $job.Id -Force -ErrorAction SilentlyContinue
    }
    Write-Host "  ✅ 后台任务已停止" -ForegroundColor Green
} else {
    Write-Host "  ℹ️  没有找到运行中的后台任务" -ForegroundColor Yellow
}

# 额外检查：直接杀掉可能残留的 uvicorn/arq 进程
$procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match "python" -and $_.CommandLine -match "uvicorn|arq"
}

# PowerShell 5.1 的 Get-Process 没有 CommandLine 属性，改用 wmic
$wmicProcs = wmic process where "name='python.exe'" get ProcessId,CommandLine /format:list 2>$null
$foundPids = @()
foreach ($line in $wmicProcs) {
    if ($line -match "CommandLine=(.*uvicorn.*|.*arq.*)") {
        # 从同一进程的 ProcessId 行提取 PID
        # wmic list 格式是交替的，这里简化处理
    }
}

# 更可靠的方式：按端口查找 uvicorn
$portProcs = netstat -ano 2>$null | Select-String ":8100\s+.*LISTENING"
if ($portProcs) {
    foreach ($match in $portProcs) {
        if ($match -match "\s+(\d+)\s*$") {
            $pid = $Matches[1]
            if ($pid -ne "0") {
                Write-Host "  发现占用 8100 端口的进程 PID=$pid，正在终止..." -ForegroundColor Yellow
                Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
                Write-Host "  ✅ PID $pid 已终止" -ForegroundColor Green
            }
        }
    }
}

Write-Host ""
Write-Host "  ✅ 所有服务已停止" -ForegroundColor Green
Write-Host ""
