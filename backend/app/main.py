"""FastAPI 应用工厂。

启动顺序：配置校验 → 日志 → 连接池预热 → 注册路由与异常处理器。
关闭顺序：释放连接池 → 关闭 Redis。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.db import dispose_engine, engine
from app.core.redis_client import close_redis, get_redis
from app.core.response import register_exception_handlers


def _setup_logging() -> None:
    settings.log_path.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(settings.log_path / "app.log", encoding="utf-8"),
        ],
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    _setup_logging()
    logger = logging.getLogger(__name__)

    settings.storage_path.mkdir(parents=True, exist_ok=True)

    # 预热连接：启动即暴露配置错误，而不是等第一个请求失败
    try:
        from sqlalchemy import text

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("数据库连接正常")
    except Exception as exc:  # noqa: BLE001
        logger.error("数据库连接失败（服务仍启动，便于先排查配置）: %s", exc)

    try:
        await get_redis().ping()
        logger.info("Redis 连接正常")
    except Exception as exc:  # noqa: BLE001
        logger.error("Redis 连接失败（队列与幂等将不可用）: %s", exc)

    if not settings.has_image_credentials():
        logger.warning("未配置图像供应商凭据：抽卡链路不可用，仅能跑本地逻辑")
    if not settings.has_vision_credentials():
        logger.warning("未配置视觉模型凭据：质检 Agent 不可用")

    yield

    await dispose_engine()
    await close_redis()
    logger.info("资源已释放")


def create_app() -> FastAPI:
    app = FastAPI(
        title="分镜流水线 · Storyboard Pipeline",
        description=(
            "AI 漫剧生产流水线的调度与质量控制层。\n\n"
            "不做内容生成模型，解决工业化生产中「黑盒抽卡、品质失控、成本不可见」三大痛点。"
        ),
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    from app.api.v1 import api_router

    app.include_router(api_router, prefix="/api/v1")

    return app


app = create_app()
