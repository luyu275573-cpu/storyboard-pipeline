"""角色建档、锚定预览与版本化人工确认。"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.core.response import ok
from app.knowledge.anchor import build_anchor_prompt, build_negative_prompt
from app.models.domain import Character, CharacterRef
from app.schemas import (
    AnchorPreviewRequest,
    CharacterConfirm,
    CharacterCreate,
    CharacterRefOut,
    CharacterUpdate,
    ReferenceReview,
)
from app.services.catalog import (
    anchor_input,
    character_data,
    character_or_404,
    confirm_character_version,
    project_or_404,
    save_character,
)
from app.services.reference_assets import add_reference, asset_file, review_reference

router = APIRouter()


@router.post("", summary="创建角色档案", status_code=201)
async def create_character(body: CharacterCreate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return ok(character_data(await save_character(db, body)))


@router.get("", summary="角色列表")
async def list_characters(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await project_or_404(db, project_id)
    rows = await db.scalars(
        select(Character)
        .where(Character.project_id == project_id)
        .order_by(Character.created_at, Character.id)
    )
    return ok([character_data(row) for row in rows])


@router.get("/{character_id}", summary="角色详情")
async def get_character(character_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    data = character_data(await character_or_404(db, character_id))
    refs = await db.scalars(select(CharacterRef).where(CharacterRef.character_id == character_id))
    data["refs"] = [CharacterRefOut.model_validate(ref).model_dump(mode="json") for ref in refs]
    return ok(data)


@router.put("/{character_id}", summary="更新角色特征并取消旧确认")
async def update_character(
    character_id: str, body: CharacterUpdate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return ok(character_data(await save_character(db, body, character_id)))


@router.post("/{character_id}/anchor-preview", summary="预览锚定描述")
async def preview_anchor(
    character_id: str, body: AnchorPreviewRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    character = await character_or_404(db, character_id)
    project = await project_or_404(db, character.project_id)
    return ok(
        {
            "character_id": character.id,
            "anchor_version": character.anchor_version,
            "level": body.level,
            "anchor_prompt": build_anchor_prompt(
                anchor_input(character), level=body.level, strengthen_fields=body.strengthen_fields
            ),
            "negative_prompt": build_negative_prompt(project.global_negative_prompt, character.style_lock),
            "subjective_word_hits": character_data(character)["subjective_word_hits"],
        }
    )


@router.post("/{character_id}/confirm", summary="人机关卡 A：确认当前角色版本")
async def confirm_character(
    character_id: str, body: CharacterConfirm, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return ok(character_data(await confirm_character_version(db, character_id, body)))


@router.post("/{character_id}/refs", summary="上传参考图原始文件", status_code=201)
async def add_ref(
    character_id: str,
    request: Request,
    ref_type: str = Query(max_length=40),
    anchor_version: int = Query(ge=1),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    limit = settings.max_upload_mb * 1024 * 1024
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            raise AppError(f"文件不能超过 {settings.max_upload_mb} MB")
        data.extend(chunk)
    ref = await add_reference(db, character_id, anchor_version, ref_type, bytes(data))
    return ok(CharacterRefOut.model_validate(ref).model_dump(mode="json"))


@router.get("/{character_id}/refs", summary="参考图列表")
async def list_refs(character_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    await character_or_404(db, character_id)
    refs = await db.scalars(
        select(CharacterRef)
        .where(CharacterRef.character_id == character_id)
        .order_by(CharacterRef.created_at, CharacterRef.id)
    )
    return ok([CharacterRefOut.model_validate(r).model_dump(mode="json") for r in refs])


@router.post("/refs/{ref_id}/review", summary="参考图人工审核与主参考选择")
async def review_ref(
    ref_id: str, body: ReferenceReview, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    return ok(
        CharacterRefOut.model_validate(await review_reference(db, ref_id, body)).model_dump(mode="json")
    )


@router.get("/refs/{ref_id}/asset", summary="读取已登记的参考图")
async def get_ref_asset(ref_id: str, db: AsyncSession = Depends(get_db)) -> FileResponse:
    import asyncio

    ref = await db.get(CharacterRef, ref_id)
    if ref is None:
        raise NotFoundError("参考图不存在")
    path = await asyncio.to_thread(asset_file, ref)
    return FileResponse(path, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})
