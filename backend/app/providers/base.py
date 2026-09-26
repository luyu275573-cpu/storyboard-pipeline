"""Provider 统一入口：调用前预留、持久幂等、逐次结算、未知结果只查询。"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import (
    AllProvidersFailedError,
    AppError,
    ProviderPendingError,
    ProviderRateLimitedError,
    ProviderRejectedError,
    ProviderUncertainError,
)
from app.core.redis_client import provider_semaphore
from app.models.enums import ProviderKind
from app.models.tracking import ApiCallLog, ProviderRequest
from app.services.cost_service import TERMINAL, CostService, cents


@dataclass(frozen=True)
class CallContext:
    kind: ProviderKind
    project_id: str
    run_id: str
    operation_key: str
    shot_id: str | None = None
    attempt_id: str | None = None
    attempt_no: int = 0
    seed: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.project_id
            or not self.run_id
            or not self.operation_key.strip()
            or len(self.operation_key) > 120
        ):
            raise AppError("模型调用必须指定项目、运行和不超过 120 字的操作标识")
        if not isinstance(self.kind, ProviderKind):
            raise AppError("模型能力类型非法")

    def identity(self, payload: dict[str, Any]) -> tuple[str, str]:
        key = json.dumps(
            [self.project_id, self.run_id, self.kind.value, self.operation_key], ensure_ascii=False
        )
        body = json.dumps(
            {"context": asdict(self), "payload": payload},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(key.encode()).hexdigest(), hashlib.sha256(body.encode()).hexdigest()


@dataclass
class ProviderResult:
    success: bool
    kind: ProviderKind
    provider: str
    model: str
    asset_path: str | None = None
    asset_sha256: str | None = None
    text: str | None = None
    parsed: dict[str, Any] | None = None
    cost_cents: int = 0
    latency_ms: int = 0
    error_code: str | None = None

    def dump(self) -> dict:
        cents(self.cost_cents)
        if type(self.success) is not bool or type(self.latency_ms) is not int or self.latency_ms < 0:
            raise AppError("供应商返回了非法的状态或耗时")
        if self.error_code is not None and len(self.error_code) > 60:
            raise AppError("供应商错误码超过 60 字")
        return json.loads(json.dumps(asdict(self), ensure_ascii=False, allow_nan=False))

    @classmethod
    def restore(cls, value: dict) -> ProviderResult:
        return cls(**{**value, "kind": ProviderKind(value["kind"])})


class BaseProvider(ABC):
    name = "base"
    kind = ProviderKind.IMAGE

    def __init__(self, cost: CostService) -> None:
        self.cost = cost
        self.timeout_s = (
            settings.video_timeout_s if self.kind == ProviderKind.VIDEO else settings.provider_timeout_s
        )

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def estimate_cost(self, payload: dict[str, Any]) -> int:
        """必须是供应商保证的本次费用上界；通过 token/时长等参数限制用量。"""

    @abstractmethod
    async def _call(
        self, payload: dict[str, Any], client: httpx.AsyncClient, call: ApiCallLog
    ) -> ProviderResult:
        """仅发送一次生成请求。传供应商幂等键；收到任务号即 await cost.attach_task(call.id, task_id)。

        只有已确认未受理/未计费才抛 ProviderRejectedError，其余异常视为结果未知。
        """

    async def _query(self, client: httpx.AsyncClient, call: ApiCallLog) -> ProviderResult | None:
        """只查任务/账单，不创建生成任务；无查询能力或仍在运行时返回 None。"""
        return None

    async def generate(self, payload: dict[str, Any], ctx: CallContext) -> ProviderResult:
        return await ProviderRouter([self], self.cost).generate(ctx.kind, payload, ctx)

    @staticmethod
    def sha256_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


class ProviderRouter:
    def __init__(self, providers: Iterable[BaseProvider], cost: CostService) -> None:
        self.providers: dict[tuple[ProviderKind, str], BaseProvider] = {}
        for provider in providers:
            key = (provider.kind, provider.name)
            if key in self.providers:
                raise ValueError(f"重复的供应商能力注册：{key}")
            if provider.cost is not cost:
                raise ValueError("路由与适配器必须使用同一记账服务")
            self.providers[key] = provider
        self.cost = cost

    def _chain(self, kind: ProviderKind) -> list[BaseProvider]:
        configured = (
            settings.image_chain
            if kind == ProviderKind.IMAGE
            else (
                settings.video_chain
                if kind == ProviderKind.VIDEO
                else (settings.vision_chain if kind == ProviderKind.VISION else settings.llm_chain)
            )
        )
        names = [name for capability, name in self.providers if capability == kind]
        # 单一适配器也可直接 generate；优先按配置排序，再使用显式注册的同能力适配器。
        order = list(dict.fromkeys([*configured, *names]))
        return [self.providers[kind, name] for name in order if (kind, name) in self.providers]

    @staticmethod
    def _replay(request: ProviderRequest) -> ProviderResult:
        if request.status in TERMINAL:
            assert request.result is not None
            return ProviderResult.restore(request.result)
        error = (
            ProviderUncertainError
            if request.status in {"unknown", "billing_disputed"}
            else ProviderPendingError
        )
        raise error("请求尚未结束，请查询原请求，不能重新生成", detail={"request_id": request.id})

    @staticmethod
    def _validate_result(result: ProviderResult, call: ApiCallLog) -> dict:
        if (result.kind.value, result.provider, result.model) != (call.kind, call.provider, call.model):
            raise AppError("供应商响应的能力、来源或模型不匹配")
        return result.dump()

    async def generate(self, kind: ProviderKind, payload: dict[str, Any], ctx: CallContext) -> ProviderResult:
        payload = copy.deepcopy(payload)
        if kind != ctx.kind:
            raise AppError("调用能力与上下文不匹配")
        key, fingerprint = ctx.identity(payload)
        existing = await self.cost.lookup(key, fingerprint)
        if existing:
            return self._replay(existing)
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip() or len(model) > 80:
            raise AppError("请求必须指定不超过 80 字的模型名称")
        owner = uuid.uuid4().hex
        last: ProviderResult | None = None
        try:
            for provider in self._chain(kind):
                if not provider.is_configured():
                    continue
                quote = cents(provider.estimate_cost(copy.deepcopy(payload)))
                async with provider_semaphore(provider.name) as lease:
                    if lease is None:
                        raise ProviderRateLimitedError("供应商并发额度等待超时，本次尚未发送")
                    request, call = await self.cost.begin_call(
                        ctx,
                        key=key,
                        request_hash=fingerprint,
                        owner=owner,
                        provider=provider.name,
                        model=model,
                        quoted_cents=quote,
                    )
                    if call is None:
                        return self._replay(request)
                    started = time.perf_counter()
                    try:
                        async with httpx.AsyncClient(timeout=provider.timeout_s) as client:
                            async with asyncio.timeout(provider.timeout_s):
                                try:
                                    result = await lease.run(
                                        provider._call(copy.deepcopy(payload), client, call)
                                    )
                                except ProviderRejectedError as exc:
                                    result = ProviderResult(
                                        False, kind, provider.name, model, error_code=exc.code.value
                                    )
                        result.latency_ms = int((time.perf_counter() - started) * 1000)
                        saved = await self.cost.finish(
                            call.id, self._validate_result(result, call), allow_fallback=True
                        )
                    except asyncio.CancelledError:
                        await asyncio.shield(self.cost.mark_unknown(call.id, "CALL_CANCELLED"))
                        raise
                    except Exception as exc:
                        await self.cost.mark_unknown(call.id, "PROVIDER_RESULT_UNKNOWN")
                        raise ProviderUncertainError(
                            "调用结果未知，额度已保留，请查询原任务", detail={"request_id": request.id}
                        ) from exc
                    if saved.status == "billing_disputed":
                        raise ProviderUncertainError(
                            "供应商账单超出报价，项目已暂停新调用", detail={"request_id": request.id}
                        )
                    if saved.status == "succeeded":
                        return self._replay(saved)
                    last = result
        finally:
            # 仅收束明确失败后的降级间隙；calling/unknown 永不释放、永不重发。
            await self.cost.finish_routing(key, owner)
        if last is not None:
            return last
        raise AllProvidersFailedError(f"没有已配置的 {kind.value} 供应商")

    async def reconcile(self, request_id: str) -> ProviderResult | None:
        """用于 Worker 恢复/对账。None 表示未决，不改变预留、不进入降级链。"""
        request, call = await self.cost.get_request(request_id)
        if request.status in TERMINAL:
            return self._replay(request)
        if request.status == "routing":
            await self.cost.finish_routing(request.idempotency_key, request.owner_token)
            request, _ = await self.cost.get_request(request_id)
            return self._replay(request)
        if request.status == "billing_disputed":
            raise ProviderUncertainError("供应商账单存在报价争议，需人工核实")
        provider = self.providers.get((ProviderKind(call.kind), call.provider))
        if provider is None or not provider.is_configured():
            raise AllProvidersFailedError("原供应商不可查询，保留额度等待恢复")
        try:
            async with httpx.AsyncClient(timeout=provider.timeout_s) as client:
                async with asyncio.timeout(provider.timeout_s):
                    result = await provider._query(client, call)
            if result is None:
                await self.cost.mark_unknown(call.id, "PROVIDER_PENDING")
                return None
            saved = await self.cost.finish(call.id, self._validate_result(result, call))
        except Exception as exc:
            await self.cost.mark_unknown(call.id, "RECONCILIATION_PENDING")
            raise ProviderUncertainError("暂时无法确认账单，额度继续保留") from exc
        return self._replay(saved)
