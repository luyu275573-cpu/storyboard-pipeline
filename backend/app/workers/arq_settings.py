"""ARQ Worker 配置。

启动方式：
    arq app.workers.arq_settings.WorkerSettings

⚠️ systemd 单元（deploy/storyboard-worker.service）与 Dockerfile 都引用此模块路径，
   改名或移动会导致 worker 起不来。

关键设计：
1. 视频生成是分钟级任务，job_timeout 必须给足，否则任务被杀但云端仍在渲染 → 白花钱
2. max_jobs 受供应商并发闸门约束，设大也没用（闸门默认 1），反而占内存
3. 所有任务函数必须能安全重入：靠幂等键防重复扣费，不能靠"不会重试"来保证
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from arq.connections import RedisSettings
from sqlalchemy import func, select

from app.agents.qc_agent import QCAgent
from app.core.config import settings
from app.core.errors import AppError, BudgetExceededError, ProviderUncertainError
from app.models.domain import Character, CharacterRef, PipelineRun, Project, Scene, Shot
from app.models.enums import AttemptStatus, ProviderKind
from app.models.tracking import QCReport, RenderAttempt
from app.providers import build_router
from app.services.cost_service import CostService
from app.services.rendering import (
    RenderSpec,
    apply_provider_result,
    build_image_payload,
    prepare_attempt,
)

logger = logging.getLogger(__name__)


def _parse_redis_url(url: str) -> RedisSettings:
    """把 redis://host:port/db 解析为 ARQ 的 RedisSettings。"""
    from redis.asyncio import Redis

    # ARQ 接受 redis.asyncio.Redis 的连接参数对象
    parsed = Redis.from_url(url)
    conn_kwargs = parsed.connection_pool.connection_kwargs
    return RedisSettings(
        host=conn_kwargs.get("host", "127.0.0.1"),
        port=int(conn_kwargs.get("port", 6379)),
        database=int(conn_kwargs.get("db", 0) or 0),
        password=conn_kwargs.get("password"),
    )


# ==================== 任务实现（由 Codex 填充）====================


async def enqueue_render(ctx: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """抽卡任务：图像或视频生成 + 质检 + 按判定结果决定重抽。

    入参 kwargs:
      shot_id: str
      n: int                     抽几张
      stage: str                 image / video
      anchor_level: str          normal / strong / strongest
      strengthen_fields: list    质检返回的漂移字段，只强化这些

    TODO(impl):
      1. 开 DB 会话（Worker 里没有 FastAPI 依赖注入，要自建 SessionLocal）
      2. 抢镜头级互斥锁 lock:shot:{shot_id}，抢不到直接返回（防并发重复抽卡）
      3. 预算预检 CostService.precheck
      4. 组装 anchor_prompt（build_anchor_prompt）+ negative_prompt
      5. 对 n 张并行/串行调 ProviderRouter.generate(IMAGE|VIDEO, payload, ctx)
         - ctx 必须带 attempt_no 与 seed，幂等键靠它们生成
      6. 每张成功产物落 render_attempts（含 request_payload 与 anchor_version，Trace 用）
      7. 逐张调 QCAgent.inspect，写 qc_reports
      8. 按 verdict 决策：
           pass          → 若是场景首张合格帧，设为 scene.baseline_attempt_id
                           shot.status = review，等人机关卡 C
           repairable    → 按 suggestion 计算下一轮 anchor_level（next_anchor_level）
                           递归投递本任务（retry_count += 1，超 max_retry 则 suspended）
           reject        → retry_count += 1，同上
           blocked       → status = suspended，禁止自动重试（合规拦截）
      9. 全程 publish_progress 推进度，供 SSE 消费
      10. finally 释放锁

    ⚠️ 三个必须注意的点：
      - 重入安全：任务可能被 ARQ 重试，幂等键保证不重复扣费，但不要重复递增 retry_count
      - 部分结果保留：中途失败时已合格的帧必须保留，不能整体回滚
        （与农牧项目"中断后的部分结果保留"、YumeShelf"执行预算"同一思路）
      - 预算耗尽不是错误：应 status=suspended 并保留成果，不是抛异常让任务失败重试
    """
    shot_id = str(kwargs.get("shot_id") or "")
    if not shot_id:
        raise AppError("缺少 shot_id")
    stage = str(kwargs.get("stage") or "image")
    if stage != "image":
        raise AppError("首个 Demo 只支持 image 阶段")
    count = int(kwargs.get("n", 1))
    if not 1 <= count <= 6:
        raise AppError("抽卡数量必须为 1 到 6")
    sessions = ctx.get("session_factory")
    if sessions is None:
        raise AppError("Worker 数据库会话未初始化")
    cost = CostService(sessions)
    router = build_router(cost)
    anchor_level = str(kwargs.get("anchor_level") or "normal")
    strengthen_fields = [str(item) for item in (kwargs.get("strengthen_fields") or [])]
    results: list[dict[str, Any]] = []
    for _offset in range(count):
        async with sessions.begin() as db:
            shot = await db.scalar(
                select(Shot).where(Shot.id == shot_id).with_for_update()
            )
            if shot is None:
                raise AppError("镜头不存在")
            scene = await db.get(Scene, shot.scene_id)
            if scene is None:
                raise AppError("镜头场景不存在")
            project = await db.get(Project, scene.project_id)
            if project is None:
                raise AppError("镜头项目不存在")
            run = await db.scalar(
                select(PipelineRun)
                .where(PipelineRun.project_id == project.id)
                .where(PipelineRun.status.in_(["waiting_model", "running"]))
                .order_by(PipelineRun.created_at.desc())
                .with_for_update()
                .limit(1)
            )
            if run is None or run.graph_state.get("storyboard_version") != project.storyboard_version:
                raise AppError("当前项目尚未通过有效的 B 分镜审核")
            characters = list(
                await db.scalars(
                    select(Character)
                    .where(Character.project_id == project.id)
                    .where(Character.id.in_(shot.character_ids))
                )
            )
            refs = list(
                await db.scalars(
                    select(CharacterRef)
                    .where(CharacterRef.character_id.in_(shot.character_ids))
                    .where(CharacterRef.qc_passed.is_(True), CharacterRef.is_primary.is_(True))
                )
            )
            last_no = await db.scalar(
                select(func.max(RenderAttempt.attempt_no)).where(RenderAttempt.shot_id == shot.id)
            )
            attempt_no = (last_no or 0) + 1
            anchor_version = max([getattr(c, "anchor_version", 1) for c in characters] or [1])
            seed = str(kwargs.get("seed") or f"{shot.id}:{attempt_no}")
            spec = RenderSpec(
                project.id,
                run.id,
                shot.id,
                attempt_no,
                anchor_version,
                seed=seed,
                attempt_id=str(uuid.uuid4()),
            )
            references = [str(settings.storage_path / ref.asset_path) for ref in refs]
            payload = build_image_payload(
                project=project,
                scene=scene,
                shot=shot,
                characters=characters,
                reference_paths=references,
                anchor_level=anchor_level,
                strengthen_fields=strengthen_fields,
                seed=spec.seed,
                model=settings.siliconflow_image_model,
            )
            attempt, call_context = prepare_attempt(spec=spec, payload=payload)
            db.add(attempt)
            await db.flush()
            attempt.started_at = datetime.now(UTC).replace(tzinfo=None)
        try:
            result = await router.generate(ProviderKind.IMAGE, payload, call_context)
        except (BudgetExceededError, ProviderUncertainError):
            raise
        except Exception as exc:
            logger.exception("镜头生成失败 shot_id=%s", shot_id)
            async with sessions.begin() as db:
                row = await db.get(RenderAttempt, attempt.id, with_for_update=True)
                if row is not None and row.status == AttemptStatus.PENDING.value:
                    row.status = AttemptStatus.FAILED.value
                    row.error_code = type(exc).__name__[:60]
                    row.finished_at = datetime.now(UTC).replace(tzinfo=None)
            results.append({"attempt_id": attempt.id, "status": AttemptStatus.FAILED.value})
            continue
        async with sessions.begin() as db:
            row = await db.get(RenderAttempt, attempt.id, with_for_update=True)
            if row is None:
                raise AppError("抽卡记录在调用后丢失")
            apply_provider_result(row, result)
            if result.success:
                row.status = AttemptStatus.SUCCEEDED.value
                current_shot = await db.get(Shot, shot_id, with_for_update=True)
                current_scene = (
                    await db.get(Scene, current_shot.scene_id, with_for_update=True)
                    if current_shot
                    else None
                )
                if current_shot is not None:
                    current_shot.status = "review"
                if current_scene is not None and current_scene.baseline_attempt_id is None:
                    current_scene.baseline_attempt_id = row.id
            else:
                current_shot = await db.get(Shot, shot_id, with_for_update=True)
                if current_shot is not None:
                    current_shot.status = "suspended"
        if result.success:
            async with sessions.begin() as db:
                qc_shot = await db.get(Shot, shot_id, with_for_update=True)
                qc_scene = await db.get(Scene, qc_shot.scene_id, with_for_update=True) if qc_shot else None
                if qc_shot is None or qc_scene is None:
                    raise AppError("质检前镜头快照不存在")
                qc_characters = list(
                    await db.scalars(
                        select(Character)
                        .where(Character.project_id == project.id)
                        .where(Character.id.in_(qc_shot.character_ids))
                    )
                )
                qc_attempt = await db.get(RenderAttempt, attempt.id, with_for_update=True)
                if qc_attempt is None or not qc_attempt.asset_path:
                    raise AppError("质检前缺少关键帧产物")
                baseline_path = None
                if qc_scene.baseline_attempt_id and qc_scene.baseline_attempt_id != qc_attempt.id:
                    baseline = await db.get(RenderAttempt, qc_scene.baseline_attempt_id)
                    baseline_path = (
                        str(settings.storage_path / baseline.asset_path)
                        if baseline and baseline.asset_path
                        else None
                    )
                image_paths = [
                    path
                    for path in [baseline_path, str(settings.storage_path / qc_attempt.asset_path)]
                    if path
                ]
                anchor_prompt = str(getattr(qc_characters[0], "anchor_prompt", "") if qc_characters else "")
                anchor_version = max([getattr(c, "anchor_version", 1) for c in qc_characters] or [1])
                qc_context = replace(
                    call_context,
                    kind=ProviderKind.VISION,
                    operation_key=f"qc:{shot_id}:{attempt.id}"[:120],
                )
                qc_result = await QCAgent(provider_router=router).inspect(
                    image_paths=image_paths,
                    anchor_prompt=anchor_prompt,
                    anchor_version=anchor_version,
                    shot_size=qc_shot.shot_size,
                    composition=qc_shot.composition,
                    action_text=qc_shot.action_text,
                    dialogue=qc_shot.dialogue,
                    has_baseline_frame=baseline_path is not None,
                    call_context=qc_context,
                )
                db.add(
                    QCReport(
                        attempt_id=qc_attempt.id,
                        model=settings.siliconflow_vision_model,
                        verdict=qc_result.verdict.value,
                        dimensions=qc_result.dimensions,
                        severity=qc_result.severity.value if qc_result.severity else None,
                        suggestion=qc_result.suggestion.value if qc_result.suggestion else None,
                        confidence=qc_result.confidence,
                        reasoning=qc_result.reasoning,
                        raw_response=qc_result.raw,
                    )
                )
                if qc_result.is_pass:
                    qc_shot.status = "review"
                    if qc_scene.baseline_attempt_id is None:
                        qc_scene.baseline_attempt_id = qc_attempt.id
                else:
                    qc_shot.status = "suspended"
                results.append(
                    {
                        "attempt_id": attempt.id,
                        "status": qc_shot.status,
                        "asset_path": qc_attempt.asset_path,
                        "qc_verdict": qc_result.verdict.value,
                    }
                )
        else:
            results.append({"attempt_id": attempt.id, "status": row.status, "asset_path": row.asset_path})
    return {"shot_id": shot_id, "stage": stage, "attempts": results}


async def enqueue_pipeline_run(ctx: dict[str, Any], run_id: str) -> dict[str, Any]:
    """整条流水线运行：剧本解析 → 分镜 → 抽卡 → 质检 → 视频 → 合成。

    TODO(impl):
      1. 加载/初始化 LangGraph 状态（见 app/pipeline/graph.py）
      2. 断点续跑：从 pipeline_runs.graph_state 恢复，已完成的阶段不重跑
         —— 这是"进程重启不重复扣费"的实现点
      3. 每完成一个阶段就持久化 graph_state 并 publish_progress
      4. 遇到人机关卡（A 角色 / B 分镜 / C 合规）→ status=waiting_gate 并暂停，
         等 API 侧放行后由新任务继续，不要在 worker 里轮询等待
      5. 全部完成 → status=completed，写 exports 与 metrics
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


async def enqueue_synthesize(
    ctx: dict[str, Any], project_id: str, run_id: str | None = None
) -> dict[str, Any]:
    """FFmpeg 合成任务。

    TODO(impl):
      1. 收集所有 locked_attempt_id 对应的视频片段，按 (scene.seq, shot.seq) 排序
      2. 生成 concat demuxer 清单文件（写到临时目录，注意路径要加引号转义）
      3. asyncio.create_subprocess_exec 调 FFmpeg —— 禁止 subprocess.run，会阻塞事件循环
      4. 设置超时与取消令牌；进程退出码非 0 要读 stderr 记日志
      5. 产物写 storage/exports，登记 exports 行与 metrics 快照
      6. 缺片段的镜头显式报错列出，不静默跳过

    安全：片段路径来自数据库，拼 FFmpeg 参数时不要走 shell=True，
    避免路径里含特殊字符导致命令注入。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


async def enqueue_qc_evaluate(ctx: dict[str, Any], project_id: str | None = None) -> dict[str, Any]:
    """在黄金测试集上跑评估，产出混淆矩阵。

    TODO(impl):
      1. 优先复用已有的 qc_reports + human_verdict 计算，不重复调 API（省钱）
      2. 仅对缺少机器判定的样本调 QCAgent
      3. 算 TP/TN/FP/FN、accuracy、recall、false_positive_rate
      4. 算 cost_fn / cost_fp（漏放 140 分/张，误杀 25 分/张）
      5. 统计 dimension_breakdown（错误分类体系）
      6. 结果写文件到 data/ 供出报告，并返回

    这是简历上"召回率 X%、误杀率 Y%"的数据源。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


# ==================== 生命周期钩子 ====================


async def startup(ctx: dict[str, Any]) -> None:
    """Worker 启动：建 DB 引擎与共享 httpx client。"""
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    ctx["engine"] = create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )
    ctx["session_factory"] = async_sessionmaker(ctx["engine"], expire_on_commit=False)
    # 复用连接池，避免每次调用重建 TCP + TLS
    ctx["http_client"] = httpx.AsyncClient(timeout=settings.provider_timeout_s)
    logger.info("Worker 启动完成 queue=%s", settings.arq_queue_name)


async def shutdown(ctx: dict[str, Any]) -> None:
    """Worker 关闭：释放资源。正在跑的任务由 ARQ 负责优雅等待。"""
    client = ctx.get("http_client")
    if client is not None:
        await client.aclose()
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()
    from app.core.redis_client import close_redis

    await close_redis()
    logger.info("Worker 已关闭")


# ==================== Worker 配置 ====================


class WorkerSettings:
    """ARQ 读取的配置类。类名固定，勿改。"""

    functions = [
        enqueue_render,
        enqueue_pipeline_run,
        enqueue_synthesize,
        enqueue_qc_evaluate,
    ]

    redis_settings = _parse_redis_url(settings.redis_url)
    queue_name = settings.arq_queue_name

    # 视频生成分钟级：任务超时必须给足，否则任务被杀但云端仍在渲染 → 白花钱
    job_timeout = settings.video_timeout_s + 120
    # 超时后不重试视频任务（重试等于重复扣费）；靠幂等键兜底
    retry_jobs = False
    max_tries = 1

    # 供应商并发闸门默认为 1，worker 并发设大也没意义，反而占内存
    max_jobs = 4

    # 结果保留 1 小时，便于排查
    keep_result = 3600
    # 任务领取后 60 秒内必须开始执行，否则视为积压
    poll_delay = 0.5

    on_startup = startup
    on_shutdown = shutdown

    # 允许在没有任务时正常空闲，不退出
    allow_abort_jobs = True
