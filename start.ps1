# ============================================================
# 分镜流水线 · Windows 本地开发启动脚本
# ============================================================
# 用法：.\start.ps1
# 功能：检查环境 → 创建 venv → 装依赖 → 启动 uvicorn + ARQ worker
# 注意：PowerShell 5.1 语法，不用 && / 2>&1 / ternary
# ============================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Join-Path $ProjectRoot "backend"
$VenvDir = Join-Path $BackendDir ".venv"
$EnvFile = Join-Path $BackendDir ".env"
$RequirementsFile = Join-Path $BackendDir "requirements.txt"

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  分镜流水线 · 本地开发启动" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# ---------- Step 1: 检查 .env ----------
Write-Host "[1/5] 检查环境变量..." -ForegroundColor Yellow
if (-not (Test-Path $EnvFile)) {
    $EnvExample = Join-Path $BackendDir ".env.example"
    if (Test-Path $EnvExample) {
        Write-Host "  .env 不存在，从 .env.example 复制..." -ForegroundColor Yellow
        Copy-Item $EnvExample $EnvFile
        Write-Host "  ⚠️  请编辑 backend\.env 填入真实值后再运行本脚本" -ForegroundColor Red
        Write-Host "  重点: DATABASE_URL, REDIS_URL, 各模型 API Key" -ForegroundColor Red
        exit 1
    } else {
        Write-Host "  ❌ 未找到 .env 和 .env.example，请手动创建 backend\.env" -ForegroundColor Red
        exit 1
    }
}
Write-Host "  ✅ .env 已就绪" -ForegroundColor Green

# ---------- Step 2: 检查 Python ----------
Write-Host "[2/5] 检查 Python..." -ForegroundColor Yellow
try {
    $pyVersion = python --version 2>$null
    if (-not $pyVersion) {
        Write-Host "  ❌ 未找到 python，请安装 Python >= 3.11" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ $pyVersion" -ForegroundColor Green
} catch {
    Write-Host "  ❌ 未找到 python，请安装 Python >= 3.11" -ForegroundColor Red
    exit 1
}

# ---------- Step 3: 创建/激活虚拟环境 ----------
Write-Host "[3/5] 检查虚拟环境..." -ForegroundColor Yellow
if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
    Write-Host "  创建虚拟环境..." -ForegroundColor Yellow
    python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ 虚拟环境创建失败" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ 虚拟环境已创建" -ForegroundColor Green
} else {
    Write-Host "  ✅ 虚拟环境已存在" -ForegroundColor Green
}

# 激活虚拟环境
$activateScript = Join-Path $VenvDir "Scripts\Activate.ps1"
if (Test-Path $activateScript) {
    & $activateScript
} else {
    Write-Host "  ❌ 激活脚本不存在: $activateScript" -ForegroundColor Red
    exit 1
}

# ---------- Step 4: 安装依赖 ----------
Write-Host "[4/5] 安装 Python 依赖..." -ForegroundColor Yellow
pip install -r $RequirementsFile -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 依赖安装失败" -ForegroundColor Red
    exit 1
}
Write-Host "  ✅ 依赖已就绪" -ForegroundColor Green

# ---------- Step 5: 启动服务 ----------
Write-Host "[5/5] 启动服务..." -ForegroundColor Yellow
Write-Host ""

# 创建 storage 和 logs 目录（如果不存在）
$storageDirs = @("projects", "assets", "renders", "exports")
foreach ($sub in $storageDirs) {
    $dir = Join-Path $ProjectRoot "storage\$sub"
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}
$logsDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
}

Write-Host "  🚀 启动 uvicorn (API Server)..." -ForegroundColor Cyan
# 在后台启动 uvicorn
$apiJob = Start-Job -ScriptBlock {
    param($bd)
    Set-Location $bd
    & "$bd\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload
} -ArgumentList $BackendDir

Write-Host "  🚀 启动 ARQ Worker..." -ForegroundColor Cyan
# 在后台启动 ARQ worker
$workerJob = Start-Job -ScriptBlock {
    param($bd)
    Set-Location $bd
    & "$bd\.venv\Scripts\python.exe" -m arq app.workers.arq_settings.WorkerSettings
} -ArgumentList $BackendDir

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  ✅ 服务已启动！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  API Docs:  http://127.0.0.1:8100/docs" -ForegroundColor White
Write-Host "  API Job ID: $($apiJob.Id)" -ForegroundColor Gray
Write-Host "  Worker Job ID: $($workerJob.Id)" -ForegroundColor Gray
Write-Host ""
Write-Host "  查看日志: Receive-Job -Id <JobId>" -ForegroundColor Gray
Write-Host "  停止服务: .\stop.ps1" -ForegroundColor Gray
Write-Host ""

# 等待一下让用户看到输出
Start-Sleep -Seconds 3

# 显示初始输出
$apiOutput = Receive-Job -Id $apiJob.Id
$workerOutput = Receive-Job -Id $workerJob.Id
if ($apiOutput) { Write-Host "[API] $apiOutput" -ForegroundColor Cyan }
if ($workerOutput) { Write-Host "[Worker] $workerOutput" -ForegroundColor Cyan }
