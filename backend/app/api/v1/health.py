"""健康检查：数据库、Redis、FFmpeg、供应商凭据配置状态。

部署后第一件事就是打这个接口，它会把配置缺失一次性暴露出来，
而不是等抽卡跑到一半才发现凭据没填。
"""

from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import settings
from app.core.db import engine
from app.core.redis_client import get_redis
from app.core.response import ok

router = APIRouter()


async def _check_db() -> dict[str, Any]:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


async def _check_redis() -> dict[str, Any]:
    try:
        await get_redis().ping()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


def _check_ffmpeg() -> dict[str, Any]:
    ffmpeg = shutil.which(settings.ffmpeg_bin)
    ffprobe = shutil.which(settings.ffprobe_bin)
    return {
        "ok": bool(ffmpeg and ffprobe),
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "hint": None if (ffmpeg and ffprobe) else "FFmpeg 未安装或不在 PATH，视频合成将不可用",
    }


def _check_providers() -> dict[str, Any]:
    """凭据配置状态。只报是否配置，绝不回显密钥内容。"""
    return {
        "image": {
            "jimeng": bool(settings.volc_access_key and settings.volc_secret_key),
            "vidu": bool(settings.vidu_api_key),
            "siliconflow": bool(settings.siliconflow_api_key),
        },
        "video": {
            "kling": bool(settings.kling_access_key and settings.kling_secret_key),
            "jimeng": bool(settings.volc_access_key and settings.volc_secret_key),
        },
        # 质检走免费 Token 额度，未配置则质检 Agent 不可用
        "vision": {
            "dashscope": bool(settings.dashscope_api_key),
            "siliconflow": bool(settings.siliconflow_api_key),
        },
        "llm": {
            "dashscope": bool(settings.dashscope_api_key),
            "siliconflow": bool(settings.siliconflow_api_key),
        },
        "image_chain": settings.image_chain,
        "video_chain": settings.video_chain,
        "vision_chain": settings.vision_chain,
        "llm_chain": settings.llm_chain,
    }


@router.get("/health", summary="健康检查")
async def health() -> dict[str, Any]:
    db = await _check_db()
    redis = await _check_redis()
    ffmpeg = _check_ffmpeg()
    providers = _check_providers()

    budget_total_yuan = round(settings.budget_total_cents / 100, 2)
    data = {
        "app": "storyboard-pipeline",
        "env": settings.app_env,
        "version": "0.1.0",
        "database": db,
        "redis": redis,
        "ffmpeg": ffmpeg,
        "providers": providers,
        "budget": {
            "total_yuan": budget_total_yuan,
            "per_shot_yuan": round(settings.budget_per_shot_cents / 100, 2),
            "warn_ratio": settings.budget_warn_ratio,
            "degrade_ratio": settings.budget_degrade_ratio,
            "shot_max_retry": settings.shot_max_retry,
        },
        "storage_root": str(settings.storage_path),
        # 整体就绪：DB + Redis 必须可用，其余按功能降级
        "ready": db["ok"] and redis["ok"],
    }
    return ok(data)
