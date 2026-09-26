"""Provider 基类：统一抽象，屏蔽供应商差异。

所有模型调用必须经过 Provider，不允许业务代码直接发 HTTP 请求——
这是成本记账、限流退避、幂等防重复扣费、失败降级能够生效的前提。

每个 Provider 必须实现四件事：
1. 幂等键：(shot_id, attempt_no, model, seed) → 防重试重复扣费
2. 调用记账：无论成败都写 api_call_logs
3. 限流退避：指数退避 + 供应商级并发闸门（云 API 免费档并发普遍为 1-2）
4. 降级链：首选 → 备选 → 挂起待人工，不静默失败
"""

from __future__ import annotations

import hashlib
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.core.errors import (
    AllProvidersFailedError,
    ProviderError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
)
from app.core.redis_client import acquire_idempotency, provider_semaphore, release_idempotency
from app.models.enums import ProviderKind
from app.services.cost_service import CostService

logger = logging.getLogger(__name__)


@dataclass
class ProviderResult:
    """统一的 Provider 返回结构。"""

    success: bool
    kind: ProviderKind
    provider: str
    model: str
    # 图像/视频：产物本地路径；LLM/视觉：文本或结构化 JSON
    asset_path: str | None = None
    asset_sha256: str | None = None
    text: str | None = None
    parsed: dict[str, Any] | None = None
    cost_cents: int = 0
    latency_ms: int = 0
    error_code: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallContext:
    """一次调用的上下文，用于记账与幂等。"""

    kind: ProviderKind
    project_id: str | None = None
    shot_id: str | None = None
    attempt_id: str | None = None
    attempt_no: int = 0
    seed: str | None = None
    estimated_cents: int = 0
    is_retry: bool = False


class BaseProvider(ABC):
    """所有供应商适配器的基类。"""

    name: str = "base"
    kind: ProviderKind = ProviderKind.IMAGE

    def __init__(self, cost: CostService) -> None:
        self.cost = cost
        self.timeout_s = settings.provider_timeout_s
        if self.kind is ProviderKind.VIDEO:
            self.timeout_s = settings.video_timeout_s

    # ---------- 子类必须实现 ----------

    @abstractmethod
    def is_configured(self) -> bool:
        """凭据是否已配置。未配置直接跳过，不浪费一次调用。"""

    @abstractmethod
    async def _call(self, payload: dict[str, Any], client: httpx.AsyncClient) -> ProviderResult:
        """实际的 HTTP 调用与响应解析。子类只需关心这一层。"""

    @abstractmethod
    def estimate_cost(self, payload: dict[str, Any]) -> int:
        """预估本次花费（分），用于预算预检。"""

    # ---------- 统一入口 ----------

    async def generate(self, payload: dict[str, Any], ctx: CallContext) -> ProviderResult:
        """统一入口：幂等 → 预检预算 → 闸门 → 退避重试 → 记账。

        业务代码只调这个方法，不直接调 _call。
        """
        if not self.is_configured():
            raise ProviderError(
                f"供应商 {self.name} 未配置凭据",
                detail={"provider": self.name},
            )

        # 1. 幂等键：防 Worker 崩溃重启导致重复扣费
        idem_key = self.build_idempotency_key(payload, ctx)
        first_try = await acquire_idempotency(idem_key)
        if not first_try:
            logger.info("幂等命中，跳过重复调用 key=%s", idem_key)
            return ProviderResult(
                success=False,
                kind=self.kind,
                provider=self.name,
                model=payload.get("model", ""),
                error_code="IDEMPOTENT_REPLAY",
            )

        estimated = ctx.estimated_cents or self.estimate_cost(payload)

        try:
            # 2. 预算预检
            if ctx.project_id:
                await self.cost.precheck(ctx.project_id, estimated)

            # 3. 供应商并发闸门 + 指数退避重试
            async with provider_semaphore(self.name) as acquired:
                if not acquired:
                    raise ProviderRateLimitedError(
                        f"供应商 {self.name} 并发闸门等待超时",
                        detail={"provider": self.name},
                    )

                result = await self._retry_call(payload, ctx)

            # 4. 记账（成功）
            if ctx.project_id and result.success:
                charged = await self.cost.charge(
                    ctx.project_id,
                    result.cost_cents,
                    shot_id=ctx.shot_id,
                    kind=self.kind.value,
                )
                if not charged:
                    # 扣费失败（预算在并发中被其他任务耗尽）：丢弃结果，不超支
                    logger.warning("扣费失败，丢弃本次结果 provider=%s", self.name)
                    result.success = False
                    result.error_code = "BUDGET_EXCEEDED"

            await self._log(ctx, result)
            return result

        except (ProviderError, ProviderTimeoutError, ProviderRateLimitedError) as exc:
            # 失败也记账
            await self._log_failure(ctx, exc)
            # 释放幂等键，允许上层降级后重试
            await release_idempotency(idem_key)
            raise
        except Exception as exc:
            await self._log_failure(ctx, exc)
            await release_idempotency(idem_key)
            raise ProviderError(f"供应商 {self.name} 调用异常: {exc}") from exc

    async def _retry_call(self, payload: dict[str, Any], ctx: CallContext) -> ProviderResult:
        """指数退避重试。只对限流与超时重试，业务错误不重试。"""
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=1, min=1, max=settings.provider_max_backoff_s),
                retry=retry_if_exception_type((ProviderRateLimitedError, ProviderTimeoutError)),
                reraise=True,
            ):
                with attempt:
                    ctx.is_retry = attempt.retry_state.attempt_number > 1
                    return await self._call(payload, client)
        # AsyncRetrying 正常不会走到这里
        raise ProviderError(f"供应商 {self.name} 重试耗尽")  # pragma: no cover

    # ---------- 幂等键 ----------

    def build_idempotency_key(self, payload: dict[str, Any], ctx: CallContext) -> str:
        """sha256(shot|attempt_no|model|seed)。

        加唯一索引后，即使 Worker 崩溃重启也不会对同一次抽卡重复调用 API。
        """
        model = payload.get("model", "")
        raw = f"{ctx.shot_id}|{ctx.attempt_no}|{model}|{ctx.seed or ''}|{self.kind.value}"
        return hashlib.sha256(raw.encode()).hexdigest()

    # ---------- 记账 ----------

    async def _log(self, ctx: CallContext, result: ProviderResult) -> None:
        await self.cost.log_call(
            kind=self.kind.value,
            provider=self.name,
            model=result.model,
            success=result.success,
            cost_cents=result.cost_cents,
            latency_ms=result.latency_ms,
            error_code=result.error_code,
            is_retry=getattr(ctx, "is_retry", False),
            project_id=ctx.project_id,
            shot_id=ctx.shot_id,
            attempt_id=ctx.attempt_id,
        )

    async def _log_failure(self, ctx: CallContext, exc: Exception) -> None:
        error_code = getattr(exc, "code", None)
        error_code = error_code.value if hasattr(error_code, "value") else (error_code or "PROVIDER_ERROR")
        await self.cost.log_call(
            kind=self.kind.value,
            provider=self.name,
            model="",
            success=False,
            cost_cents=0,
            error_code=str(error_code),
            is_retry=getattr(ctx, "is_retry", False),
            project_id=ctx.project_id,
            shot_id=ctx.shot_id,
            attempt_id=ctx.attempt_id,
        )

    # ---------- 工具 ----------

    @staticmethod
    def sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def now_ms(start: float) -> int:
        return int((time.perf_counter() - start) * 1000)


class ProviderRouter:
    """按镜头类型与供应商健康度选路，失败自动降级。

    路由策略应基于 api_call_logs 实测的各供应商成功率与单价制定，
    而不是拍脑袋。降级链在 .env 的 IMAGE_PROVIDER_CHAIN / VIDEO_PROVIDER_CHAIN 配置。
    """

    def __init__(self, providers: dict[str, BaseProvider], cost: CostService) -> None:
        self.providers = providers
        self.cost = cost

    def _chain(self, kind: ProviderKind) -> list[str]:
        if kind is ProviderKind.IMAGE:
            return settings.image_chain
        if kind is ProviderKind.VIDEO:
            return settings.video_chain
        return list(self.providers)

    async def generate(self, kind: ProviderKind, payload: dict[str, Any], ctx: CallContext) -> ProviderResult:
        """按降级链依次尝试；全部失败则抛 AllProvidersFailedError（挂起转人工，不静默丢弃）。"""
        ctx.kind = kind
        names = [n for n in self._chain(kind) if n in self.providers]
        if not names:
            raise AllProvidersFailedError(f"没有可用的 {kind.value} 供应商")

        last_error: Exception | None = None
        for name in names:
            provider = self.providers[name]
            if not provider.is_configured():
                logger.info("供应商 %s 未配置，跳过", name)
                continue
            try:
                result = await provider.generate(payload, ctx)
                if result.success:
                    return result
                last_error = ProviderError(f"{name} 返回失败: {result.error_code}")
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("供应商 %s 调用失败，尝试降级: %s", name, exc)
                continue

        raise AllProvidersFailedError(
            f"所有 {kind.value} 供应商均失败",
            detail={"chain": names, "last_error": str(last_error)},
        )
