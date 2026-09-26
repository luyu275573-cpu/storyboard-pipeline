"""LangGraph 流水线状态图：九阶段编排 + 三道强制人机关卡 + 断点续跑。

设计要点（详见 docs/01-架构设计.md 第 3 节）：
1. 状态持久化到 pipeline_runs.graph_state + Redis，进程重启从最后完成节点继续，
   不重复扣费——这是"断点续跑"的实现点，也是与"脚本串一遍"的本质区别
2. 抽卡 ↔ 质检 之间是环（带 max_retry 上限），不是单向流水线
3. 三道人机关卡（A 角色 / B 分镜 / C 合规）是图中断点，
   worker 不轮询等待，而是 status=waiting_gate 后退出，等 API 侧放行再由新任务继续
4. 预算熔断在每次生成前检查，耗尽时 status=suspended 并保留已有成果，不抛异常
"""

from __future__ import annotations

from typing import Any, TypedDict

# ==================== 阶段定义 ====================

# 九阶段。顺序即数据流，也是断点续跑的恢复依据
STAGES: tuple[str, ...] = (
    "script_parse",  # 1 剧本解析：LLM → Scene[] + Character[]
    "character_build",  # 2 角色建档（→ 关卡 A）
    "storyboard",  # 3 分镜生成：LLM → Shot[]（→ 关卡 B）
    "keyframe_render",  # 4 关键帧抽卡
    "quality_check",  # 5 质检判定 ★核心
    "human_review",  # 6 人工终审（→ 关卡 C，先审后播）
    "video_render",  # 7 视频生成
    "synthesize",  # 8 FFmpeg 合成导出
    "metrics",  # 9 数据沉淀：成本报表 + 成功率统计 + 反哺黄金测试集
)

# 人机关卡 → 所在阶段。这三处不可跳过
GATES: dict[str, str] = {
    "character": "character_build",
    "storyboard": "storyboard",
    "compliance": "human_review",
}

# 合规拦截时禁止自动重试，直接挂起
NON_RETRYABLE_VERDICTS: frozenset[str] = frozenset({"blocked"})


class PipelineState(TypedDict, total=False):
    """LangGraph 状态。必须是可 JSON 序列化的——要持久化到 MySQL 与 Redis。

    ⚠️ 不要往状态里塞 ORM 对象或数据库会话，序列化会失败，
       断点续跑也就无从谈起。只存 ID 与纯数据。
    """

    run_id: str
    project_id: str
    stage: str
    # 已完成的阶段，恢复时跳过
    completed_stages: list[str]
    # 当前处理的镜头队列（按场景分组，同场景集中生成）
    pending_shot_ids: list[str]
    done_shot_ids: list[str]
    suspended_shot_ids: list[str]
    # 预算与重试计数
    spent_cents: int
    retry_counts: dict[str, int]
    # 关卡状态：gate_type -> approved/rejected/pending
    gates: dict[str, str]
    # 错误信息
    error_code: str | None
    error_message: str | None


# ==================== 节点实现（由 Codex 填充）====================


async def node_script_parse(state: PipelineState) -> dict[str, Any]:
    """阶段 1：剧本解析。

    TODO(impl):
      1. 读 project.synopsis / 上传的剧本文本
      2. 调 LLM Provider 抽取 Scene[] 与 Character[]，要求结构化 JSON 输出
      3. 解析失败重试（最多 settings.qc_parse_max_retry 次），仍失败则 error_code=QC_PARSE_FAILED
      4. 落 scenes 表；characters 建草稿（confirmed=False）
      5. 返回 {"stage": "character_build", "completed_stages": [...]}

    注意：LLM 抽取的角色特征可能含主观词（"清秀""帅气"），
    入库前应过 build_anchor_prompt 的主观词检测并告警，提示人工修正。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


async def node_character_build(state: PipelineState) -> dict[str, Any]:
    """阶段 2：角色建档 + 关卡 A。

    TODO(impl):
      1. 为每个角色拼装 anchor_prompt（build_anchor_prompt），anchor_version=1
      2. 检查是否已有 confirmed=True 的角色
      3. 未确认 → 写 review_gates(gate_type='character', status='pending')
                → 返回 {"stage": "character_build", "gates": {"character": "pending"}}
                → 图在此中断，等 API 侧 confirm_character 放行
      4. 已确认 → 进入 storyboard

    关卡不可跳过：未确认的角色进生产线，后面全崩（基准图有畸形同理）。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


async def node_storyboard(state: PipelineState) -> dict[str, Any]:
    """阶段 3：分镜生成 + 关卡 B。

    TODO(impl):
      1. 调分镜 Agent（LLM）产出 Shot[]：景别、构图、角色、动作、台词、时长、运镜
      2. 要求结构化 JSON，校验 shot_size 在合法枚举内、duration_ms 在范围内
      3. 按场景分组写入 shots，并设置 prev_locked_attempt_id 递延链的起点
      4. 写 review_gates(gate_type='storyboard', status='pending')，中断等人工审核
      5. 人工可编辑分镜后放行 → 编辑要递增 shot.version（乐观锁）

    分镜描述不可实现（如"百人混战全景"）会导致后续无限重抽烧钱，
    这道关卡是成本控制的第一个闸门。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


async def node_keyframe_render(state: PipelineState) -> dict[str, Any]:
    """阶段 4：关键帧抽卡。

    TODO(impl):
      1. 从 pending_shot_ids 取镜头，按场景分组处理（同场景集中生成，色彩基准才稳定）
      2. 每镜头抽 settings.render_n_per_shot 张
      3. 组装 anchor_prompt（当前 anchor_level + strengthen_fields）+ negative_prompt
      4. 调 ProviderRouter.generate(IMAGE, payload, ctx)，ctx 带 attempt_no 与 seed
      5. 产物落 render_attempts，务必记 request_payload 与 anchor_version（Trace 归因用）
      6. 每张前检查预算；耗尽则整体 suspended 并保留已合格帧
      7. 返回待质检的 attempt_ids

    幂等由基类的 idempotency_key 保证，重试不会重复扣费。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


async def node_quality_check(state: PipelineState) -> dict[str, Any]:
    """阶段 5：质检判定 ★核心。

    TODO(impl):
      1. 逐张调 QCAgent.inspect，image_paths=[基准帧?, 待判定帧]
         基准帧优先级：scene.baseline_attempt_id → shot.prev_locked_attempt_id
      2. 写 qc_reports（含 drifted_fields）
      3. 按 verdict 分支：
           pass       → 若场景无基准帧则设为基准帧；shot.status=review
           repairable → 按 suggestion 决定动作：
                          reseed              同 Prompt 换 seed 重抽
                          redraw              局部重绘
                          strengthen_anchor   next_anchor_level() 升级后重抽，
                                              只强化 drifted_fields 里的字段
           reject     → retry_count += 1
           blocked    → shot.status=suspended，禁止自动重试（合规一票否决）
      4. retry_count 超 max_retry → suspended，转人工
         （按 15% 一次过率，6 次内至少一张合格的概率约 62%；
           超 6 次说明问题不在运气，继续抽是纯烧钱）
      5. 未决镜头回 pending_shot_ids → 形成 抽卡↔质检 的环

    本地决策树不完全信任模型的 verdict：模型说 pass 但本地阈值判定不合格，以本地为准。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


async def node_human_review(state: PipelineState) -> dict[str, Any]:
    """阶段 6：人工终审 + 关卡 C（先审后播）。

    TODO(impl):
      1. 汇总所有 verdict=pass 的帧供人工挑选
      2. 写 review_gates(gate_type='compliance', status='pending')，中断等放行
      3. 放行 → 写 shot.locked_attempt_id，status=video
      4. 被 QC 判 blocked 的镜头禁止在此放行，必须重新生成
      5. 留 snapshot（当时的帧与判定快照），防事后篡改争议

    对应 2025.9《管理提示（动画微短剧管理）》：AIGC 动画纳入分类分层审核、先审后播。
    质检 Agent 只给建议，放行权在人。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


async def node_video_render(state: PipelineState) -> dict[str, Any]:
    """阶段 7：视频生成（图生视频）。

    TODO(impl):
      1. 仅处理已通过关卡 C 的镜头（locked_attempt_id 非空）
      2. 走 video_provider_chain（可灵优先，单价低）
      3. 分层超时：单次请求 < 任务总超时 < 前端等待；配取消令牌
      4. 分钟级任务，必须异步等待 + SSE 推进度
      5. 预算不足时降级：降分辨率 → 换便宜供应商 → 挂起，不直接失败
      6. 僵尸任务防护：超时后必须确认云端任务已取消，否则持续扣费

    预算策略：视频层只做链路验证 + 1 个样例片，不做大规模抽卡。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


async def node_synthesize(state: PipelineState) -> dict[str, Any]:
    """阶段 8：FFmpeg 合成导出。

    TODO(impl):
      1. 收集视频片段，按 (scene.seq, shot.seq) 排序
      2. 生成 concat demuxer 清单（临时文件，路径加引号转义）
      3. asyncio.create_subprocess_exec 调 FFmpeg —— 禁止 subprocess.run，会阻塞事件循环
      4. 超时 + 取消令牌；退出码非 0 读 stderr 记日志
      5. 产物写 storage/exports，登记 exports 行
      6. 缺片段的镜头显式报错列出，不静默跳过

    安全：片段路径来自数据库，拼参数不要走 shell=True，避免命令注入。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


async def node_metrics(state: PipelineState) -> dict[str, Any]:
    """阶段 9：数据沉淀。

    TODO(impl):
      1. 汇总成本报表（CostService.report_*）
      2. 算一次过合格率 first_pass_rate
      3. 算质检拦截节省额 report_qc_savings
      4. 把本次运行的抽卡与判定数据加入黄金测试集候选池
      5. 写入 exports.metrics 快照

    这一步产出的就是简历上的量化指标，不能省。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


# ==================== 图构建（由 Codex 填充）====================


def build_graph() -> Any:
    """构建 LangGraph 状态图。

    TODO(impl):
      from langgraph.graph import StateGraph, END

      g = StateGraph(PipelineState)
      g.add_node("script_parse", node_script_parse)
      ... 九个节点全部注册

      # 主链
      g.set_entry_point("script_parse")
      script_parse → character_build → storyboard → keyframe_render
      keyframe_render → quality_check
      # 环：质检未决则回抽卡（条件边，判断 pending_shot_ids 是否非空）
      quality_check → keyframe_render (若仍有 pending)
      quality_check → human_review (若全部 pass 或 suspended)
      human_review → video_render → synthesize → metrics → END

      # 关卡中断：用 interrupt_before 或在节点内返回 waiting 状态
      # 状态持久化：checkpointer 用 Redis 或 Postgres/MySQL saver，
      #            并把 state 同步写入 pipeline_runs.graph_state

      return g.compile(checkpointer=...)

    ⚠️ 关卡处理不要在 worker 里轮询等待——那会占住 worker 几分钟到几小时。
       正确做法：status=waiting_gate 后任务正常结束，
       API 侧放行时再投递一个新任务从该阶段恢复。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")
