"""生成前的数据版本、审核与恢复。所有写入遵循项目锁 → 当前读。"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, GatePendingError, NotFoundError
from app.models.domain import Character, CharacterRef, PipelineRun, Project, ReviewGate, Scene, Shot
from app.models.tracking import ProviderRequest
from app.schemas import CharacterOut, CharacterRefOut, SceneOut, ShotOut, StoryboardDecision


async def ensure_editable(db: AsyncSession, project: Project) -> None:
    pending = await db.scalar(
        select(ProviderRequest.id)
        .where(
            ProviderRequest.project_id == project.id,
            ProviderRequest.status.in_(["calling", "routing", "unknown", "billing_disputed"]),
        )
        .with_for_update()
        .limit(1)
    )
    if pending:
        raise ConflictError("项目仍有处理中或待对账的模型请求，请核实结果后再修改上游内容")
    running = await db.scalar(
        select(PipelineRun.id)
        .where(PipelineRun.project_id == project.id, PipelineRun.status.in_(["running", "queued"]))
        .with_for_update()
        .limit(1)
    )
    if running:
        raise ConflictError("请先暂停正在运行的生成任务，再修改上游内容")


async def invalidate_board(db: AsyncSession, project: Project, *, character_changed: bool = False) -> None:
    """旧审核不可修改；增加总版本，清除下游可用指针并回到人工审核。"""
    project.storyboard_version += 1
    scenes = list(await db.scalars(select(Scene).where(Scene.project_id == project.id).with_for_update()))
    for scene in scenes:
        scene.baseline_attempt_id = None
    shots = await db.scalars(select(Shot).join(Scene).where(Scene.project_id == project.id).with_for_update())
    for shot in shots:
        shot.locked_attempt_id = None
        shot.prev_locked_attempt_id = None
        shot.status = "pending"
    runs = await db.scalars(select(PipelineRun).where(PipelineRun.project_id == project.id).with_for_update())
    for run in runs:
        if run.status in ("completed", "canceled", "failed"):
            continue
        run.status = "waiting_gate"
        run.current_stage = (
            "character" if character_changed or run.current_stage == "character" else "storyboard"
        )
        run.graph_state = {
            "mode": "preparation",
            "storyboard_version": project.storyboard_version,
            "blockers": ["上游内容已更新，请重新检查并审核当前版本"],
        }
        run.error_code = None
        run.error_message = None


async def get_run(db: AsyncSession, project: Project, run_id: str) -> PipelineRun:
    run = await db.scalar(select(PipelineRun).where(PipelineRun.id == run_id).with_for_update())
    if run is None or run.project_id != project.id:
        raise NotFoundError("该项目中不存在此运行")
    if run.status in ("completed", "canceled", "failed", "running", "queued"):
        raise ConflictError("当前运行不可修改准备状态")
    return run


async def board_snapshot(db: AsyncSession, project: Project, run_id: str) -> dict[str, Any]:
    characters = list(
        await db.scalars(
            select(Character)
            .where(Character.project_id == project.id)
            .order_by(Character.created_at, Character.id)
            .with_for_update()
        )
    )
    refs = list(
        await db.scalars(
            select(CharacterRef)
            .join(Character)
            .where(Character.project_id == project.id)
            .order_by(CharacterRef.created_at, CharacterRef.id)
            .with_for_update()
        )
    )
    scenes = list(
        await db.scalars(
            select(Scene).where(Scene.project_id == project.id).order_by(Scene.seq).with_for_update()
        )
    )
    shots = list(
        await db.scalars(
            select(Shot)
            .join(Scene)
            .where(Scene.project_id == project.id)
            .order_by(Scene.seq, Shot.seq)
            .with_for_update()
        )
    )
    gates = list(
        await db.scalars(
            select(ReviewGate)
            .where(ReviewGate.run_id == run_id)
            .order_by(ReviewGate.created_at, ReviewGate.id)
            .with_for_update()
        )
    )
    blockers: list[str] = []
    unconfirmed = [
        c.name
        for c in characters
        if not c.confirmed
        or not any(
            g.gate_type == "character"
            and g.status == "approved"
            and g.snapshot.get("id") == c.id
            and g.snapshot.get("anchor_version") == c.anchor_version
            for g in gates
        )
    ]
    if unconfirmed:
        blockers.append("请完成 A 角色确认：" + "、".join(unconfirmed))
    used_ids = {cid for shot in shots for cid in shot.character_ids}
    for character in characters:
        if character.id in used_ids and not any(
            ref.character_id == character.id
            and ref.qc_passed
            and ref.is_primary
            and ref.ref_type == "front_half"
            and ref.reviewed_anchor_version == character.anchor_version
            for ref in refs
        ):
            blockers.append(f"{character.name} 缺少当前角色版本审核通过的正面半身主参考图")
    if not scenes or not shots:
        blockers.append("请至少建立一个场景和一个镜头")
    for scene in scenes:
        if not any(s.scene_id == scene.id for s in shots):
            blockers.append(f"场景 {scene.seq} 尚无镜头")
    latest_b = next(
        (
            g
            for g in reversed(gates)
            if g.gate_type == "storyboard"
            and g.snapshot.get("storyboard_version") == project.storyboard_version
        ),
        None,
    )
    return {
        "project_id": project.id,
        "run_id": run_id,
        "storyboard_version": project.storyboard_version,
        "style": project.style,
        "global_negative_prompt": project.global_negative_prompt,
        "characters": [CharacterOut.model_validate(c).model_dump(mode="json") for c in characters],
        "references": [CharacterRefOut.model_validate(r).model_dump(mode="json") for r in refs],
        "scenes": [SceneOut.model_validate(s).model_dump(mode="json") for s in scenes],
        "shots": [ShotOut.model_validate(s).model_dump(mode="json") for s in shots],
        "blockers": blockers,
        "characters_ready": not unconfirmed,
        "gate": {"id": latest_b.id, "status": latest_b.status} if latest_b else None,
    }


async def advance_preparation(db: AsyncSession, project: Project, run: PipelineRun) -> dict[str, Any]:
    board = await board_snapshot(db, project, run.id)
    if not board["characters_ready"]:
        stage, status = "character", "waiting_gate"
    elif board["blockers"] or not board["gate"] or board["gate"]["status"] != "approved":
        stage, status = "storyboard", "waiting_gate"
    else:
        stage, status = "keyframe_render", "waiting_model"
    run.current_stage, run.status = stage, status
    run.graph_state = {
        "mode": "preparation",
        "storyboard_version": project.storyboard_version,
        "blockers": board["blockers"],
        "approved_gate_id": board["gate"]["id"] if board["gate"] else None,
        "pending_shot_ids": [s["id"] for s in board["shots"]],
    }
    run.error_code = "PROVIDER_SETUP_REQUIRED" if status == "waiting_model" else None
    run.error_message = (
        "准备已完成，下一步需接入图像生成与视觉质检模型" if status == "waiting_model" else None
    )
    await db.flush()
    return board


async def decide_storyboard(db: AsyncSession, project: Project, body: StoryboardDecision) -> ReviewGate:
    await ensure_editable(db, project)
    run = await get_run(db, project, body.run_id)
    if project.storyboard_version != body.storyboard_version:
        raise ConflictError("分镜或上游内容已变化，请刷新后重新审核")
    board = await advance_preparation(db, project, run)
    if board["blockers"]:
        raise GatePendingError("当前准备项尚未完成", detail={"blockers": board["blockers"]})
    if board["gate"]:
        gate = await db.get(ReviewGate, board["gate"]["id"])
        assert gate is not None
        if gate.status == body.status:
            return gate
        raise ConflictError("此版本已作出审核决定；请修改分镜后审核新版本")
    gate = ReviewGate(
        run_id=run.id,
        gate_type="storyboard",
        status=body.status,
        reviewer=body.reviewer,
        note=body.note,
        snapshot=board,
        decided_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db.add(gate)
    await db.flush()
    await advance_preparation(db, project, run)
    await db.refresh(gate)
    return gate
