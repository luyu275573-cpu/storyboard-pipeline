"""Alembic 迁移环境（异步引擎版）。

关键点：
1. 连接串从 .env 的 DATABASE_URL 读取，不写进 alembic.ini，避免凭据入库
2. 必须用 async_engine 跑迁移——项目全程异步驱动，混用同步引擎会报事件循环错误
3. target_metadata 指向 app.models.Base.metadata，
   所以新增模型必须在 app/models/__init__.py 里导入，否则 autogenerate 发现不了
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import settings

# 惰性模型包：必须显式加载，否则 autogenerate 发现不了表
from app.models import Base, load_all_models

load_all_models()

config = context.config

# 注入连接串（覆盖 alembic.ini 里的空值）
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。用于 CI 里做迁移检查门禁。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,  # 检测字段类型变更，不只检测增删表
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # MySQL 批量 ALTER 有限制，关闭 batch 模式以避免生成不可执行的语句
        render_as_batch=False,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：用异步引擎跑迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
