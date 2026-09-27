"""Persistent video attempts: enqueue after commit; retries query the same request."""

import uuid
from datetime import UTC, datetime
from typing import Any

from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, ConflictError, GatePendingError, ProviderUncertainError
from app.models.domain import PipelineRun, ReviewGate, Scene, Shot
from app.models.enums import ProviderKind
from app.models.tracking import ProviderRequest, RenderAttempt
from app.providers import CallContext, build_router
from app.schemas import MediaDecision
from app.services.catalog import project_or_404
from app.services.cost_service import CostService
from app.services.rendering import apply_provider_result


async def current_context(db: AsyncSession, shot_id: str):
    project_id = await db.scalar(select(Scene.project_id).join(Shot).where(Shot.id == shot_id))
    if not project_id:
        raise AppError("镜头不存在")
    project = await project_or_404(db, project_id, lock=True)
    shot = await db.get(Shot, shot_id, with_for_update=True)
    run = await db.scalar(
        select(PipelineRun)
        .where(PipelineRun.project_id == project_id)
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )
    if (
        not shot
        or not run
        or run.status != "waiting_model"
        or run.graph_state.get("storyboard_version") != project.storyboard_version
    ):
        raise GatePendingError("请先完成当前版本的 B 分镜审核")
    gates = await db.scalars(
        select(ReviewGate).where(
            ReviewGate.run_id == run.id, ReviewGate.gate_type == "storyboard", ReviewGate.status == "approved"
        )
    )
    if not any(g.snapshot.get("storyboard_version") == project.storyboard_version for g in gates):
        raise GatePendingError("缺少当前版本的 B 审核记录")
    return project, shot, run


def matches(attempt: RenderAttempt, shot: Shot, run: PipelineRun) -> bool:
    snapshot = attempt.request_payload
    return (
        attempt.stage == "video"
        and attempt.shot_id == shot.id
        and snapshot.get("run_id") == run.id
        and snapshot.get("shot_version") == shot.version
        and snapshot.get("image_attempt_id") == shot.locked_attempt_id
        and snapshot.get("storyboard_version") == run.graph_state.get("storyboard_version")
    )


def context_for(attempt: RenderAttempt) -> CallContext:
    p = attempt.request_payload
    return CallContext(
        ProviderKind.VIDEO,
        p["project_id"],
        p["run_id"],
        f"video:{attempt.id}",
        shot_id=attempt.shot_id,
        attempt_id=attempt.id,
        attempt_no=attempt.attempt_no,
        seed=attempt.seed,
    )


async def prepare_video(db: AsyncSession, shot_id: str) -> RenderAttempt:
    project, shot, run = await current_context(db, shot_id)
    image = await db.get(RenderAttempt, shot.locked_attempt_id) if shot.locked_attempt_id else None
    if (
        not image
        or image.shot_id != shot.id
        or image.stage != "image"
        or image.status != "succeeded"
        or not image.asset_path
        or shot.status != "video"
    ):
        raise GatePendingError("视频生成需要已通过 C 审核的关键帧")
    gates = await db.scalars(
        select(ReviewGate).where(
            ReviewGate.run_id == run.id, ReviewGate.gate_type == "compliance", ReviewGate.status == "approved"
        )
    )
    if not any(
        g.snapshot.get("shot_id") == shot.id and g.snapshot.get("attempt_id") == image.id for g in gates
    ):
        raise GatePendingError("锁定关键帧缺少 C 审核记录")
    existing = list(
        await db.scalars(
            select(RenderAttempt)
            .where(
                RenderAttempt.shot_id == shot.id,
                RenderAttempt.stage == "video",
            )
            .order_by(RenderAttempt.attempt_no.desc())
            .with_for_update()
        )
    )
    for row in existing:
        if matches(row, shot, run):
            return row
        if row.status in {"pending", "running", "unknown"}:
            raise ConflictError("旧视频任务尚未确认结果，请先查询原任务")
    path = (settings.storage_path / image.asset_path).resolve()
    if not path.is_relative_to(settings.storage_path.resolve() / "generated") or not path.is_file():
        raise AppError("锁定的关键帧文件不存在")
    with Image.open(path) as frame:
        width, height = frame.size
    size = "1280x720" if width / height > 1.2 else ("720x1280" if height / width > 1.2 else "960x960")
    attempt_id = str(uuid.uuid4())
    number = (
        await db.scalar(select(func.max(RenderAttempt.attempt_no)).where(RenderAttempt.shot_id == shot.id))
        or 0
    ) + 1
    payload = {
        "model": settings.siliconflow_video_model,
        "image_path": str(path),
        "image_size": size,
        "prompt": f"{shot.action_text}。运镜：{shot.camera_move or '固定镜头'}。保持参考图人物与画风一致。",
        "negative_prompt": "；".join(filter(None, [project.global_negative_prompt, shot.negative_prompt])),
        "seed": attempt_id,
        "project_id": project.id,
        "run_id": run.id,
        "shot_version": shot.version,
        "storyboard_version": project.storyboard_version,
        "image_attempt_id": image.id,
    }
    attempt = RenderAttempt(
        id=attempt_id,
        shot_id=shot.id,
        attempt_no=number,
        stage="video",
        provider="siliconflow",
        model=payload["model"],
        seed=attempt_id,
        request_payload=payload,
        status="pending",
        cost_cents=0,
    )
    attempt.idempotency_key = context_for(attempt).identity(payload)[0]
    db.add(attempt)
    shot.accepted_video_attempt_id = None
    await db.flush()
    return attempt


async def run_video(ctx: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    sessions = ctx["session_factory"]
    cost = CostService(sessions)
    router = build_router(cost)
    async with sessions.begin() as db:
        attempt = await db.get(RenderAttempt, attempt_id)
        if not attempt or attempt.stage != "video":
            raise AppError("视频记录不存在")
        if attempt.status in {"succeeded", "failed"}:
            return {"attempt_id": attempt_id, "status": attempt.status}
        _, shot, run = await current_context(db, attempt.shot_id)
        if not matches(attempt, shot, run):
            raise ConflictError("视频输入版本已失效")
        payload = attempt.request_payload
        context = context_for(attempt)
        request = await db.scalar(
            select(ProviderRequest).where(ProviderRequest.idempotency_key == attempt.idempotency_key)
        )
        request_id = request.id if request else None
        attempt.status = "running"
        attempt.started_at = attempt.started_at or datetime.now(UTC).replace(tzinfo=None)
    try:
        result = (
            await router.reconcile(request_id)
            if request_id
            else await router.generate(ProviderKind.VIDEO, payload, context)
        )
    except Exception as exc:
        async with sessions.begin() as db:
            row = await db.get(RenderAttempt, attempt_id, with_for_update=True)
            if row is not None and row.status != "succeeded":
                row.status = "unknown" if isinstance(exc, ProviderUncertainError) else "pending"
                row.error_code = type(exc).__name__[:60]
        raise
    async with sessions.begin() as db:
        row = await db.get(RenderAttempt, attempt_id, with_for_update=True)
        if row is None:
            raise AppError("视频记录丢失")
        if result is None:
            row.status = "unknown"
        else:
            apply_provider_result(row, result)
        return {"attempt_id": row.id, "status": row.status}


async def decide_video(db: AsyncSession, shot_id: str, body: MediaDecision) -> ReviewGate:
    _, shot, run = await current_context(db, shot_id)
    attempt = await db.get(RenderAttempt, body.attempt_id, with_for_update=True)
    if (
        body.run_id != run.id
        or body.expected_version != shot.version
        or not attempt
        or not matches(attempt, shot, run)
        or attempt.status != "succeeded"
    ):
        raise ConflictError("视频、运行或镜头版本不匹配，请刷新后审核")
    gates = await db.scalars(
        select(ReviewGate)
        .where(ReviewGate.run_id == run.id, ReviewGate.gate_type == "video")
        .with_for_update()
    )
    for gate in gates:
        if gate.snapshot.get("attempt_id") == attempt.id:
            if gate.status != body.status:
                raise ConflictError("该视频已有终审决定")
            return gate
    shot.accepted_video_attempt_id = attempt.id if body.status == "approved" else None
    gate = ReviewGate(
        run_id=run.id,
        gate_type="video",
        status=body.status,
        reviewer=body.reviewer,
        note=body.note,
        snapshot={
            "shot_id": shot.id,
            "attempt_id": attempt.id,
            "shot_version": shot.version,
            "asset_sha256": attempt.asset_sha256,
            "storyboard_version": run.graph_state["storyboard_version"],
        },
        decided_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db.add(gate)
    await db.flush()
    return gate
