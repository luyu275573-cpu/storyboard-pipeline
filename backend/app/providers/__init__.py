"""Provider 包：模型 API 适配层。

所有模型调用必须经过 Provider，不允许业务代码直接发 HTTP 请求——
这是成本记账、限流退避、幂等防重复扣费、失败降级能生效的前提。

供应商实现文件（由 Codex 填充）：
  image_jimeng.py   即梦图像生成（火山引擎）
  image_vidu.py     Vidu 参考生图（多角色主体绑定）
  video_kling.py    可灵视频生成（降级链首位，单价低）
  video_jimeng.py   即梦视频生成（降级备选）
  vision_dashscope.py  通义千问多模态视觉（质检判定，走免费 Token 额度）
  llm_dashscope.py  通义千问文本（剧本解析、分镜生成）

每个实现只需继承 BaseProvider 并填三个方法：
  is_configured() / _call() / estimate_cost()
幂等、闸门、退避、记账全部由基类的 generate() 统一处理，子类不要重复实现。
"""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.models.enums import ProviderKind
from app.providers.base import BaseProvider, CallContext, ProviderResult, ProviderRouter
from app.services.cost_service import CostService

__all__ = [
    "BaseProvider",
    "CallContext",
    "CostService",
    "ProviderKind",
    "ProviderResult",
    "ProviderRouter",
    "build_router",
]


def build_router(cost: CostService) -> ProviderRouter:
    """组装路由降级器。

    TODO(impl): 各供应商实现完成后，在此实例化并注册：
        providers = {
            "jimeng": JimengImageProvider(cost),
            "vidu": ViduImageProvider(cost),
            "kling": KlingVideoProvider(cost),
            "dashscope_vision": DashscopeVisionProvider(cost),
            "dashscope_llm": DashscopeLLMProvider(cost),
        }
        return ProviderRouter(providers, cost)

    降级链在 .env 的 IMAGE_PROVIDER_CHAIN / VIDEO_PROVIDER_CHAIN 配置，
    顺序应基于 api_call_logs 实测的成功率与单价调整，不是拍脑袋定。

    当前返回空路由器：调用时会抛 AllProvidersFailedError（挂起转人工），
    而不是静默成功——宁可显式失败，也不要假装生成了内容。
    """
    return ProviderRouter({}, cost)
