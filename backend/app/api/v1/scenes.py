"""场景与手工分镜的管理。"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.response import ok
from app.models.domain import Scene
from app.schemas import SceneCreate, SceneOut, SceneUpdate
from app.services.catalog import project_or_404
from app.services.preparation import ensure_editable, invalidate_board

router = APIRouter()


async def flush_sequence(db: AsyncSession) -> None:
    try:
        await db.flush()
    except IntegrityError as exc:
        if exc.orig is not None and exc.orig.args and exc.orig.args[0] == 1062:
            raise ConflictError("此序号已被使用，请刷新或选择其他序号") from exc
        raise


@router.post("", status_code=201)
async def create_scene(body: SceneCreate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    project = await project_or_404(db, body.project_id, lock=True)
    await ensure_editable(db, project)
    count = await db.scalar(select(func.count()).select_from(Scene).where(Scene.project_id == project.id))
    if count and count >= 50:
        raise AppError("每个项目最多 50 个场景")
    scene = Scene(**body.model_dump())
    db.add(scene)
    await invalidate_board(db, project)
    await flush_sequence(db)
    return ok(SceneOut.model_validate(scene).model_dump(mode="json"))


@router.get("")
async def list_scenes(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await project_or_404(db, project_id)
    scenes = await db.scalars(select(Scene).where(Scene.project_id == project_id).order_by(Scene.seq))
    return ok([SceneOut.model_validate(s).model_dump(mode="json") for s in scenes])


@router.put("/{scene_id}")
async def update_scene(
    scene_id: str, body: SceneUpdate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    project = await project_or_404(db, body.project_id, lock=True)
    await ensure_editable(db, project)
    scene = await db.scalar(select(Scene).where(Scene.id == scene_id).with_for_update())
    if scene is None or scene.project_id != project.id:
        raise NotFoundError("该项目中不存在此场景")
    if project.storyboard_version != body.expected_storyboard_version:
        raise ConflictError("分镜版本已更新，请刷新后重试")
    for key, value in body.model_dump(exclude={"expected_storyboard_version", "project_id"}).items():
        setattr(scene, key, value)
    await invalidate_board(db, project)
    await flush_sequence(db)
    return ok(SceneOut.model_validate(scene).model_dump(mode="json"))


@router.delete("/{scene_id}")
async def delete_scene(
    scene_id: str, expected_storyboard_version: int = Query(ge=1), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    project_id = await db.scalar(select(Scene.project_id).where(Scene.id == scene_id))
    if not project_id:
        raise NotFoundError("场景不存在")
    project = await project_or_404(db, project_id, lock=True)
    await ensure_editable(db, project)
    scene = await db.scalar(select(Scene).where(Scene.id == scene_id).with_for_update())
    if scene is None:
        raise NotFoundError("场景不存在")
    if project.storyboard_version != expected_storyboard_version:
        raise ConflictError("分镜版本已更新，请刷新后重试")
    from app.models.domain import Shot

    if await db.scalar(select(Shot.id).where(Shot.scene_id == scene.id).with_for_update().limit(1)):
        raise ConflictError("请先移除场景中的镜头，再删除空场景")
    await db.delete(scene)
    await invalidate_board(db, project)
    await db.flush()
    return ok({"deleted": scene_id})
