# 分镜流水线 · 发布前检查清单

> **每次部署/更新前逐项确认**。这不是形式主义的文档，每一项都对应一个真实的线上事故风险。

---

## 1. 环境变量与密钥

- [ ] `.env` 文件已从 `.env.example` 复制并填入**真实值**
- [ ] `DATABASE_URL` 使用 `mysql+asyncmy://` 异步驱动（同步驱动会卡死事件循环）
- [ ] `REDIS_URL` 指向正确的 Redis 实例和 DB 编号
- [ ] **预算配置 `BUDGET_TOTAL_CENTS` 是真实值而非测试值**
      - ⚠️ 默认值 20000 = ¥200，测试时可能改成了很小的数
      - 上线后如果还是测试值，几分钟就会触发熔断停止所有生成
- [ ] 各模型供应商 API Key 已配置且**未过期**：
      - [ ] `VOLC_ACCESS_KEY` / `VOLC_SECRET_KEY`（火山引擎·即梦）
      - [ ] `DASHSCOPE_API_KEY`（通义千问视觉/文本）
      - [ ] `VIDU_API_KEY`（Vidu）
      - [ ] `KLING_ACCESS_KEY` / `KLING_SECRET_KEY`（可灵）
- [ ] **并发闸门 `PROVIDER_MAX_CONCURRENCY` 与供应商实际配额匹配**
      - ⚠️ 免费档通常只有 1-2 并发，设太高会被限流甚至封号
      - 确认每个供应商的 QPS/RPM 限制后再设置
- [ ] `APP_ENV=production`，`APP_DEBUG=false`
- [ ] `APP_CORS_ORIGINS` 只包含生产前端域名，不要留 `*` 或 localhost

## 2. 数据库迁移

- [ ] Alembic 迁移版本与当前代码一致：`alembic current` 显示的 revision 是最新的
- [ ] 在 staging 环境先跑过 `alembic upgrade head` 且无报错
- [ ] 如果有破坏性迁移（删列、改类型），已准备数据备份和回滚脚本
- [ ] 数据库字符集是 `utf8mb4`（支持 emoji 和多语言）

## 3. SSE 进度推送验证

> **这是上一个项目踩过的坑，必须验证。**

- [ ] Nginx 配置中 `/api/v1/stream` location 包含以下指令：
      ```nginx
      proxy_buffering off;
      proxy_cache off;
      proxy_set_header X-Accel-Buffering no;
      gzip off;
      proxy_read_timeout 3600s;
      ```
- [ ] **用 curl 实测 SSE 是否真的没被缓冲**：
      ```bash
      # 替换为实际的 stream 接口路径
      curl -N -H "Accept: text/event-stream" \
           https://your-domain/api/v1/stream/test-progress
      # -N 禁用 curl 自身缓冲
      # 预期：事件逐条实时输出，间隔均匀
      # 如果事件攒在一起批量出现 → Nginx 缓冲未关闭
      ```
- [ ] 浏览器 DevTools Network 面板确认 EventStream 标签页有实时事件

## 4. FFmpeg 可用性

- [ ] `ffmpeg -version` 正常输出版本号
- [ ] `ffprobe -version` 正常输出版本号
- [ ] `.env` 中 `FFMPEG_BIN` 和 `FFPROBE_BIN` 指向正确路径（默认 `ffmpeg`/`ffprobe` 即可）
- [ ] Docker 部署时镜像内也验证过：`docker compose exec api ffmpeg -version`

## 5. 存储与磁盘

- [ ] `storage/` 下四个子目录存在且有写权限：`projects/ assets/ renders/ exports/`
- [ ] **磁盘剩余空间充足**：视频素材非常占空间
      ```bash
      df -h /opt/storyboard-pipeline/storage
      # 建议至少预留 50GB，单个视频成片可达数百 MB
      ```
- [ ] storage 目录属主与 systemd 服务的 User 一致（如 www-data）
- [ ] Docker 部署时 volume 挂载正确，容器内可写入

## 6. 日志与监控

- [ ] `logs/` 目录存在且有写权限
- [ ] 日志轮转已配置（防止日志撑爆磁盘）：
      ```bash
      # /etc/logrotate.d/storyboard-pipeline
      /opt/storyboard-pipeline/logs/*.log {
          daily
          rotate 30
          compress
          delaycompress
          missingok
          notifempty
          copytruncate
      }
      ```
- [ ] systemd journal 大小限制合理：`journalctl --disk-usage`
- [ ] 关键告警渠道已配置（预算预警、任务失败、API Key 过期等）

## 7. 服务状态验证

- [ ] 两个 systemd 服务都在运行：
      ```bash
      systemctl status storyboard-api storyboard-worker
      ```
- [ ] API 健康检查通过：`curl http://localhost:8100/docs`
- [ ] Worker 日志无异常：`journalctl -u storyboard-worker --since "5 min ago"`
- [ ] Redis 队列连通：`redis-cli ping`
- [ ] 数据库连通：从 API 日志确认 "数据库连接正常"

## 8. 备份策略

- [ ] MySQL 定时备份已配置（mysqldump 或 xtrabackup）
- [ ] Redis RDB/AOF 持久化已启用（队列中的未完成任务不能丢）
- [ ] storage 目录定期备份到对象存储或异地
- [ ] 备份恢复流程已验证过（至少做过一次恢复演练）

## 9. 回滚预案

- [ ] 上一个稳定版本的 Git tag/commit 已标记
- [ ] 数据库迁移可逆：`alembic downgrade -1` 能成功执行
- [ ] 回滚步骤已记录：
      1. `git checkout <stable-tag>`
      2. `alembic downgrade <prev-revision>`
      3. `systemctl restart storyboard-api storyboard-worker`
      4. 验证健康检查和核心功能
- [ ] Docker 部署时旧镜像 tag 仍可拉取

## 10. 安全检查

- [ ] `.env` 文件权限为 600，仅服务用户可读
- [ ] Nginx 不暴露 `.env`、`.git`、`__pycache__` 等敏感路径
- [ ] API 文档 (`/docs`, `/redoc`) 在生产环境有访问控制
- [ ] 数据库端口不对外暴露（绑定 127.0.0.1）
- [ ] Redis 端口不对外暴露（绑定 127.0.0.1）

---

> 📌 **面试话术提示**：这份清单体现了工程化思维——不只是"能跑就行"，而是系统性地覆盖了配置安全、长连接陷阱、资源容量、可观测性和灾难恢复。每一项都可以展开讲背后的故事。
