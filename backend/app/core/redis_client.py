"""Redis 客户端与并发原语。

提供三类能力：
1. 通用异步客户端
2. 供应商并发闸门（信号量，带 TTL 自动释放，防 Worker 崩溃后永久占用）
3. 幂等键（防重试重复扣费）
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import cast

import redis.asyncio as redis

from app.core.config import settings

_pool: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _pool  # noqa: PLW0603
    if _pool is None:
        _pool = redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
            health_check_interval=30,
        )
    return _pool


async def close_redis() -> None:
    global _pool  # noqa: PLW0603
    if _pool is not None:
        await _pool.aclose()
        _pool = None


# ==================== 键命名（集中管理，避免散落拼错）====================

KEY_RUN_STATE = "run:{run_id}:state"
KEY_RUN_PROGRESS = "run:{run_id}:progress"
KEY_BUDGET_HOT = "budget:hot:{project_id}"
KEY_IDEMPOTENCY = "idem:{key}"
KEY_SEMAPHORE = "sem:{provider}"
KEY_SHOT_LOCK = "lock:shot:{shot_id}"

TTL_IDEMPOTENCY = 86400  # 24h
TTL_SEMAPHORE = 60  # 秒；必须短，Worker 崩溃后能自动释放
TTL_SHOT_LOCK = 30
TTL_RUN_STATE = 7 * 86400
TTL_PROGRESS = 86400


# ==================== 供应商并发闸门 ====================
#
# 云 API 免费档并发普遍限制为 1-2，超限应排队而非直接失败。
# 用 Redis 计数器实现跨进程闸门；TTL 保证崩溃后自动释放。


@asynccontextmanager
async def provider_semaphore(
    provider: str,
    max_concurrency: int | None = None,
    wait_timeout_s: float = 120.0,
) -> AsyncIterator[bool]:
    """获取供应商并发额度。

    yield True 表示拿到额度；yield False 表示等待超时（调用方应降级或排队，不要硬失败）。
    """
    limit = max_concurrency or settings.provider_max_concurrency
    key = KEY_SEMAPHORE.format(provider=provider)
    r = get_redis()
    token = uuid.uuid4().hex
    acquired = False

    try:
        # 简单自旋 + 短 sleep；生产可换 Redis Stream 或 BRPOPLPUSH 做公平排队
        deadline = asyncio.get_event_loop().time() + wait_timeout_s
        while asyncio.get_event_loop().time() < deadline:
            current = await r.incr(key)
            if current == 1:
                await r.expire(key, TTL_SEMAPHORE)
            if current <= limit:
                acquired = True
                break
            await r.decr(key)  # 没抢到，回退计数
            await asyncio.sleep(0.5)

        yield acquired
    finally:
        if acquired:
            # 释放：计数减一，归零则删键
            remaining = await r.decr(key)
            if remaining <= 0:
                await r.delete(key)
        _ = token  # 预留：改为带 token 的公平队列时使用


# ==================== 幂等 ====================


async def acquire_idempotency(key: str, ttl: int = TTL_IDEMPOTENCY) -> bool:
    """幂等键抢占。返回 True 表示首次执行，False 表示已执行过（应跳过，防重复扣费）。"""
    r = get_redis()
    # NX + EX：原子操作，不存在才设置并带过期
    return bool(await r.set(KEY_IDEMPOTENCY.format(key=key), "1", nx=True, ex=ttl))


async def release_idempotency(key: str) -> None:
    """执行失败时释放幂等键，允许重试（成功时不释放，防止重复扣费）。"""
    await get_redis().delete(KEY_IDEMPOTENCY.format(key=key))


# ==================== 进度推送（SSE 数据源）====================


async def publish_progress(run_id: str, stage: str, payload: dict) -> None:
    """写进度到 Redis Hash 并推入 List 供 SSE 消费。"""
    r = get_redis()
    pkey = KEY_RUN_PROGRESS.format(run_id=run_id)

    await cast(Awaitable[int], r.hset(pkey, stage, json.dumps(payload, ensure_ascii=False)))
    await r.expire(pkey, TTL_PROGRESS)
    await cast(
        Awaitable[int], r.rpush(f"{pkey}:events", json.dumps({"stage": stage, **payload}, ensure_ascii=False))
    )
    await r.expire(f"{pkey}:events", TTL_PROGRESS)
    await cast(Awaitable[str], r.ltrim(f"{pkey}:events", -500, -1))  # 只保留最近 500 条，防内存膨胀


async def consume_progress(run_id: str, timeout_s: float = 15.0) -> list[str]:
    """阻塞读取进度事件（SSE 用）。"""
    r = get_redis()
    pkey = f"{KEY_RUN_PROGRESS.format(run_id=run_id)}:events"
    result = await cast(Awaitable[list[str]], r.blpop([pkey], timeout=int(timeout_s)))
    return [result[1]] if result else []
