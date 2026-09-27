"""Queue video assembly; preview exports never imply human acceptance."""

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError
from app.models.domain import PipelineRun, Scene, Shot
from app.models.tracking import Export, RenderAttempt
from app.providers.video_siliconflow import probe_video
from app.services.catalog import project_or_404
from app.services.video import matches


async def prepare_export(db: AsyncSession, project_id: str, run_id: str, preview: bool) -> Export:
    project = await project_or_404(db, project_id, lock=True)
    run = await db.get(PipelineRun, run_id)
    if (
        not run
        or run.project_id != project.id
        or run.graph_state.get("storyboard_version") != project.storyboard_version
    ):
        raise AppError("运行与当前分镜版本不匹配")
    shots = list(
        await db.scalars(
            select(Shot).join(Scene).where(Scene.project_id == project_id).order_by(Scene.seq, Shot.seq)
        )
    )
    if not shots:
        raise AppError("项目没有镜头")
    sources = []
    for shot in shots:
        attempt = (
            await db.get(RenderAttempt, shot.accepted_video_attempt_id)
            if shot.accepted_video_attempt_id
            else None
        )
        if preview and attempt is None:
            attempts = await db.scalars(
                select(RenderAttempt)
                .where(
                    RenderAttempt.shot_id == shot.id,
                    RenderAttempt.stage == "video",
                    RenderAttempt.status == "succeeded",
                )
                .order_by(RenderAttempt.attempt_no.desc())
            )
            attempt = next((a for a in attempts if matches(a, shot, run)), None)
        if (
            not attempt
            or not matches(attempt, shot, run)
            or attempt.status != "succeeded"
            or not attempt.asset_path
        ):
            raise AppError(f"镜头 {shot.seq} 缺少当前版本的{'视频' if preview else '终审视频'}")
        sources.append({"attempt_id": attempt.id, "path": attempt.asset_path})
    export = Export(
        project_id=project_id,
        run_id=run.id,
        status="pending",
        resolution="1280x720",
        metrics={
            "source": "generated_videos",
            "preview": preview,
            "sources": sources,
            "storyboard_version": project.storyboard_version,
        },
    )
    db.add(export)
    await db.flush()
    return export


async def _ffmpeg(*arguments: str) -> None:
    process = await asyncio.create_subprocess_exec(
        settings.ffmpeg_bin,
        "-nostdin",
        "-y",
        *arguments,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), 120)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise AppError(f"视频合成失败：{stderr.decode(errors='replace')[-500:]}")


def _preview_mark(path: Path) -> None:
    image = Image.new("RGBA", (520, 48), (0, 0, 0, 180))
    ImageDraw.Draw(image).text((12, 14), "PREVIEW - NOT REVIEWED", fill="white")
    image.save(path)


async def run_video_export(ctx: dict[str, Any], export_id: str) -> dict[str, Any]:
    sessions = ctx["session_factory"]
    async with sessions.begin() as db:
        export = await db.get(Export, export_id, with_for_update=True)
        if not export:
            raise AppError("导出任务不存在")
        if export.status != "pending":
            return {"id": export.id, "status": export.status}
        export.status = "running"
        metrics = export.metrics
    root = settings.storage_path.resolve()
    output = root / "exports" / f"{'preview' if metrics['preview'] else 'video'}-{export_id}.mp4"
    try:
        await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sbp-video-") as temporary:
            paths = []
            for index, source in enumerate(metrics["sources"]):
                path = (root / source["path"]).resolve()
                if not path.is_relative_to(root / "generated") or not path.is_file():
                    raise AppError("视频源文件不存在")
                clip = Path(temporary) / f"{index}.mp4"
                filters = (
                    "scale=1280:720:force_original_aspect_ratio=decrease,"
                    "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24"
                )
                if metrics["preview"]:
                    mark = Path(temporary) / "preview.png"
                    await asyncio.to_thread(_preview_mark, mark)
                    await _ffmpeg("-i", str(path), "-i", str(mark), "-filter_complex",
                                  f"[0:v]{filters}[base];[base][1:v]overlay=24:24", "-an",
                                  "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip))
                else:
                    await _ffmpeg("-i", str(path), "-vf", filters, "-an", "-c:v", "libx264",
                                  "-pix_fmt", "yuv420p", str(clip))
                paths.append(clip)
            manifest = Path(temporary) / "concat.txt"
            text = "".join("file '" + p.as_posix().replace("'", "'\\''") + "'\n" for p in paths)
            await asyncio.to_thread(manifest.write_text, text, encoding="utf-8")
            await _ffmpeg(
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            )
        metadata = await probe_video(output)
        async with sessions.begin() as db:
            export = await db.get(Export, export_id, with_for_update=True)
            assert export is not None
            export.status = "succeeded"
            export.output_path = output.relative_to(root).as_posix()
            export.duration_ms = metadata["duration_ms"]
            export.metrics = {**metrics, **metadata}
            export.finished_at = datetime.now(UTC).replace(tzinfo=None)
        return {"id": export_id, "status": "succeeded"}
    except BaseException:
        async with sessions.begin() as db:
            export = await db.get(Export, export_id, with_for_update=True)
            if export is not None:
                export.status = "failed"
        await asyncio.to_thread(output.unlink, missing_ok=True)
        raise
