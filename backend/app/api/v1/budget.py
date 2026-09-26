"""预算与成本路由：三级预算查询、熔断状态、成本报表。

这里出的数字就是简历量化指标的数据源，必须第一周打通。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.response import ok
from app.models.tracking import ApiCallLog, BudgetLedger, ProviderRequest
from app.services.catalog import project_or_404
from app.services.cost_service import CostService

router = APIRouter()


def get_cost_service() -> CostService:
    return CostService()


@router.get("/config", summary="当前预算配置")
async def budget_config() -> dict[str, Any]:
    """只读返回生效的预算配置，便于部署后核对是否忘了改回真实值。

    已实现：这是纯配置读取，无副作用。
    """
    return {
        "budget_total_yuan": round(settings.budget_total_cents / 100, 2),
        "budget_per_shot_yuan": round(settings.budget_per_shot_cents / 100, 2),
        "warn_ratio": settings.budget_warn_ratio,
        "degrade_ratio": settings.budget_degrade_ratio,
        "shot_max_retry": settings.shot_max_retry,
        "render_n_per_shot": settings.render_n_per_shot,
        "image_chain": settings.image_chain,
        "video_chain": settings.video_chain,
        "provider_max_concurrency": settings.provider_max_concurrency,
        # 提醒：测试时调小预算后上线忘记恢复，会导致几分钟内触发熔断停掉所有生成
        "warning": "上线前确认 budget_total 为真实值，且 provider_max_concurrency 不超过供应商免费档配额",
    }


@router.get("/{project_id}", summary="预算使用情况")
async def get_budget(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    project = await project_or_404(db, project_id)
    rows = list(
        await db.scalars(
            select(BudgetLedger)
            .where(BudgetLedger.project_id == project_id)
            .order_by(BudgetLedger.scope, BudgetLedger.scope_key)
        )
    )
    spent = next((row.spent_cents for row in rows if row.scope == "project" and row.scope_key == "total"), 0)
    reserved = next(
        (row.reserved_cents for row in rows if row.scope == "project" and row.scope_key == "total"), 0
    )
    disputed = await db.scalar(
        select(ProviderRequest.id)
        .where(ProviderRequest.project_id == project_id, ProviderRequest.status == "billing_disputed")
        .limit(1)
    )
    return ok(
        {
            "project_id": project.id,
            "budget_cents": project.budget_cents,
            "spent_cents": spent,
            "reserved_cents": reserved,
            "remaining_cents": project.budget_cents - spent - reserved,
            "ratio": (spent + reserved) / project.budget_cents if project.budget_cents else 0,
            "billing_disputed": bool(disputed),
            "warn_ratio": settings.budget_warn_ratio,
            "degrade_ratio": settings.budget_degrade_ratio,
            "ledgers": [
                {
                    "scope": r.scope,
                    "scope_key": r.scope_key,
                    "budget_cents": r.budget_cents,
                    "spent_cents": r.spent_cents,
                    "reserved_cents": r.reserved_cents,
                }
                for r in rows
            ],
        }
    )


@router.get("/{project_id}/shots", summary="单镜头成本明细")
async def shot_costs(
    project_id: str, db: AsyncSession = Depends(get_db), cost: CostService = Depends(get_cost_service)
) -> dict[str, Any]:
    await project_or_404(db, project_id)
    return ok(await cost.report_shot_cost(project_id))


@router.get("/providers/stats", summary="各供应商成功率与单价对比")
async def provider_stats(cost: CostService = Depends(get_cost_service)) -> dict[str, Any]:
    return ok(await cost.report_provider_stats())


@router.get("/{project_id}/qc-savings", summary="质检拦截带来的成本节省")
async def qc_savings(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 调 CostService.report_qc_savings(project_id)。

    逻辑：质检在图像层拦住一张废片，就等于省下它流到视频层的约 1.4 元。
    这是"质检战场放图像层"这个架构决策的直接经济价值，能算出具体金额。
    简历上"累计拦截 N 次无效生成，节省 M 元"就是从这里来的。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/{project_id}/report", summary="完整成本与质量报表")
async def full_report(project_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """TODO(impl): 汇总为 CostReport。

    必须包含：
      - total_spent_cents / total_calls / success_calls
      - shot_cost（单镜头明细）
      - provider_stats（供应商对比）
      - qc_savings（拦截节省）
      - first_pass_rate（一次过合格率，调 /qc/first-pass-rate 的逻辑）

    这个接口的输出直接抄进简历，所以口径要一次定清楚：
    花费单位统一用「分」存储、展示时转元并保留两位，避免浮点误差累积。
    """
    raise NotImplementedError("TODO(impl): 由 Codex 实现")


@router.get("/{project_id}/calls", summary="原始调用日志")
async def call_logs(
    project_id: str,
    kind: str | None = None,
    provider: str | None = None,
    success: bool | None = None,
    page: int = Query(default=1, ge=1, le=10000),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    await project_or_404(db, project_id)
    query = select(ApiCallLog).where(ApiCallLog.project_id == project_id)
    if kind is not None:
        query = query.where(ApiCallLog.kind == kind)
    if provider is not None:
        query = query.where(ApiCallLog.provider == provider)
    if success is not None:
        query = query.where(ApiCallLog.success == success, ApiCallLog.status.in_(["succeeded", "failed"]))
    rows = await db.scalars(query.order_by(ApiCallLog.id.desc()).offset((page - 1) * limit).limit(limit + 1))
    items = list(rows)
    fields = (
        "id",
        "request_id",
        "call_no",
        "kind",
        "provider",
        "model",
        "status",
        "cost_cents",
        "reserved_cents",
        "quoted_cents",
        "reported_cost_cents",
        "error_code",
        "latency_ms",
        "is_retry",
    )
    return ok(
        {
            "items": [{key: getattr(row, key) for key in fields} for row in items[:limit]],
            "page": page,
            "has_more": len(items) > limit,
        }
    )
