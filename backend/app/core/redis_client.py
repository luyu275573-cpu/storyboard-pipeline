"""Redis 客户端与并发原语。

提供三类能力：
1. 通用异步客户端
2. 供应商并发租约（独立令牌、续租、条件释放）
3. 进度队列（M4 将替换为广播重放）；付费幂等由 MySQL 负责
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TypeVar, cast

import redis.asyncio as redis

from app.core.config import settings
from app.core.errors import ProviderUncertainError

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
KEY_SEMAPHORE = "lease:provider:{provider}"
KEY_SHOT_LOCK = "lock:shot:{shot_id}"

TTL_SEMAPHORE = 60  # 持有者每 TTL/3 续租
TTL_SHOT_LOCK = 30
TTL_RUN_STATE = 7 * 86400
TTL_PROGRESS = 86400


# ==================== 供应商并发租约 ====================

# Redis TIME 避免不同 Worker 时钟偏移；每个令牌有独立到期时间。
_ACQUIRE = """
local t = redis.call('TIME')
local now = t[1]*1000 + math.floor(t[2]/1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], now + ARGV[3], ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[3]*2)
return 1
"""
_RENEW = """
local t = redis.call('TIME')
local now = t[1]*1000 + math.floor(t[2]/1000)
local expiry = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not expiry or tonumber(expiry) <= now then return 0 end
redis.call('ZADD', KEYS[1], now + ARGV[2], ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[2]*2)
return 1
"""
_RELEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
if redis.call('ZCARD', KEYS[1]) == 0 then redis.call('DEL', KEYS[1]) end
return 1
"""
T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass
class ProviderLease:
    key: str
    token: str
    ttl_ms: int
    client: redis.Redis
    lost: asyncio.Event = field(default_factory=asyncio.Event)

    async def renew(self) -> bool:
        return bool(
            await cast(Awaitable[int], self.client.eval(_RENEW, 1, self.key, self.token, str(self.ttl_ms)))
        )

    async def release(self) -> None:
        await cast(Awaitable[int], self.client.eval(_RELEASE, 1, self.key, self.token))

    async def heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.ttl_ms / 3000)
                if not await self.renew():
                    self.lost.set()
                    return
        except Exception:
            self.lost.set()

    async def run(self, work: Awaitable[T]) -> T:
        task = asyncio.ensure_future(work)
        lost = asyncio.create_task(self.lost.wait())
        try:
            await asyncio.wait([task, lost], return_when=asyncio.FIRST_COMPLETED)
            if self.lost.is_set():
                raise ProviderUncertainError("供应商租约失效，需查询原任务结果")
            return await task
        finally:
            task.cancel()
            lost.cancel()
            await asyncio.gather(task, lost, return_exceptions=True)


@asynccontextmanager
async def provider_semaphore(
    provider: str,
    max_concurrency: int | None = None,
    wait_timeout_s: float = 120.0,
    *,
    lease_ttl_s: float = TTL_SEMAPHORE,
) -> AsyncIterator[ProviderLease | None]:
    limit = settings.provider_max_concurrency if max_concurrency is None else max_concurrency
    if limit < 1 or lease_ttl_s < 0.1 or wait_timeout_s < 0:
        raise ValueError("并发数、租约时长或等待时长非法")
    r = get_redis()
    lease = ProviderLease(
        KEY_SEMAPHORE.format(provider=provider), uuid.uuid4().hex, int(lease_ttl_s * 1000), r
    )
    deadline = asyncio.get_running_loop().time() + wait_timeout_s
    while not await cast(
        Awaitable[int], r.eval(_ACQUIRE, 1, lease.key, lease.token, str(limit), str(lease.ttl_ms))
    ):
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            yield None
            return
        await asyncio.sleep(min(0.1, remaining))
    heartbeat = asyncio.create_task(lease.heartbeat())
    try:
        yield lease
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        try:
            await lease.release()
        except Exception:
            logger.warning("供应商租约释放失败，将自动到期 provider=%s", provider)


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
