"""角色与锚定路由。

角色档案是三级一致性锚定的第 1 级。anchor_prompt 必须程序化拼装，
禁止前端直接传入手写 Prompt——人手写会不自觉换措辞，模型会当成不同角色。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.schemas import AnchorPreview, CharacterCreate, CharacterOut, CharacterRefCreate, CharacterRefOut

router = APIRouter()


@router.post("", summary="创建角色档案")
async def create_character(body: CharacterCreate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """建档并生成 anchor_prompt v1。

    TODO(impl):
      1. 校验 project_id 存在
      2. 调 build_anchor_prompt(AnchorInput(...)) 拼装锚定描述
      3. anchor_version = 1，confirmed = False
      4. 若返回的 subjective_word_hits 非空，在响应里带上告警但不阻断建档
      5. 返回 CharacterOut
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("", summary="角色列表")
async def list_characters(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 按 project_id 过滤。"""
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/{character_id}", summary="角色详情")
async def get_character(character_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 含 refs 列表。查不到抛 NotFoundError。"""
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.put("/{character_id}", summary="更新角色特征")
async def update_character(
    character_id: str, body: CharacterCreate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """更新特征并递增 anchor_version。

    TODO(impl):
      1. 重新拼装 anchor_prompt
      2. anchor_version += 1（关键：质检报告记录了判定时的版本，
         不递增版本会导致优化效果无法归因）
      3. 乐观锁：UPDATE ... WHERE id=? AND version=?，rowcount=0 抛 ConflictError
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.post("/{character_id}/anchor-preview", summary="锚定 Prompt 预览")
async def preview_anchor(
    character_id: str,
    level: str = "normal",
    strengthen_fields: list[str] | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """预览不同锚定强度下的 Prompt，不落库。

    用途：质检返回 drifted_fields 后，先预览强化后的 Prompt 再决定是否重抽。

    TODO(impl):
      1. 读角色档案
      2. build_anchor_prompt(level=level, strengthen_fields=strengthen_fields or [])
      3. build_negative_prompt(project.global_negative_prompt, style_lock)
      4. 返回 AnchorPreview（含 subjective_word_hits）
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.post("/{character_id}/confirm", summary="人机关卡 A：角色设定确认")
async def confirm_character(
    character_id: str, reviewer: str | None = None, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """强制关卡，不可跳过。未确认的角色不能进入分镜与抽卡。

    TODO(impl):
      1. confirmed = True, confirmed_at = now
      2. 写 review_gates(gate_type='character', status='approved', snapshot=当前特征快照)
      3. snapshot 必须存，防事后篡改争议
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


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
