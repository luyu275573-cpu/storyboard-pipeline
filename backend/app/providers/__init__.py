"""模型适配器注册；M2 只验收基础设施，真实供应商在价格与额度核实后接入。"""

from app.models.enums import ProviderKind
from app.providers.base import BaseProvider, CallContext, ProviderResult, ProviderRouter
from app.providers.siliconflow import (
    SiliconFlowImageProvider,
    SiliconFlowLLMProvider,
    SiliconFlowVisionProvider,
)
from app.services.cost_service import CostService

__all__ = [
    "BaseProvider",
    "CallContext",
    "CostService",
    "ProviderKind",
    "ProviderResult",
    "ProviderRouter",
    "SiliconFlowImageProvider",
    "SiliconFlowLLMProvider",
    "SiliconFlowVisionProvider",
    "build_router",
]


def build_router(cost: CostService) -> ProviderRouter:
    # 同一家供应商的图像、视频等能力分别注册，路由不跨能力降级。
    return ProviderRouter(
        [SiliconFlowImageProvider(cost), SiliconFlowVisionProvider(cost), SiliconFlowLLMProvider(cost)],
        cost,
    )
