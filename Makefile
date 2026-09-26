# ============================================================
# 分镜流水线 · Makefile（常用命令聚合）
# ============================================================
# 主要给 Linux 服务器使用，命令 POSIX 兼容
# Windows 本地开发请用 start.ps1 / stop.ps1
#
# 用法：make <target>
#   make install   — 安装依赖
#   make dev       — 启动 API（开发模式，热重载）
#   make worker    — 启动 ARQ Worker
#   make test      — 运行测试
#   make lint      — 代码检查
#   make typecheck — 类型检查
#   make migrate   — 执行数据库迁移
#   make revision  — 生成新迁移
#   make stats     — 运行统计脚本
#   make clean     — 清理临时文件
# ============================================================

.PHONY: install dev worker test lint typecheck migrate revision stats clean help

# ---------- 变量 ----------
BACKEND_DIR := backend
VENV        := $(BACKEND_DIR)/.venv
PYTHON      := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
UVICORN     := $(VENV)/bin/uvicorn
ARQ         := $(VENV)/bin/arq
ALEMBIC     := $(VENV)/bin/alembic
RUFF        := $(VENV)/bin/ruff
MYPY        := $(VENV)/bin/mypy
PYTEST      := $(VENV)/bin/pytest

# ---------- 默认目标 ----------
help: ## 显示帮助信息
	@echo "分镜流水线 · 可用命令："
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo ""

# ---------- 安装 ----------
install: ## 创建虚拟环境并安装依赖
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install --upgrade pip setuptools wheel -q
	$(PIP) install -r $(BACKEND_DIR)/requirements.txt -q
	@echo "✅ 依赖安装完成"

# ---------- 开发 ----------
dev: ## 启动 API Server（开发模式，自动重载）
	cd $(BACKEND_DIR) && $(UVICORN) app.main:app --host 127.0.0.1 --port 8100 --reload

worker: ## 启动 ARQ Worker
	cd $(BACKEND_DIR) && $(ARQ) app.workers.arq_settings.WorkerSettings

# ---------- 质量 ----------
test: ## 运行测试（跳过集成测试和慢测试）
	cd $(BACKEND_DIR) && $(PYTEST) -x -q -m "not integration and not slow"

lint: ## Ruff 代码检查 + 自动修复
	cd $(BACKEND_DIR) && $(RUFF) check app/ --fix
	cd $(BACKEND_DIR) && $(RUFF) format app/

typecheck: ## mypy 类型检查
	cd $(BACKEND_DIR) && $(MYPY) app/

# ---------- 数据库 ----------
migrate: ## 执行 Alembic 迁移到最新版本
	cd $(BACKEND_DIR) && $(ALEMBIC) upgrade head

revision: ## 生成新的 Alembic 迁移（用法：make revision MSG="add xxx table"）
	cd $(BACKEND_DIR) && $(ALEMBIC) revision --autogenerate -m "$(MSG)"

# ---------- 运维 ----------
stats: ## 运行统计脚本（如有）
	@if [ -f "$(BACKEND_DIR)/scripts/stats.py" ]; then \
		cd $(BACKEND_DIR) && $(PYTHON) scripts/stats.py; \
	else \
		echo "⚠️  未找到 scripts/stats.py，请先创建统计脚本"; \
	fi

# ---------- 清理 ----------
clean: ## 清理临时文件和缓存（不删除 storage/logs/.env）
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "✅ 临时文件已清理"
