# ============================================================
# 分镜流水线 · Docker 镜像（多阶段构建）
# ============================================================
# 设计决策：
#   - 多阶段构建：builder 装编译依赖，runtime 只保留运行时文件，减小镜像体积
#   - python:3.11-slim：比 full 小 ~800MB，比 alpine 兼容性好（C 扩展预编译 wheel）
#   - FFmpeg 通过 apt 安装系统包，不用 pip 包（pip 的 ffmpeg-python 只是 wrapper）
#   - 非 root 用户运行：安全最佳实践，即使容器被突破也无法提权
#   - 不打包 .env：密钥通过 docker-compose 或 K8s Secret 注入
# ============================================================

# ---------- Stage 1: Builder ----------
FROM python:3.11-slim AS builder

WORKDIR /build

# 安装构建依赖（部分 Python 包需要 C 编译器）
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 先复制 requirements.txt 利用 Docker 缓存层
# 只有依赖变更时才重新 pip install，代码改动不影响这一层
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- Stage 2: Runtime ----------
FROM python:3.11-slim AS runtime

# 运行时系统依赖：
#   ffmpeg / ffprobe — 视频合成核心工具
#   libmariadb-dev   — asyncmy 底层 C 库
#   curl             — 健康检查探针
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libmariadb-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 复制已安装的 Python 包
COPY --from=builder /install /usr/local

# 创建非 root 用户
# 为什么不用 nobody？因为需要写 storage/logs，nobody 没有固定 home 目录
RUN groupadd -r appuser && useradd -r -g appuser -d /home/appuser -s /sbin/nologin appuser \
    && mkdir -p /home/appuser && chown -R appuser:appuser /home/appuser

WORKDIR /app

# 复制后端代码
COPY backend/ ./backend/

# 创建运行时目录并设置权限
RUN mkdir -p /app/storage/{projects,assets,renders,exports} /app/logs \
    && chown -R appuser:appuser /app/storage /app/logs

# 切换到非 root 用户
USER appuser

# 默认环境变量（可被 docker-compose 覆盖）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    APP_HOST=0.0.0.0 \
    APP_PORT=8100

EXPOSE 8100

# 健康检查：轻量级 HTTP 探针，不依赖数据库
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8100/docs || exit 1

# 默认启动 uvicorn；worker 镜像通过 docker-compose command 覆盖
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "4"]
