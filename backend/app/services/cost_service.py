"""短事务预算预留、逐次调用记录与持久化重放。外部请求绝不占着数据库事务。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.errors import (
    AppError,
    BudgetExceededError,
    ConflictError,
    NotFoundError,
    ProviderPendingError,
    ProviderUncertainError,
)
from app.models import load_all_models
from app.models.domain import PipelineRun, Project, Scene, Shot
from app.models.tracking import ApiCallLog, BudgetLedger, ProviderRequest, RenderAttempt

if TYPE_CHECKING:
    from app.providers.base import CallContext

load_all_models()
TERMINAL = {"succeeded", "failed"}


def cents(value: int) -> int:
    if type(value) is not int or not 0 <= value <= 2_147_483_647:
        raise AppError("金额必须是非负整数分")
    return value


class CostService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession] = SessionLocal) -> None:
        self.sessions = sessions

    async def lookup(self, key: str, request_hash: str) -> ProviderRequest | None:
        async with self.sessions() as db:
            request = await db.scalar(select(ProviderRequest).where(ProviderRequest.idempotency_key == key))
            if request and request.request_hash != request_hash:
                raise ConflictError("同一操作标识的请求内容发生变化，请使用新的操作标识")
            return request

    async def get_request(self, request_id: str) -> tuple[ProviderRequest, ApiCallLog]:
        async with self.sessions() as db:
            request = await db.get(ProviderRequest, request_id)
            if request is None:
                raise NotFoundError("模型请求不存在")
            call = await db.scalar(
                select(ApiCallLog)
                .where(ApiCallLog.request_id == request.id)
                .order_by(ApiCallLog.call_no.desc())
                .limit(1)
            )
            assert call is not None
            return request, call

    async def _project(self, db: AsyncSession, project_id: str) -> Project:
        # ponytail: 本机吞吐下按项目串行化预算事务；吞吐成为瓶颈时再细化账本锁。
        project = await db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        if project is None:
            raise NotFoundError("项目不存在")
        return project

    async def _ledgers(
        self, db: AsyncSession, project: Project, kind: str, shot_id: str | None
    ) -> list[BudgetLedger]:
        targets = [("project", "total", project.budget_cents), ("kind", kind, project.budget_cents)]
        if shot_id:
            targets.append(("shot", shot_id, min(project.budget_cents, settings.budget_per_shot_cents)))
        rows = []
        for scope, key, limit in targets:
            row = await db.scalar(
                select(BudgetLedger)
                .where(
                    BudgetLedger.project_id == project.id,
                    BudgetLedger.scope == scope,
                    BudgetLedger.scope_key == key,
                )
                .with_for_update()
            )
            if row is None:
                row = BudgetLedger(
                    project_id=project.id,
                    scope=scope,
                    scope_key=key,
                    budget_cents=limit,
                    spent_cents=0,
                    reserved_cents=0,
                )
                db.add(row)
            rows.append(row)
        return rows

    async def begin_call(
        self,
        ctx: CallContext,
        *,
        key: str,
        request_hash: str,
        owner: str,
        provider: str,
        model: str,
        quoted_cents: int,
    ) -> tuple[ProviderRequest, ApiCallLog | None]:
        quoted = cents(quoted_cents)
        async with self.sessions.begin() as db:
            project = await self._project(db, ctx.project_id)
            request = await db.scalar(
                select(ProviderRequest).where(ProviderRequest.idempotency_key == key).with_for_update()
            )
            if request:
                if request.request_hash != request_hash:
                    raise ConflictError("同一操作标识的请求内容发生变化")
                if request.status in TERMINAL:
                    return request, None
                if request.owner_token != owner or request.status != "routing":
                    raise ProviderPendingError(
                        "请求尚未结束，请查询原请求", detail={"request_id": request.id}
                    )
            run = await db.scalar(select(PipelineRun).where(PipelineRun.id == ctx.run_id).with_for_update())
            if run is None or run.project_id != project.id:
                raise AppError("运行不属于该项目")
            if ctx.shot_id:
                shot_project = await db.scalar(
                    select(Scene.project_id).join(Shot).where(Shot.id == ctx.shot_id)
                )
                if shot_project != project.id:
                    raise AppError("镜头不属于该项目")
            if ctx.attempt_id:
                attempt = await db.get(RenderAttempt, ctx.attempt_id)
                if not ctx.shot_id or attempt is None or attempt.shot_id != ctx.shot_id:
                    raise AppError("抽卡记录不属于该镜头")
            disputed = await db.scalar(
                select(ProviderRequest.id)
                .where(ProviderRequest.project_id == project.id, ProviderRequest.status == "billing_disputed")
                .with_for_update()
                .limit(1)
            )
            if disputed:
                raise ProviderUncertainError(
                    "该项目有超出报价的账单，需先核实", detail={"request_id": disputed}
                )
            ledgers = await self._ledgers(db, project, ctx.kind.value, ctx.shot_id)
            for row in ledgers:
                if row.spent_cents + row.reserved_cents + quoted > row.budget_cents:
                    raise BudgetExceededError(
                        "预算可用额度不足，调用未发送",
                        detail={
                            "scope": row.scope,
                            "scope_key": row.scope_key,
                            "need_cents": quoted,
                            "available_cents": row.budget_cents - row.spent_cents - row.reserved_cents,
                        },
                    )
            for row in ledgers:
                row.reserved_cents += quoted
            if request is None:
                request = ProviderRequest(
                    project_id=project.id,
                    run_id=ctx.run_id,
                    operation_key=ctx.operation_key,
                    kind=ctx.kind.value,
                    idempotency_key=key,
                    request_hash=request_hash,
                    owner_token=owner,
                    status="calling",
                )
                db.add(request)
                await db.flush()
            request.status = "calling"
            previous = await db.scalar(
                select(func.max(ApiCallLog.call_no)).where(ApiCallLog.request_id == request.id)
            )
            number = (previous or 0) + 1
            call = ApiCallLog(
                request_id=request.id,
                call_no=number,
                project_id=project.id,
                shot_id=ctx.shot_id,
                attempt_id=ctx.attempt_id,
                kind=ctx.kind.value,
                provider=provider,
                model=model,
                success=False,
                cost_cents=0,
                status="calling",
                quoted_cents=quoted,
                reserved_cents=quoted,
                is_retry=number > 1,
                idempotency_key=hashlib.sha256(f"{request.id}:{number}".encode()).hexdigest(),
            )
            db.add(call)
            await db.flush()
            return request, call

    async def _locked_call(
        self, db: AsyncSession, call_id: int
    ) -> tuple[Project, ProviderRequest, ApiCallLog]:
        project_id = await db.scalar(select(ApiCallLog.project_id).where(ApiCallLog.id == call_id))
        if project_id is None:
            raise NotFoundError("调用记录不存在")
        project = await self._project(db, project_id)
        call = await db.scalar(select(ApiCallLog).where(ApiCallLog.id == call_id).with_for_update())
        assert call is not None and call.request_id is not None
        request = await db.scalar(
            select(ProviderRequest).where(ProviderRequest.id == call.request_id).with_for_update()
        )
        assert request is not None
        return project, request, call

    async def attach_task(self, call_id: int, task_id: str) -> None:
        """适配器收到任务号后立刻持久化，再开始轮询或下载。"""
        if not task_id.strip() or len(task_id) > 200:
            raise AppError("供应商任务号非法")
        async with self.sessions.begin() as db:
            _, _, call = await self._locked_call(db, call_id)
            if call.provider_task_id not in (None, task_id):
                raise ConflictError("调用已绑定另一个供应商任务")
            call.provider_task_id = task_id

    async def mark_unknown(self, call_id: int, error_code: str) -> None:
        async with self.sessions.begin() as db:
            _, request, call = await self._locked_call(db, call_id)
            if call.status in TERMINAL or request.status == "billing_disputed":
                return
            call.status = "unknown"
            call.error_code = error_code[:60]
            request.status = "unknown"

    async def finish(self, call_id: int, result: dict, *, allow_fallback: bool = False) -> ProviderRequest:
        actual = cents(result["cost_cents"])
        async with self.sessions.begin() as db:
            project, request, call = await self._locked_call(db, call_id)
            if call.status in TERMINAL:
                return request
            call.reported_cost_cents = actual
            call.response = result
            if actual > call.quoted_cents:
                # 不把实际账单伪装成零，也不擅自突破用户预算。冻结项目待核实。
                call.status = "unknown"
                call.error_code = "BUDGET_QUOTE_MISMATCH"
                request.status = "billing_disputed"
                return request
            if request.status == "billing_disputed":
                raise ConflictError("报价争议需人工核实，不能用后续较低报价自动覆盖")
            for row in await self._ledgers(db, project, call.kind, call.shot_id):
                row.reserved_cents -= call.reserved_cents
                row.spent_cents += actual
            run = await db.scalar(
                select(PipelineRun).where(PipelineRun.id == request.run_id).with_for_update()
            )
            assert run is not None
            run.spent_cents += actual
            call.reserved_cents = 0
            call.cost_cents = actual
            call.success = result["success"]
            call.status = "succeeded" if call.success else "failed"
            call.latency_ms = result["latency_ms"]
            call.error_code = result["error_code"]
            call.finished_at = datetime.now(UTC).replace(tzinfo=None)
            request.status = "succeeded" if call.success else ("routing" if allow_fallback else "failed")
            request.result = result
            return request

    async def finish_routing(self, key: str, owner: str) -> None:
        async with self.sessions.begin() as db:
            project_id = await db.scalar(
                select(ProviderRequest.project_id).where(ProviderRequest.idempotency_key == key)
            )
            if project_id is None:
                return
            await self._project(db, project_id)
            request = await db.scalar(
                select(ProviderRequest).where(ProviderRequest.idempotency_key == key).with_for_update()
            )
            assert request is not None
            if request.owner_token == owner and request.status == "routing":
                request.status = "failed"

    async def report_shot_cost(self, project_id: str) -> list[dict]:
        async with self.sessions() as db:
            rows = await db.execute(
                text(
                    "SELECT shot_id, COUNT(*) AS attempts, SUM(cost_cents) AS cost_cents, "
                    "SUM(reserved_cents) AS reserved_cents, SUM(success) AS ok_count "
                    "FROM api_call_logs WHERE project_id = :pid AND shot_id IS NOT NULL "
                    "GROUP BY shot_id ORDER BY cost_cents DESC"
                ),
                {"pid": project_id},
            )
            return [dict(row._mapping) for row in rows]

    async def report_provider_stats(self) -> list[dict]:
        async with self.sessions() as db:
            rows = await db.execute(
                text(
                    "SELECT provider, kind, model, COUNT(*) AS calls, "
                    "SUM(status IN ('calling', 'unknown')) AS pending_calls, "
                    "AVG(CASE WHEN status IN ('succeeded','failed') THEN success END) AS success_rate, "
                    "AVG(CASE WHEN success = 1 THEN cost_cents END) AS avg_cost_cents "
                    "FROM api_call_logs GROUP BY provider, kind, model ORDER BY calls DESC"
                )
            )
            return [dict(row._mapping) for row in rows]
