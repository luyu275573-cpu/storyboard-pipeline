"""模型包：ORM 模型与领域枚举。

采用惰性导出（PEP 562），解决一个真实问题：
`app/models/enums.py` 是纯标准库模块（只有 StrEnum），但如果本文件急切导入
domain/tracking，就会连带拉起 app.core.db → app.core.config → pydantic_settings，
导致「只想测一段纯逻辑，却必须先装齐数据库与 Web 依赖」。

惰性后：
- `from app.models.enums import QCVerdict` 不触发任何 ORM / DB / 配置导入，
  纯逻辑单元测试可在最小依赖下运行
- `from app.models import Base, Project` 按需加载，Alembic autogenerate 仍能发现全部表

⚠️ 新增模型必须登记到 _LAZY 与 TYPE_CHECKING 两处，否则 Alembic 发现不了该表。
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import DeclarativeBase

    from app.models.domain import (
        Character,
        CharacterRef,
        PipelineRun,
        Project,
        ReviewGate,
        Scene,
        Shot,
    )
    from app.models.tracking import (
        ApiCallLog,
        BudgetLedger,
        Export,
        QCGoldenSet,
        QCReport,
        RenderAttempt,
    )

# 名称 -> (所在模块, 属性名)
_LAZY: dict[str, tuple[str, str]] = {
    "Base": ("app.core.db", "Base"),
    # domain
    "Project": ("app.models.domain", "Project"),
    "Character": ("app.models.domain", "Character"),
    "CharacterRef": ("app.models.domain", "CharacterRef"),
    "Scene": ("app.models.domain", "Scene"),
    "Shot": ("app.models.domain", "Shot"),
    "PipelineRun": ("app.models.domain", "PipelineRun"),
    "ReviewGate": ("app.models.domain", "ReviewGate"),
    # tracking
    "RenderAttempt": ("app.models.tracking", "RenderAttempt"),
    "QCReport": ("app.models.tracking", "QCReport"),
    "QCGoldenSet": ("app.models.tracking", "QCGoldenSet"),
    "ApiCallLog": ("app.models.tracking", "ApiCallLog"),
    "BudgetLedger": ("app.models.tracking", "BudgetLedger"),
    "Export": ("app.models.tracking", "Export"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        msg = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(msg)
    module_name, attr = target
    return getattr(importlib.import_module(module_name), attr)


def __dir__() -> list[str]:
    return sorted([*globals(), *_LAZY])


def load_all_models() -> None:
    """显式加载全部模型，供 Alembic 迁移环境调用。

    autogenerate 需要所有表都注册到 Base.metadata，
    惰性导入下必须主动触发一次，否则新表不会被发现。
    """
    importlib.import_module("app.models.domain")
    importlib.import_module("app.models.tracking")


__all__ = [
    "ApiCallLog",
    "Base",
    "BudgetLedger",
    "Character",
    "CharacterRef",
    "Export",
    "PipelineRun",
    "Project",
    "QCGoldenSet",
    "QCReport",
    "RenderAttempt",
    "ReviewGate",
    "Scene",
    "Shot",
    "load_all_models",
]
