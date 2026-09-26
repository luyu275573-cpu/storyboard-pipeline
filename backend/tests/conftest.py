"""隔离的 MySQL 数据库与带随机键前缀的 Redis，绝不清空开发数据。"""

import os
import uuid

import pytest


@pytest.fixture
async def mysql_sessions():
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("运行 python scripts/test_mysql.py 启用真实数据库测试")
    assert (make_url(url).database or "").startswith("sbp_test_")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def client(mysql_sessions):
    from httpx import ASGITransport, AsyncClient

    from app.api.v1.budget import get_cost_service
    from app.core.db import get_db
    from app.main import app
    from app.services.cost_service import CostService

    async def db_override():
        async with mysql_sessions.begin() as session:
            yield session

    app.dependency_overrides[get_db] = db_override
    app.dependency_overrides[get_cost_service] = lambda: CostService(mysql_sessions)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            yield api
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def redis_test(monkeypatch):
    import redis.asyncio as redis

    from app.core import redis_client

    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("需要 TEST_REDIS_URL；test_mysql.py 会配置")
    client = redis.from_url(url, decode_responses=True)
    await client.ping()
    prefix = f"sbp_test:{uuid.uuid4().hex}:"
    monkeypatch.setattr(redis_client, "_pool", client)
    monkeypatch.setattr(redis_client, "KEY_SEMAPHORE", prefix + "lease:{provider}")
    monkeypatch.setattr(redis_client, "KEY_RUN_PROGRESS", prefix + "progress:{run_id}")
    try:
        yield client
    finally:
        keys = [key async for key in client.scan_iter(match=prefix + "*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()
