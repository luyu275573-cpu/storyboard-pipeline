"""分镜镜头路由：抽卡触发、质检联动、人机关卡、视频与合成。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.scenes import flush_sequence
from app.core.config import settings
from app.core.db import get_db
from app.core.errors import AppError, ConflictError, GatePendingError, NotFoundError
from app.core.response import ok
from app.models.domain import Character, PipelineRun, ReviewGate, Scene, Shot
from app.models.tracking import Export, QCReport, RenderAttempt
from app.schemas import (
    AttemptOut,
    MediaDecision,
    RenderRequest,
    ShotCreate,
    ShotOut,
    ShotUpdate,
)
from app.services.catalog import project_or_404
from app.services.preparation import ensure_editable, invalidate_board

router = APIRouter()


async def save_shot(db: AsyncSession, body: ShotCreate | ShotUpdate, shot_id: str | None = None) -> Shot:
    project_id = await db.scalar(select(Scene.project_id).where(Scene.id == body.scene_id))
    if not project_id:
        raise NotFoundError("场景不存在")
    project = await project_or_404(db, project_id, lock=True)
    await ensure_editable(db, project)
    scene = await db.scalar(select(Scene).where(Scene.id == body.scene_id).with_for_update())
    if scene is None:
        raise NotFoundError("场景不存在")
    characters = list(
        await db.scalars(
            select(Character.id)
            .where(Character.project_id == project.id, Character.id.in_(body.character_ids))
            .with_for_update()
        )
    )
    if set(characters) != set(body.character_ids):
        raise AppError("镜头只能使用本项目的角色")
    if shot_id:
        shot = await db.scalar(select(Shot).where(Shot.id == shot_id).with_for_update())
        if shot is None or shot.scene_id != body.scene_id:
            raise NotFoundError("此场景中不存在该镜头；跨场景移动需重新建立镜头")
        if not isinstance(body, ShotUpdate) or shot.version != body.expected_version:
            raise ConflictError("镜头已更新，请刷新后重试")
        shot.version += 1
    else:
        count = await db.scalar(
            select(func.count()).select_from(Shot).join(Scene).where(Scene.project_id == project.id)
        )
        if count and count >= 200:
            raise AppError("每个项目最多 200 个镜头")
        shot = Shot(max_retry=settings.shot_max_retry)
        db.add(shot)
    for key, value in body.model_dump(exclude={"expected_version"}).items():
        setattr(shot, key, value)
    await invalidate_board(db, project)
    await flush_sequence(db)
    return shot


@router.post("", summary="创建分镜镜头", status_code=201)
async def create_shot(body: ShotCreate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return ok(ShotOut.model_validate(await save_shot(db, body)).model_dump(mode="json"))


@router.put("/{shot_id}", summary="编辑镜头并使旧 B 审核失效")
async def update_shot(shot_id: str, body: ShotUpdate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return ok(ShotOut.model_validate(await save_shot(db, body, shot_id)).model_dump(mode="json"))


@router.delete("/{shot_id}", summary="删除未生成的镜头")
async def delete_shot(
    shot_id: str, expected_version: int = Query(ge=1), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    project_id = await db.scalar(select(Scene.project_id).join(Shot).where(Shot.id == shot_id))
    if not project_id:
        raise NotFoundError("镜头不存在")
    project = await project_or_404(db, project_id, lock=True)
    await ensure_editable(db, project)
    shot = await db.scalar(select(Shot).where(Shot.id == shot_id).with_for_update())
    if shot is None:
        raise NotFoundError("镜头不存在")
    if shot.version != expected_version:
        raise ConflictError("镜头已更新，请刷新后重试")
    if await db.scalar(
        select(RenderAttempt.id).where(RenderAttempt.shot_id == shot_id).with_for_update().limit(1)
    ):
        raise ConflictError("已产生生成记录的镜头不能删除，请保留审计记录")
    await db.delete(shot)
    await invalidate_board(db, project)
    await db.flush()
    return ok({"deleted": shot_id})


@router.get("", summary="镜头列表")
async def list_shots(
    project_id: str, scene_id: str | None = None, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    await project_or_404(db, project_id)
    query = select(Shot).join(Scene).where(Scene.project_id == project_id).order_by(Scene.seq, Shot.seq)
    if scene_id:
        query = query.where(Shot.scene_id == scene_id)
    return ok([ShotOut.model_validate(s).model_dump(mode="json") for s in await db.scalars(query)])


@router.get("/{shot_id}", summary="镜头详情")
async def get_shot(shot_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    shot = await db.get(Shot, shot_id)
    if shot is None:
        raise NotFoundError("镜头不存在")
    return ok(ShotOut.model_validate(shot).model_dump(mode="json"))


@router.post("/{shot_id}/render", summary="触发抽卡")
async def render_shot(
    shot_id: str, body: RenderRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """核心接口：触发图像或视频抽卡。

    TODO(impl):
      1. 校验镜头所属角色已 confirmed（人机关卡 A），否则抛 GatePendingError
      2. 校验预算：ProviderRouter 调用前通过 CostService.begin_call 预留预算
      3. 校验 retry_count < max_retry，超限抛 MaxRetryError（挂起转人工，不静默失败）
      4. 镜头级互斥锁 lock:shot:{shot_id}，防并发重复抽卡
      5. 组装 anchor_prompt（按 body.anchor_level / strengthen_fields）
      6. 投递 ARQ 任务 enqueue_render(shot_id, n, stage, anchor_level, strengthen_fields)
      7. 返回 {shot_id, queued: n, attempt_nos: [...]}

    ⚠️ 不要在此同步调用 Provider。图像生成秒级、视频分钟级，
       同步等待会占满 worker 并触发 HTTP 超时。必须走队列。
    """
    if body.stage != "image":
        raise AppError("首个 Demo 只支持图像关键帧生成")
    project_id = await db.scalar(select(Scene.project_id).join(Shot).where(Shot.id == shot_id))
    if not project_id:
        raise NotFoundError("镜头不存在")
    shot = await db.scalar(select(Shot).where(Shot.id == shot_id).with_for_update())
    if shot is None:
        raise NotFoundError("镜头不存在")
    project = await project_or_404(db, project_id, lock=True)
    run = await db.scalar(
        select(PipelineRun)
        .where(PipelineRun.project_id == project.id)
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )
    if (
        run is None
        or run.status != "waiting_model"
        or run.graph_state.get("storyboard_version") != project.storyboard_version
    ):
        raise GatePendingError("请先通过当前版本的 B 分镜审核")
    from app.core.queue import enqueue_render_job
    job_id = await enqueue_render_job(shot_id, body.n)
    return ok({"shot_id": shot_id, "queued": body.n, "job_id": job_id})


@router.get("/{shot_id}/attempts", summary="抽卡历史")
async def list_attempts(shot_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    if await db.get(Shot, shot_id) is None:
        raise NotFoundError("镜头不存在")
    rows = await db.scalars(
        select(RenderAttempt).where(RenderAttempt.shot_id == shot_id).order_by(RenderAttempt.attempt_no)
    )
    return ok([AttemptOut.model_validate(row).model_dump(mode="json") for row in rows])


@router.get("/attempts/{attempt_id}/file", summary="读取生成关键帧")
async def get_attempt_file(attempt_id: str, db: AsyncSession = Depends(get_db)) -> FileResponse:
    attempt = await db.get(RenderAttempt, attempt_id)
    if attempt is None or attempt.status != "succeeded" or not attempt.asset_path:
        raise NotFoundError("生成关键帧不存在")
    root = settings.storage_path.resolve()
    target = (root / attempt.asset_path).resolve()
    if not target.is_relative_to(root / "generated") or not target.is_file():
        raise NotFoundError("生成关键帧不存在")
    return FileResponse(target, media_type="video/mp4" if attempt.stage == "video" else "image/png",
                        headers={"X-Content-Type-Options": "nosniff"})


@router.post("/{shot_id}/gate/compliance", summary="人机关卡 C：先审后播合规终审")
async def compliance_gate(
    shot_id: str, body: MediaDecision, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """强制人工关卡。质检 Agent 只给建议，放行权在人。

    TODO(impl):
      1. 校验该镜头有 verdict=pass 的质检报告，否则抛 GatePendingError
      2. approved → 写 locked_attempt_id，status=review→video，version+=1（乐观锁）
         rejected → status=suspended，记录 note
      3. 写 review_gates(gate_type='compliance', snapshot=当前帧与判定快照)
      4. 被 QC 判 blocked（合规拦截）的镜头禁止在此放行，必须重新生成
         —— 对应 2025.9《管理提示（动画微短剧管理）》的"先审后播"要求
    """
    project_id = await db.scalar(select(Scene.project_id).join(Shot).where(Shot.id == shot_id))
    if not project_id:
        raise NotFoundError("镜头不存在")
    project = await project_or_404(db, project_id, lock=True)
    shot = await db.scalar(select(Shot).join(Scene).where(Shot.id == shot_id).with_for_update())
    if shot is None:
        raise NotFoundError("镜头不存在")
    await ensure_editable(db, project)
    if shot.version != body.expected_version:
        raise ConflictError("镜头版本已变化，请刷新后审核")
    from app.services.video import current_context
    _, _, run = await current_context(db, shot_id)
    if body.run_id != run.id:
        raise ConflictError("运行记录与镜头不匹配")
    report = await db.scalar(
        select(QCReport)
        .join(RenderAttempt)
        .where(RenderAttempt.shot_id == shot.id, RenderAttempt.stage == "image",
               RenderAttempt.id == body.attempt_id)
        .order_by(QCReport.created_at.desc(), QCReport.id.desc())
        .with_for_update()
        .limit(1)
    )
    if report is None or report.verdict != "pass":
        raise GatePendingError("只有视觉质检通过的关键帧才能进入 C 审核")
    attempt = await db.get(RenderAttempt, report.attempt_id, with_for_update=True)
    if attempt is None or attempt.status != "succeeded" or not attempt.asset_path:
        raise AppError("质检通过记录缺少关键帧产物")
    if body.status == "approved":
        shot.locked_attempt_id = attempt.id
        shot.status = "video"
    else:
        shot.locked_attempt_id = None
        shot.status = "suspended"
    shot.accepted_video_attempt_id = None
    shot.version += 1
    run_id = await db.scalar(
        select(PipelineRun.id)
        .where(PipelineRun.project_id == project.id)
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )
    if run_id is None:
        raise AppError("项目没有可用运行记录")
    gate = ReviewGate(
        run_id=run_id,
        gate_type="compliance",
        status=body.status,
        reviewer=body.reviewer or "manual",
        note=body.note or "",
        snapshot={
            "shot_id": shot.id,
            "attempt_id": attempt.id,
            "asset_path": attempt.asset_path,
            "qc_report_id": report.id,
            "verdict": report.verdict,
            "shot_version": shot.version,
            "storyboard_version": project.storyboard_version,
        },
        decided_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db.add(gate)
    await db.flush()
    return ok({"shot_id": shot.id, "status": shot.status, "attempt_id": attempt.id, "gate_id": gate.id})


@router.post("/{shot_id}/video", summary="触发视频生成")
async def generate_video(shot_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from app.core.queue import enqueue_job
    from app.services.video import prepare_video

    attempt = await prepare_video(db, shot_id)
    await db.refresh(attempt)
    data = AttemptOut.model_validate(attempt).model_dump(mode="json")
    await db.commit()
    if attempt.status not in {"succeeded", "failed"}:
        await enqueue_job("run_video", attempt_id=attempt.id, _job_id=f"video:{attempt.id}")
    return ok(data)


@router.post("/{shot_id}/gate/video", summary="人工视频终审")
async def video_gate(shot_id: str, body: MediaDecision,
                     db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from app.services.video import decide_video
    gate = await decide_video(db, shot_id, body)
    return ok({"gate_id": gate.id, "status": gate.status})


@router.post("/video-exports", summary="异步合成视频")
async def create_video_export(project_id: str, run_id: str, preview: bool = False,
                              db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    from app.core.queue import enqueue_job
    from app.services.video_export import prepare_export
    export = await prepare_export(db, project_id, run_id, preview)
    await db.commit()
    await enqueue_job("run_video_export", export_id=export.id)
    return ok({"id": export.id, "status": export.status})


@router.get("/exports/{export_id}", summary="读取导出进度")
async def export_status(export_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    export = await db.get(Export, export_id)
    if not export:
        raise NotFoundError("导出记录不存在")
    return ok({"id": export.id, "status": export.status, "duration_ms": export.duration_ms,
               "metrics": export.metrics})


@router.post("/synthesize", summary="FFmpeg 时间轴合成")
async def synthesize(
    project_id: str, run_id: str | None = None, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """TODO(impl):
      1. 取该场景/项目所有 locked_attempt_id 的视频片段，按 (scene.seq, shot.seq) 排序
      2. 生成 FFmpeg concat demuxer 文件清单 → 拼接 → 转场 → 导出到 storage/exports
      3. 写 exports 行，metrics 存本次成本与成功率快照（直接用于简历数据）
      4. 缺片段的镜头要显式报错列出，不能静默跳过

    注意：FFmpeg 是子进程调用，必须用 asyncio.create_subprocess_exec，
    不能用 subprocess.run（会阻塞事件循环）。设置超时与取消令牌。
    """
    from app.services.synthesis import synthesize_storyboard

    return ok(await synthesize_storyboard(db, project_id=project_id, run_id=run_id))


@router.get("/exports/{export_id}/file", summary="下载分镜预演")
async def download_export(export_id: str, db: AsyncSession = Depends(get_db)) -> FileResponse:
    export = await db.get(Export, export_id)
    if export is None or export.status != "succeeded" or not export.output_path:
        raise NotFoundError("导出文件不存在")
    root = settings.storage_path.resolve()
    target = (root / export.output_path).resolve()
    if not target.is_relative_to(root / "exports") or not target.is_file():
        raise NotFoundError("导出文件不存在")
    return FileResponse(target, media_type="video/mp4", filename=f"storyboard-{export_id}.mp4")
