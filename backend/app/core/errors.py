"""统一错误码与业务异常。

设计原则：
- 错误码分域前缀，便于前端与日志聚合
- 业务异常携带 HTTP 状态码与机器可读 code，统一由异常处理器转响应
- Provider 层的降级/熔断通过特定异常类型表达，上层可精确捕获
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    # 通用
    OK = "OK"
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INTERNAL = "INTERNAL"

    # 预算 / 成本域
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    BUDGET_DEGRADED = "BUDGET_DEGRADED"

    # Provider / 模型域
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_ALL_FAILED = "PROVIDER_ALL_FAILED"
    PROVIDER_NO_CREDENTIALS = "PROVIDER_NO_CREDENTIALS"

    # 质检域
    QC_PARSE_FAILED = "QC_PARSE_FAILED"
    QC_BLOCKED_COMPLIANCE = "QC_BLOCKED_COMPLIANCE"

    # 流水线域
    PIPELINE_MAX_RETRY = "PIPELINE_MAX_RETRY"
    PIPELINE_SUSPENDED = "PIPELINE_SUSPENDED"
    GATE_PENDING = "GATE_PENDING"
    IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"


class AppError(Exception):
    """业务异常基类。"""

    http_status: int = 400
    code: ErrorCode = ErrorCode.BAD_REQUEST

    def __init__(self, message: str, *, code: ErrorCode | None = None, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.detail = detail or {}


class NotFoundError(AppError):
    http_status = 404
    code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    http_status = 409
    code = ErrorCode.CONFLICT


class BudgetExceededError(AppError):
    """预算熔断：拒绝本次调用，保留已有成果。"""

    http_status = 402  # Payment Required
    code = ErrorCode.BUDGET_EXCEEDED


class ProviderError(AppError):
    """单个供应商调用失败，可降级到下一个。"""

    http_status = 502
    code = ErrorCode.PROVIDER_UNAVAILABLE


class ProviderRateLimitedError(ProviderError):
    code = ErrorCode.PROVIDER_RATE_LIMITED


class ProviderTimeoutError(ProviderError):
    code = ErrorCode.PROVIDER_TIMEOUT


class AllProvidersFailedError(AppError):
    """降级链全部失败：挂起转人工，不静默丢弃。"""

    http_status = 502
    code = ErrorCode.PROVIDER_ALL_FAILED


class QCParseError(AppError):
    """质检模型返回无法解析为结构化判定。"""

    http_status = 502
    code = ErrorCode.QC_PARSE_FAILED


class ComplianceBlockedError(AppError):
    """内容合规拦截：强制人工，禁止自动重试。"""

    http_status = 451  # Unavailable For Legal Reasons
    code = ErrorCode.QC_BLOCKED_COMPLIANCE


class MaxRetryError(AppError):
    """镜头抽卡超过上限：挂起待人工。"""

    http_status = 409
    code = ErrorCode.PIPELINE_MAX_RETRY


class GatePendingError(AppError):
    """人机关卡未放行，流水线不可继续。"""

    http_status = 423  # Locked
    code = ErrorCode.GATE_PENDING
