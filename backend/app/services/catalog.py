"""项目/角色准备阶段。项目锁统一串行化同项目的建档、修改与审核。"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ConflictError, NotFoundError
from app.knowledge.anchor import AnchorInput, _check_subjective, build_anchor_prompt
from app.models import load_all_models
from app.models.domain import Character, PipelineRun, Project, ReviewGate
from app.schemas import CharacterConfirm, CharacterCreate, CharacterOut, CharacterUpdate

load_all_models()
FEATURES = ("face_features", "hair_features", "body_features", "outfit_features", "style_lock")


async def project_or_404(db: AsyncSession, project_id: str, *, lock: bool = False) -> Project:
    stmt = select(Project).where(Project.id == project_id)
    if lock:
        stmt = stmt.with_for_update()
    project = await db.scalar(stmt)
    if project is None:
        raise NotFoundError("项目不存在")
    return project


async def character_or_404(db: AsyncSession, character_id: str) -> Character:
    character = await db.get(Character, character_id)
    if character is None:
        raise NotFoundError("角色不存在")
    return character


def anchor_input(character: Character) -> AnchorInput:
    return AnchorInput(name=character.name, **{key: getattr(character, key) for key in FEATURES})


def character_data(character: Character) -> dict[str, Any]:
    data = CharacterOut.model_validate(character).model_dump(mode="json")
    data["subjective_word_hits"] = sorted(
        {hit for key in FEATURES for hit in _check_subjective(getattr(character, key))}
    )
    return data


async def flush_character(db: AsyncSession) -> None:
    try:
        await db.flush()
    except IntegrityError as exc:
        if exc.orig is not None and exc.orig.args and exc.orig.args[0] == 1062:
            raise ConflictError("该项目中已有同名角色") from exc
        raise


async def save_character(
    db: AsyncSession, body: CharacterCreate | CharacterUpdate, character_id: str | None = None
) -> Character:
    await project_or_404(db, body.project_id, lock=True)
    # 全部修改和确认均先锁项目，角色读取不能使用锁前的旧快照。
    if character_id:
        character = await character_or_404(db, character_id)
        if character.project_id != body.project_id:
            raise AppError("不能将角色移到另一个项目")
        if not isinstance(body, CharacterUpdate) or character.anchor_version != body.expected_version:
            raise ConflictError("角色已更新，请刷新后重试")
        character.anchor_version += 1
    else:
        character = Character(project_id=body.project_id, anchor_version=1)
        db.add(character)
    character.name = body.name
    for key in FEATURES:
        setattr(character, key, getattr(body, key))
    character.anchor_prompt = build_anchor_prompt(anchor_input(character))
    if len(character.anchor_prompt.encode("utf-8")) > 65535:
        raise AppError("角色特征总长度过大，请精简描述后重试")
    character.confirmed = False
    character.confirmed_at = None
    await flush_character(db)
    return character


async def confirm_character_version(db: AsyncSession, character_id: str, body: CharacterConfirm) -> Character:
    # 先读取归属 ID，不把旧 ORM 实例放进 identity map。
    project_id = await db.scalar(select(Character.project_id).where(Character.id == character_id))
    if project_id is None:
        raise NotFoundError("角色不存在")
    await project_or_404(db, project_id, lock=True)
    character = await db.scalar(select(Character).where(Character.id == character_id).with_for_update())
    assert character is not None
    run = await db.scalar(select(PipelineRun).where(PipelineRun.id == body.run_id).with_for_update())
    if run is None or run.project_id != project_id:
        raise AppError("审核运行不属于该角色的项目")
    if run.status != "waiting_gate" or run.current_stage != "character":
        raise ConflictError("该运行当前不在角色确认阶段")
    if character.anchor_version != body.anchor_version:
        raise ConflictError("角色版本已变化，请刷新后重新审核")
    if not any(getattr(character, key) for key in FEATURES[:4]):
        raise AppError("请先填写至少一组客观角色特征")
    gates = await db.scalars(
        select(ReviewGate)
        .where(
            ReviewGate.run_id == run.id, ReviewGate.gate_type == "character", ReviewGate.status == "approved"
        )
        .with_for_update()
    )
    if any(
        g.snapshot.get("id") == character.id and g.snapshot.get("anchor_version") == body.anchor_version
        for g in gates
    ):
        return character
    character.confirmed = True
    character.confirmed_at = datetime.now(UTC).replace(tzinfo=None)
    db.add(
        ReviewGate(
            run_id=run.id,
            gate_type="character",
            status="approved",
            reviewer=body.reviewer,
            snapshot=character_data(character),
            decided_at=character.confirmed_at,
        )
    )
    await db.flush()
    return character
