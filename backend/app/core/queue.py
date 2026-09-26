"""ARQ 投递辅助。"""

from arq import create_pool
from arq.connections import RedisSettings
from redis.asyncio import Redis

from app.core.config import settings


async def enqueue_render_job(shot_id: str, n: int = 1) -> str:
    parsed = Redis.from_url(settings.redis_url)
    kwargs = parsed.connection_pool.connection_kwargs
    pool = await create_pool(
        RedisSettings(
            host=kwargs.get("host", "127.0.0.1"),
            port=int(kwargs.get("port", 6379)),
            database=int(kwargs.get("db", 0) or 0),
            password=kwargs.get("password"),
        ),
        default_queue_name=settings.arq_queue_name,
    )
    try:
        job = await pool.enqueue_job("enqueue_render", shot_id=shot_id, n=n, stage="image")
        if job is None:
            raise RuntimeError("任务已存在或未能进入队列")
        return job.job_id
    finally:
        await pool.close()
