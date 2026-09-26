"""角色建档、锚定预览与版本化人工确认。"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.response import ok
from app.knowledge.anchor import build_anchor_prompt, build_negative_prompt
from app.models.domain import Character, CharacterRef
from app.schemas import (
    AnchorPreviewRequest,
    CharacterConfirm,
    CharacterCreate,
    CharacterRefCreate,
    CharacterRefOut,
    CharacterUpdate,
)
from app.services.catalog import (
    anchor_input,
    character_data,
    character_or_404,
    confirm_character_version,
    project_or_404,
    save_character,
)

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


# ==================== 基准图（第 2 级锚定）====================


@router.post("/{character_id}/refs", summary="上传角色基准图")
async def add_ref(
    character_id: str, body: CharacterRefCreate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """登记基准图。

    TODO(impl):
      1. 计算文件 sha256 存 asset_sha256（去重与幂等用）
      2. qc_passed 默认 False —— 基准图本身必须先过质检，
         基准图有畸形则后续全崩，这是第一周要先做的事
      3. is_primary=True 时把同角色其他主基准图置 False
      4. 返回 CharacterRefOut
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/{character_id}/refs", summary="基准图列表")
async def list_refs(character_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 按 ref_type 分组返回。"""
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.post("/refs/{ref_id}/qc-pass", summary="标记基准图质检通过")
async def pass_ref_qc(ref_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): qc_passed = True。未通过的基准图不允许用于生成。"""
    raise NotImplementedError("TODO(impl): 由 Codex 实现")
