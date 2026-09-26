# 08 · Codex 实施契约

> 2026-09-26 起，冲突约定以 [10-实施修订与任务](10-实施修订与任务.md) 为准。原“不要改动”不阻止修复已验证缺陷；QC 非法分数默认合格已修正，禁止恢复该行为。

> 本文档是给 AI 编码助手（Codex / Claude Code）的施工图。
> **脚手架已就位、纯逻辑已通过测试**，剩下的是按契约填充实现。
> 所有待实现点均以 `TODO(impl)` 标注，可直接全局搜索定位。

---

## 一、已验证可用的部分（不要重写）

跑 `python -m pytest tests/test_anchor.py tests/test_qc_agent.py -q`，**39 项测试全绿**。
以下模块逻辑已完整实现并通过测试，Codex 只需**调用**，不要改动其行为：

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `app/models/enums.py` | ✅ 完整 | 所有状态机合法取值 |
| `app/models/domain.py` | ✅ 完整 | 项目/角色/场景/镜头/运行/关卡 ORM |
| `app/models/tracking.py` | ✅ 完整 | 抽卡/质检/黄金集/调用日志/预算/导出 ORM |
| `app/core/config.py` | ✅ 完整 | 配置与校验（含"必须异步驱动"的启动期拦截） |
| `app/core/db.py` | ✅ 完整 | 异步会话、请求级事务与回滚 |
| `app/core/redis_client.py` | ✅ 完整 | 并发闸门、幂等键、进度发布/消费 |
| `app/core/errors.py` | ✅ 完整 | 错误码与业务异常体系 |
| `app/core/response.py` | ✅ 完整 | 统一响应与全局异常处理器 |
| `app/knowledge/anchor.py` | ✅ 完整+测试 | 三级锚定第 1 级：程序化拼装 |
| `app/agents/qc_agent.py` | ✅ 完整+测试 | 质检判定维度、Prompt、本地决策树 |
| `app/services/cost_service.py` | ✅ 完整 | 原子扣费、记账、三类报表 |
| `app/providers/base.py` | ✅ 完整 | Provider 基类（幂等/闸门/退避/记账）+ 路由降级 |
| `app/api/v1/health.py` | ✅ 完整 | 健康检查（DB/Redis/FFmpeg/凭据/预算） |
| `app/api/v1/stream.py` | ✅ 完整 | SSE 进度流（心跳、快照、断线补偿） |
| `app/api/v1/budget.py` | 🟡 部分 | `GET /budget/config` 已实现，其余待填 |

**关键设计已固化在测试里，改动会立刻被测试抓到**：
- 锚定 Prompt 逐字节确定性（同输入必同输出）
- 质检"本地阈值覆盖模型自报"（模型说 pass 但分数超阈值 → 判不合格）
- 合规一票否决优先级最高
- 置信度低于 0.6 转人工，不猜 pass
- 漏放成本(140分) > 误杀成本(25分)，约 5.6 倍

---

## 二、待实现清单（按优先级）

### P0 · 不做就跑不起来

| 文件 | 待实现 | 依赖 |
| --- | --- | --- |
| `app/providers/image_jimeng.py` | 即梦图像生成 Provider | 继承 `BaseProvider`，填 `is_configured/_call/estimate_cost` |
| `app/providers/vision_dashscope.py` | 视觉质检 Provider（**必须暴露 `judge()` 方法**，签名见 `QCAgent.inspect` 调用处） | httpx.AsyncClient |
| `app/providers/llm_dashscope.py` | LLM Provider（剧本解析、分镜生成） | 结构化 JSON 输出 |
| `app/providers/__init__.py` | `build_router()` 注册各 Provider | 已有函数体，填 providers 字典 |
| `app/api/v1/*.py` | 各路由实现（契约与 TODO 注释已写好） | — |
| 首个 Alembic 迁移 | `alembic revision --autogenerate -m "init schema"` | 需先装齐依赖并连上 MySQL |

### P1 · 核心卖点

| 文件 | 待实现 |
| --- | --- |
| `app/workers/arq_settings.py` | `enqueue_render`（抽卡→质检→按判定重抽的完整环） |
| `app/pipeline/graph.py` | 九个 node + `build_graph()` 的 LangGraph 装配 |
| `app/providers/image_vidu.py` | Vidu 参考生图（**多角色同框必须用它**，否则角色特征互相污染） |
| `app/agents/storyboard_agent.py`（新建） | 分镜生成 Agent，输出结构化 Shot[] |

### P2 · 完整度

| 文件 | 待实现 |
| --- | --- |
| `app/providers/video_kling.py` / `video_jimeng.py` | 视频生成 + 降级链 |
| `app/services/ffmpeg_service.py`（新建） | 合成服务。**必须用 `asyncio.create_subprocess_exec`，禁止 `subprocess.run`** |
| `backend/scripts/label_golden_set.py` | 黄金测试集标注脚本 |
| `backend/scripts/run_stats.py` | 统计脚本（出简历数据） |

---

## 三、必须遵守的硬约束（违反会出线上事故）

### 1. 异步纪律
- **禁止**在 `async def` 里调用同步阻塞库：同步 MySQL 驱动（PyMySQL）、`requests`、`subprocess.run`、`time.sleep`
- 数据库用 `asyncmy`，HTTP 用 `httpx.AsyncClient`，子进程用 `asyncio.create_subprocess_exec`，等待用 `asyncio.sleep`
- `pyproject.toml` 已启用 Ruff 的 `ASYNC` 规则集，违规会被 lint 抓到
- 违反的后果：事件循环被卡死，并发直接归零，比 Flask 同步还慢

### 2. 花钱的三件事
- **所有模型调用必须走 Provider**，不许业务代码直接发 HTTP——否则记账、闸门、幂等全部失效
- **幂等键不可绕过**：`build_idempotency_key()` 由基类统一生成，Worker 崩溃重启靠它防重复扣费
- **预算扣费用 `CostService.charge()`**，它内部是数据库原子条件更新；自己写"先查后写"会在并发下超支

### 3. 失败处理
- 供应商失败 → 降级链下一个；全部失败 → 抛 `AllProvidersFailedError`（挂起转人工），**不静默返回空结果**
- 预算耗尽 → `status=suspended` 并**保留已合格帧**，不是抛异常让任务失败重试
- 合规拦截（`blocked`）→ **禁止自动重试**，必须人工介入
- 缺片段的镜头 → 显式报错列出，不静默跳过

### 4. Trace 可归因
- `render_attempts.request_payload` 必须存实际发送的完整参数
- `render_attempts.anchor_version` 必须记录判定当时用的锚定版本——不记版本，优化效果无法归因
- 失败的 API 调用也要写 `api_call_logs`（`success=0`）

### 5. 安全
- FFmpeg 参数**不要走 `shell=True`**，片段路径来自数据库，防命令注入
- 健康检查与日志**绝不回显密钥内容**，只报"是否已配置"
- `.env` 已在 `.gitignore` 中，`.env.example` 保留

---

## 四、验收标准

每完成一个模块，按下表自查：

| 检查项 | 命令 / 方法 |
| --- | --- |
| 测试通过 | `pytest -q` |
| Lint 通过 | `ruff check app tests` |
| 类型检查 | `mypy app` |
| 应用能启动 | `uvicorn app.main:app --reload --port 8100`，看启动日志有无 DB/Redis/凭据告警 |
| 健康检查 | `curl http://127.0.0.1:8100/api/v1/health` |
| 迁移可生成 | `alembic revision --autogenerate -m "xxx"` 后人工核对生成内容 |
| 迁移与代码一致 | `alembic check`（CI 门禁） |
| SSE 不被缓冲 | `curl -N http://<服务器>/api/v1/stream/<run_id>`，事件应逐条实时到达而非一次性吐出 |

---

## 五、给 Codex 的提示词建议

分模块交付，一次只做一个，避免上下文过长导致质量下降。建议顺序：

```
1. 先装依赖：pip install -r requirements.txt
2. 生成首个迁移：alembic revision --autogenerate -m "init schema"，然后人工核对
3. 实现 vision_dashscope.py（含 judge 方法）+ 为 QCAgent 补集成测试（用假 Provider）
4. 实现 image_jimeng.py + build_router 注册 + 为 Provider 基类补测试（幂等/降级/记账）
5. 实现 projects.py / characters.py 路由 + 对应 service
6. 实现 enqueue_render 完整环 + 单元测试（用假 Provider，验证重抽决策与预算熔断）
7. 实现 pipeline/graph.py 九个 node + 断点续跑测试
8. 补 label_golden_set.py 与 run_stats.py
```

**每步都要求 Codex 同时写测试**。这个项目的卖点是工程质量与可度量性，
没有测试的"已实现"在这个项目里没有意义。

**明确告诉 Codex 不要做的事**：
- 不要重写第一节表格里标 ✅ 的模块
- 不要为了让代码跑通而放宽质检阈值或跳过人机关卡
- 不要引入同步阻塞调用"临时凑一下"
- 不要在没装 FFmpeg 的情况下用 pip 的 ffmpeg 包替代系统可执行文件

---

## 六、当前依赖状态（本机实测 2026-09-24）

> 以下保留脚手架交付时的历史记录。开发依赖现已补齐，服务连接与验收基线见 [开发准备记录](开发准备.md)。

```
OK   sqlalchemy 2.0.52      MISS pydantic_settings
OK   pydantic 2.13.4        MISS fastapi
OK   httpx 0.28.1           MISS redis
OK   tenacity               MISS arq
OK   langgraph              MISS asyncmy
OK   alembic 1.19.1
```

第一步先补齐：`pip install -r requirements.txt`

> 注：纯逻辑测试（anchor + qc_agent）在缺依赖时也能跑通——
> 这是 `app/models/__init__.py` 采用惰性导出（PEP 562）的目的：
> 让领域逻辑不被数据库与 Web 框架依赖绑架。这个设计不要退回急切导入。

Python 本机为 3.13.15，满足 `requires-python >= 3.11`。
