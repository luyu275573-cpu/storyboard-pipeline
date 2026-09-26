"""业务服务包。

约定：Controller 只做参数校验与编排，业务规则写在 Service 层。
这套分层沿用农牧项目（Controller / Service / Model），便于跨项目复用经验。
"""

from app.services.cost_service import CostService

__all__ = ["CostService"]
