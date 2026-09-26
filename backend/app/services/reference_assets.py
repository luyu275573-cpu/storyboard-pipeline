"""只接受真实位图；重新编码并按内容哈希存储，不接收客户端路径。"""

import asyncio
import hashlib
import io
import warnings
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.models.domain import Character, CharacterRef, ReviewGate
from app.schemas import CharacterRefOut, ReferenceReview
from app.services.catalog import project_or_404
from app.services.preparation import ensure_editable, get_run, invalidate_board

REF_TYPES = ("front_half", "side_half", "full_body", "expression_happy", "expression_angry", "expression_sad")


def store_image(data: bytes) -> tuple[str, str]:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ("PNG", "JPEG", "WEBP") or getattr(image, "n_frames", 1) != 1:
                    raise AppError("仅支持单帧 PNG、JPEG、WebP 图片")
                if min(image.size) < 64 or max(image.size) > 8192 or image.width * image.height > 32_000_000:
                    raise AppError("图片每边需为 64–8192 像素，总像素不超过 3200 万")
                image.load()
                clean = ImageOps.exif_transpose(image).convert("RGB")
                clean.info.clear()
                output = io.BytesIO()
                clean.save(output, format="PNG")
                normalized = output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ) as exc:
        raise AppError("图片损坏或格式不受支持，请上传完整的位图文件") from exc
    if len(normalized) > settings.max_upload_mb * 1024 * 1024:
        raise AppError("解码后的图片过大，请缩小尺寸后上传")
    digest = hashlib.sha256(normalized).hexdigest()
    relative = f"refs/{digest}.png"
    path = settings.storage_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    # 内容寻址：相同内容同一路径。用临时文件原子替换，避免读到半张图。
    import tempfile

    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temporary = Path(temp.name)
        temp.write(normalized)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return relative, digest


def asset_file(ref: CharacterRef) -> Path:
    root = settings.storage_path.resolve()
    target = (root / ref.asset_path).resolve()
    if not target.is_relative_to(root / "refs") or target.suffix != ".png" or not target.is_file():
        raise NotFoundError("参考图文件不存在或路径无效，请重新上传")
    return target


async def locked_character(db: AsyncSession, character_id: str):
    project_id = await db.scalar(select(Character.project_id).where(Character.id == character_id))
    if not project_id:
        raise NotFoundError("角色不存在")
    project = await project_or_404(db, project_id, lock=True)
    await ensure_editable(db, project)
    character = await db.scalar(select(Character).where(Character.id == character_id).with_for_update())
    assert character is not None
    return project, character


async def add_reference(
    db: AsyncSession, character_id: str, anchor_version: int, ref_type: str, data: bytes
) -> CharacterRef:
    if ref_type not in REF_TYPES:
        raise AppError("参考图类型无效")
    project, character = await locked_character(db, character_id)
    if character.anchor_version != anchor_version:
        raise ConflictError("角色版本已更新，请刷新后再上传")
    refs = list(
        await db.scalars(
            select(CharacterRef).where(CharacterRef.character_id == character_id).with_for_update()
        )
    )
    if len(refs) >= 20:
        raise AppError("每个角色最多保存 20 张参考图")
    path, digest = await asyncio.to_thread(store_image, data)
    existing = next((r for r in refs if r.asset_sha256 == digest), None)
    if existing:
        if existing.ref_type != ref_type:
            raise ConflictError("相同图片已登记为其他参考图类型")
        return existing
    ref = CharacterRef(
        character_id=character_id,
        ref_type=ref_type,
        asset_path=path,
        asset_sha256=digest,
        uploaded_anchor_version=anchor_version,
    )
    db.add(ref)
    await invalidate_board(db, project)
    await db.flush()
    return ref


async def review_reference(db: AsyncSession, ref_id: str, body: ReferenceReview) -> CharacterRef:
    character_id = await db.scalar(select(CharacterRef.character_id).where(CharacterRef.id == ref_id))
    if not character_id:
        raise NotFoundError("参考图不存在")
    project, character = await locked_character(db, character_id)
    run = await get_run(db, project, body.run_id)
    ref = await db.scalar(select(CharacterRef).where(CharacterRef.id == ref_id).with_for_update())
    assert ref is not None
    if character.anchor_version != body.anchor_version or ref.review_version != body.expected_review_version:
        raise ConflictError("角色或参考图审核已更新，请刷新后重新核对")
    if body.is_primary and (not body.passed or ref.ref_type != "front_half"):
        raise AppError("主参考图必须是审核通过的正面半身图")
    await asyncio.to_thread(asset_file, ref)
    if body.is_primary:
        others = await db.scalars(
            select(CharacterRef)
            .where(CharacterRef.character_id == character_id, CharacterRef.id != ref_id)
            .with_for_update()
        )
        for other in others:
            other.is_primary = False
    ref.qc_passed, ref.is_primary = body.passed, body.is_primary
    ref.reviewed_anchor_version, ref.reviewed_by = body.anchor_version, body.reviewer
    ref.reviewed_at, ref.review_note = datetime.now(UTC).replace(tzinfo=None), body.note
    ref.review_version += 1
    db.add(
        ReviewGate(
            run_id=run.id,
            gate_type="reference",
            status="approved" if body.passed else "rejected",
            reviewer=body.reviewer,
            note=body.note,
            decided_at=ref.reviewed_at,
            snapshot={
                **CharacterRefOut.model_validate(ref).model_dump(mode="json"),
                "name": character.name,
                "anchor_version": character.anchor_version,
                "anchor_prompt": character.anchor_prompt,
            },
        )
    )
    await invalidate_board(db, project)
    await db.flush()
    return ref
