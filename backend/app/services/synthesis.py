"""用已通过 C 审核的关键帧生成最小可播放分镜预演。"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, NotFoundError
from app.models.domain import PipelineRun, Project, Scene, Shot
from app.models.tracking import Export, RenderAttempt


def _asset_file(path: str) -> Path:
    root = settings.storage_path.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise AppError("关键帧文件不存在，无法合成分镜")
    return target


async def synthesize_storyboard(
    db: AsyncSession, *, project_id: str, run_id: str | None = None
) -> dict[str, object]:
    project = await db.get(Project, project_id, with_for_update=True)
    if project is None:
        raise NotFoundError("项目不存在")
    run = await db.get(PipelineRun, run_id) if run_id else await db.scalar(
        select(PipelineRun)
        .where(PipelineRun.project_id == project_id)
        .order_by(PipelineRun.created_at.desc())
        .limit(1)
    )
    if run is None or run.project_id != project_id:
        raise NotFoundError("运行记录不存在")
    shots = list(
        await db.scalars(
            select(Shot)
            .join(Scene)
            .where(Scene.project_id == project_id)
            .order_by(Scene.seq, Shot.seq)
        )
    )
    if not shots:
        raise AppError("项目还没有镜头")
    if any(not shot.locked_attempt_id for shot in shots):
        missing = [f"{shot.scene_id}:{shot.seq}" for shot in shots if not shot.locked_attempt_id]
        raise AppError(f"仍有镜头未完成 C 审核：{', '.join(missing)}")

    rows: list[tuple[Path, float]] = []
    for shot in shots:
        attempt = await db.get(RenderAttempt, shot.locked_attempt_id)
        if attempt is None or not attempt.asset_path:
            raise AppError(f"镜头 {shot.id} 的审核关键帧不存在")
        rows.append((_asset_file(attempt.asset_path), max(0.5, shot.duration_ms / 1000)))

    ffmpeg = __import__("shutil").which(settings.ffmpeg_bin)
    if not ffmpeg:
        raise AppError("未找到 FFmpeg，请安装 FFmpeg 并加入 PATH 后重试")
    export_dir = settings.storage_path / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    output = export_dir / f"storyboard-{uuid.uuid4().hex}.mp4"
    export = Export(
        project_id=project_id,
        run_id=run.id,
        status="running",
        resolution="1280x720",
        metrics={"shots": len(rows), "source": "locked_keyframes"},
    )
    db.add(export)
    await db.flush()

    try:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as manifest:
            manifest_path = Path(manifest.name)
            for image, duration in rows:
                # concat demuxer 的 file 行使用绝对路径，-safe 0 允许已校验的 storage 路径。
                escaped = str(image).replace("'", "'\\''")
                manifest.write(f"file '{escaped}'\n")
                manifest.write(f"duration {duration:.3f}\n")
            escaped = str(rows[-1][0]).replace("'", "'\\''")
            manifest.write(f"file '{escaped}'\n")
        process = await asyncio.create_subprocess_exec(
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(manifest_path),
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "-r", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
        if process.returncode != 0:
            raise AppError(f"FFmpeg 合成失败：{stderr.decode('utf-8', errors='replace')[-1000:]}")
        export.status = "succeeded"
        export.output_path = str(output.relative_to(settings.storage_path))
        export.duration_ms = sum(int(duration * 1000) for _, duration in rows)
        export.finished_at = datetime.now(UTC).replace(tzinfo=None)
        await db.flush()
        return {"id": export.id, "status": export.status, "output_path": export.output_path,
                "duration_ms": export.duration_ms, "shots": len(rows)}
    except TimeoutError as exc:
        export.status = "failed"
        export.metrics = {**export.metrics, "error": "timeout"}
        raise AppError("FFmpeg 合成超时") from exc
    finally:
        await asyncio.to_thread(manifest_path.unlink, missing_ok=True)
        if export.status == "failed":
            await asyncio.to_thread(output.unlink, missing_ok=True)
