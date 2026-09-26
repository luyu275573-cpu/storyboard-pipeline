# 分镜流水线 · 部署指南

## 两种部署方式对比

| 维度 | systemd 直装 | Docker Compose |
|------|-------------|----------------|
| **适用场景** | 单台腾讯云 CVM，长期稳定运行 | 开发/测试环境、多环境隔离、CI/CD 流水线 |
| **运维复杂度** | 低：直接 systemctl 管理 | 中：需理解容器网络和卷挂载 |
| **资源开销** | 无额外开销 | Docker daemon + 容器运行时 ~200MB |
| **环境一致性** | 依赖宿主机 Python/FFmpeg 版本 | 镜像锁定所有依赖，跨机器一致 |
| **故障排查** | journalctl 直接看日志 | docker compose logs + exec 进容器 |
| **更新方式** | git pull → pip install → restart | docker compose build → up -d |
| **推荐度** | ⭐⭐⭐ 生产首选 | ⭐⭐ 开发/staging |

> **选择建议**：如果只有一台服务器且团队熟悉 Linux 运维，用 systemd 直装。如果需要多环境隔离或 CI/CD 自动部署，用 Docker Compose。

---

## 方式一：systemd 直装（推荐生产）

### 前置条件

- Ubuntu 20.04+ / Debian 11+
- Python >= 3.11
- MySQL 8.0
- Redis 7+
- FFmpeg（系统包）
- Nginx（反向代理）

### 安装步骤

```bash
# 1. 克隆项目到目标目录
git clone <repo-url> /opt/storyboard-pipeline
cd /opt/storyboard-pipeline

# 2. 运行一键安装脚本
bash deploy/install.sh

# 3. 编辑环境变量（⚠️ 必须修改默认值）
nano backend/.env

# 4. 启动服务
sudo systemctl start storyboard-api storyboard-worker

# 5. 配置 Nginx
sudo cp deploy/nginx.conf /etc/nginx/sites-available/storyboard-pipeline
sudo ln -s /etc/nginx/sites-available/storyboard-pipeline /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 6. 验证
curl http://localhost:8100/docs          # API 文档
journalctl -u storyboard-api -f          # API 日志
journalctl -u storyboard-worker -f       # Worker 日志
```

### 日常运维

```bash
# 查看状态
systemctl status storyboard-api storyboard-worker

# 重启服务
sudo systemctl restart storyboard-api storyboard-worker

# 查看最近日志
journalctl -u storyboard-api --since "1 hour ago"
journalctl -u storyboard-worker --since "1 hour ago"

# 更新代码后重新部署
cd /opt/storyboard-pipeline
git pull origin main
source backend/.venv/bin/activate
pip install -r backend/requirements.txt
alembic upgrade head
sudo systemctl restart storyboard-api storyboard-worker
```

---

## 方式二：Docker Compose

### 前置条件

- Docker >= 24.0
- Docker Compose >= 2.20

### 安装步骤

```bash
cd /opt/storyboard-pipeline

# 1. 准备环境变量
cp backend/.env.example backend/.env
nano backend/.env
# ⚠️ Docker 环境下 DATABASE_URL 和 REDIS_URL 的主机名要改为服务名：
#   DATABASE_URL=mysql+asyncmy://sbp:sbp_password@mysql:3306/storyboard_pipeline
#   REDIS_URL=redis://redis:6379/3

# 2. 构建并启动
docker compose -f deploy/docker-compose.yml up -d --build

# 3. 等待健康检查通过
docker compose -f deploy/docker-compose.yml ps

# 4. 验证
curl http://localhost:8100/docs
docker compose -f deploy/docker-compose.yml logs -f api worker
```

### 日常运维

```bash
# 查看日志
docker compose -f deploy/docker-compose.yml logs -f api

# 进入容器排查
docker compose -f deploy/docker-compose.yml exec api bash
docker compose -f deploy/docker-compose.yml exec worker bash

# 更新重建
git pull origin main
docker compose -f deploy/docker-compose.yml up -d --build

# 停止（保留数据）
docker compose -f deploy/docker-compose.yml down

# 完全销毁（⚠️ 删除所有数据卷）
docker compose -f deploy/docker-compose.yml down -v
```

---

## 本地开发（Windows）

```powershell
# 首次安装
.\start.ps1    # 自动创建 venv、装依赖、启动服务

# 停止
.\stop.ps1

# 或使用 Makefile（需要 make）
make install   # 安装依赖
make dev       # 启动 API
make worker    # 启动 Worker
```

---

## 发布前必做

无论哪种部署方式，发布前都必须过一遍 `RELEASE_CHECKLIST.md`。

重点检查：
1. 预算配置是否为真实值（不是测试时的极小值）
2. SSE 进度推送是否真的没被 Nginx 缓冲
3. FFmpeg 是否可用
4. storage 磁盘空间是否充足
