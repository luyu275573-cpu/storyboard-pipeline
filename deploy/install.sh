#!/usr/bin/env bash
# ============================================================
# 分镜流水线 · 一键部署安装脚本
# ============================================================
# 设计原则：
#   - 幂等：可重复执行，已完成的步骤会跳过
#   - 安全：不包含 rm -rf 等危险命令
#   - 透明：每步打印进度，失败立即退出并报错
#
# 用法：sudo -u www-data bash install.sh
#   或：bash install.sh（需要当前用户对 /opt/storyboard-pipeline 有写权限）
# ============================================================

set -euo pipefail

# ---------- 配置变量 ----------
PROJECT_ROOT="/opt/storyboard-pipeline"
BACKEND_DIR="${PROJECT_ROOT}/backend"
VENV_DIR="${BACKEND_DIR}/.venv"
PYTHON_MIN_VERSION="3.11"
DEPLOY_USER="${SUDO_USER:-$(whoami)}"

# ---------- 辅助函数 ----------
info()  { echo -e "\033[1;34m[INFO]\033[0m  $*"; }
ok()    { echo -e "\033[1;32m[OK]\033[0m    $*"; }
warn()  { echo -e "\033[1;33m[WARN]\033[0m  $*"; }
fail()  { echo -e "\033[1;31m[FAIL]\033[0m  $*" >&2; exit 1; }

check_command() {
    if ! command -v "$1" &>/dev/null; then
        fail "缺少必要命令: $1。请先安装: $2"
    fi
    ok "$1 已就绪"
}

# ============================================================
# Step 1: 检查前置依赖
# ============================================================
info "===== Step 1/8: 检查系统依赖 ====="

# Python 版本检查
check_command python3 "apt install python3 python3-venv python3-pip"
PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if [[ "$(printf '%s\n' "${PYTHON_MIN_VERSION}" "${PY_VERSION}" | sort -V | head -n1)" != "${PYTHON_MIN_VERSION}" ]]; then
    fail "Python 版本 ${PY_VERSION} < ${PYTHON_MIN_VERSION}，请升级 Python"
fi
ok "Python ${PY_VERSION} >= ${PYTHON_MIN_VERSION}"

# FFmpeg 检查（视频合成核心依赖，必须是系统级可执行文件）
check_command ffmpeg "apt install ffmpeg"
FFMPEG_VER=$(ffmpeg -version 2>/dev/null | head -1 || true)
info "FFmpeg: ${FFMPEG_VER}"

# MySQL 客户端检查（用于建库和迁移验证）
check_command mysql "apt install mysql-client"

# Redis CLI 检查（用于验证连接）
check_command redis-cli "apt install redis-tools"

# ============================================================
# Step 2: 确认项目目录结构
# ============================================================
info "===== Step 2/8: 确认项目目录 ====="

if [[ ! -d "${PROJECT_ROOT}" ]]; then
    fail "项目目录 ${PROJECT_ROOT} 不存在。请先 git clone 或复制项目到此路径"
fi
ok "项目根目录存在: ${PROJECT_ROOT}"

if [[ ! -f "${BACKEND_DIR}/requirements.txt" ]]; then
    fail "未找到 ${BACKEND_DIR}/requirements.txt，请确认 backend/ 目录完整"
fi
ok "requirements.txt 已找到"

# ============================================================
# Step 3: 创建 Python 虚拟环境
# ============================================================
info "===== Step 3/8: 创建 Python 虚拟环境 ====="

if [[ -d "${VENV_DIR}" ]] && [[ -f "${VENV_DIR}/bin/python" ]]; then
    ok "虚拟环境已存在，跳过创建"
else
    info "创建虚拟环境: ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
    ok "虚拟环境创建完成"
fi

# 激活虚拟环境（后续命令都在 venv 内执行）
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
info "Python: $(python --version)"
info "pip: $(pip --version)"

# ============================================================
# Step 4: 安装 Python 依赖
# ============================================================
info "===== Step 4/8: 安装 Python 依赖 ====="

# 先升级 pip 本身，避免旧版 pip 解析依赖失败
pip install --upgrade pip setuptools wheel -q
pip install -r "${BACKEND_DIR}/requirements.txt" -q
ok "Python 依赖安装完成"

# ============================================================
# Step 5: 环境变量配置
# ============================================================
info "===== Step 5/8: 环境变量配置 ====="

ENV_FILE="${BACKEND_DIR}/.env"
ENV_EXAMPLE="${BACKEND_DIR}/.env.example"

if [[ -f "${ENV_FILE}" ]]; then
    ok ".env 文件已存在，跳过（如需更新请手动编辑）"
else
    if [[ -f "${ENV_EXAMPLE}" ]]; then
        cp "${ENV_EXAMPLE}" "${ENV_FILE}"
        warn ".env 已从 .env.example 复制，⚠️  请立即编辑填入真实值："
        warn "   nano ${ENV_FILE}"
        warn "   重点检查: DATABASE_URL, REDIS_URL, 各模型 API Key, BUDGET_TOTAL_CENTS"
    else
        warn "未找到 .env.example，请手动创建 ${ENV_FILE}"
        warn "参考 config.py 中的字段列表"
    fi
fi

# ============================================================
# Step 6: 数据库初始化
# ============================================================
info "===== Step 6/8: 数据库初始化 ====="

# 尝试连接数据库，如果 .env 中配置了有效凭据则建库
if [[ -f "${ENV_FILE}" ]]; then
    # 从 .env 提取 DATABASE_URL 用于建库检查
    DB_URL=$(grep -E '^DATABASE_URL=' "${ENV_FILE}" | cut -d= -f2- || true)
    if [[ -n "${DB_URL}" ]]; then
        # 提取数据库名（格式: mysql+asyncmy://user:pass@host:port/dbname）
        DB_NAME=$(echo "${DB_URL}" | sed -E 's|.*/([^?]+).*|\1|')
        DB_HOST=$(echo "${DB_URL}" | sed -E 's|.*@([^:/]+).*|\1|')
        DB_USER=$(echo "${DB_URL}" | sed -E 's|.*://([^:]+):.*|\1|')
        DB_PASS=$(echo "${DB_URL}" | sed -E 's|.*://[^:]+:([^@]+)@.*|\1|')

        info "尝试创建数据库 '${DB_NAME}'（如已存在则跳过）..."
        # CREATE DATABASE IF NOT EXISTS 是幂等的
        mysql -h "${DB_HOST}" -u "${DB_USER}" -p"${DB_PASS}" \
            -e "CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" \
            2>/dev/null && ok "数据库 '${DB_NAME}' 就绪" \
            || warn "数据库创建失败（可能凭据未配置或 MySQL 未启动），请手动处理"
    else
        warn "DATABASE_URL 未在 .env 中配置，跳过建库"
    fi
else
    warn ".env 不存在，跳过数据库初始化"
fi

# Alembic 迁移（如果 migrations/ 目录存在）
ALEMBIC_INI="${BACKEND_DIR}/alembic.ini"
MIGRATIONS_DIR="${BACKEND_DIR}/migrations"
if [[ -f "${ALEMBIC_INI}" ]] && [[ -d "${MIGRATIONS_DIR}" ]]; then
    info "执行 Alembic 数据库迁移..."
    cd "${BACKEND_DIR}"
    alembic upgrade head && ok "数据库迁移完成" || warn "迁移失败，请检查 alembic 配置和数据库连接"
    cd "${PROJECT_ROOT}"
else
    warn "未找到 alembic.ini 或 migrations/ 目录，跳过迁移"
    warn "首次部署请在 backend/ 下运行: alembic init migrations && alembic revision --autogenerate"
fi

# ============================================================
# Step 7: 创建运行时目录
# ============================================================
info "===== Step 7/8: 创建运行时目录 ====="

# storage 子目录：projects/assets/renders/exports
for subdir in projects assets renders exports; do
    mkdir -p "${PROJECT_ROOT}/storage/${subdir}"
done
ok "storage/{projects,assets,renders,exports} 已创建"

# 日志目录
mkdir -p "${PROJECT_ROOT}/logs"
ok "logs/ 已创建"

# 设置目录归属（确保 systemd 服务的 User 有写权限）
if id "${DEPLOY_USER}" &>/dev/null; then
    chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${PROJECT_ROOT}/storage" "${PROJECT_ROOT}/logs" 2>/dev/null \
        || warn "无法修改目录归属（可能需要 sudo），请手动执行: chown -R ${DEPLOY_USER} storage logs"
fi

# ============================================================
# Step 8: 安装 systemd 服务
# ============================================================
info "===== Step 8/8: 安装 systemd 服务 ====="

SYSTEMD_DIR="/etc/systemd/system"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for svc in storyboard-api.service storyboard-worker.service; do
    SRC="${SCRIPT_DIR}/${svc}"
    DST="${SYSTEMD_DIR}/${svc}"
    if [[ -f "${SRC}" ]]; then
        cp "${SRC}" "${DST}"
        ok "${svc} → ${DST}"
    else
        warn "未找到 ${SRC}，跳过"
    fi
done

systemctl daemon-reload
ok "systemd 配置已重载"

info "启用服务（开机自启 + 立即启动）..."
systemctl enable storyboard-api storyboard-worker 2>/dev/null \
    && ok "服务已设为开机自启" \
    || warn "enable 失败（可能需要 sudo）"

# ============================================================
# 完成提示
# ============================================================
echo ""
echo "============================================================"
echo -e "\033[1;32m✅ 安装完成！\033[0m"
echo "============================================================"
echo ""
echo "后续步骤："
echo "  1. 编辑环境变量:  nano ${BACKEND_DIR}/.env"
echo "  2. 启动服务:      sudo systemctl start storyboard-api storyboard-worker"
echo "  3. 查看状态:      systemctl status storyboard-api storyboard-worker"
echo "  4. 查看日志:      journalctl -u storyboard-api -f"
echo "                    journalctl -u storyboard-worker -f"
echo "  5. 配置 Nginx:    cp deploy/nginx.conf /etc/nginx/sites-available/storyboard-pipeline"
echo "                    ln -s /etc/nginx/sites-available/storyboard-pipeline /etc/nginx/sites-enabled/"
echo "                    nginx -t && systemctl reload nginx"
echo ""
echo "⚠️  发布前务必阅读 deploy/RELEASE_CHECKLIST.md"
echo ""
