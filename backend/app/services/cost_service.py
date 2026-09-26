"""成本记账与预算熔断服务。

两条铁律：
1. 花费累加用数据库原子条件更新，不是先查后写（并发下会超支）
2. 失败的调用也要记账（success=0），因为失败同样消耗限流配额，
   且供应商成功率对比是路由策略的依据

这里是简历上所有量化指标的唯一数据来源，必须第一周打通。
"""

from __future__ import annotations

import logging
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import BudgetExceededError
from app.core.redis_client import KEY_BUDGET_HOT, get_redis
from app.models.enums import BudgetScope

logger = logging.getLogger(__name__)


class CostService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ==================== 预算检查 ====================

    async def precheck(self, project_id: str, estimated_cents: int) -> None:
        """调用前预检。不足则抛 BudgetExceededError，由上层降级或挂起。

        先用 Redis 热计数快速拒绝（省一次 DB 往返），DB 是最终账本。
        """
        if estimated_cents <= 0:
            return

        r = get_redis()
        hot_key = KEY_BUDGET_HOT.format(project_id=project_id)
        hot = await r.get(hot_key)
        if hot is not None and int(hot) + estimated_cents > settings.budget_total_cents:
            await self._emit_budget_event(project_id, int(hot), estimated_cents)
            msg = f"预算不足：已用 {int(hot) / 100:.2f} 元，本次需 {estimated_cents / 100:.2f} 元"
            raise BudgetExceededError(msg, detail={"spent_cents": int(hot), "need_cents": estimated_cents})

        spent = await self._get_project_spent(project_id)
        if spent + estimated_cents > settings.budget_total_cents:
            await self._emit_budget_event(project_id, spent, estimated_cents)
            msg = f"预算不足：已用 {spent / 100:.2f} 元，本次需 {estimated_cents / 100:.2f} 元"
            raise BudgetExceededError(msg, detail={"spent_cents": spent, "need_cents": estimated_cents})

        # 预警与降级提示
        ratio = (spent + estimated_cents) / max(settings.budget_total_cents, 1)
        if ratio >= settings.budget_degrade_ratio:
            logger.warning("预算达 %.0f%%，应降级到最低成本策略 project=%s", ratio * 100, project_id)
        elif ratio >= settings.budget_warn_ratio:
            logger.warning("预算达 %.0f%% project=%s", ratio * 100, project_id)

    async def _get_project_spent(self, project_id: str) -> int:
        row = await self.db.execute(
            text(
                "SELECT COALESCE(SUM(spent_cents), 0) FROM budget_ledger "
                "WHERE project_id = :pid AND scope = :scope AND scope_key = 'total'"
            ),
            {"pid": project_id, "scope": BudgetScope.PROJECT.value},
        )
        return int(row.scalar() or 0)

    # ==================== 花费记账 ====================

    async def charge(
        self,
        project_id: str,
        cost_cents: int,
        *,
        scope_key: str = "total",
        shot_id: str | None = None,
        kind: str | None = None,
    ) -> bool:
        """原子扣费。返回 True 表示扣费成功，False 表示预算不足（调用方须放弃本次结果）。

        关键：靠 SQL 条件 `spent + cost <= budget` 保证并发下不超支，
        而不是先 SELECT 再 UPDATE——后者两个 Worker 可能同时读到"还有余额"。
        """
        if cost_cents <= 0:
            return True

        targets: list[tuple[str, str, int]] = [
            (BudgetScope.PROJECT.value, "total", settings.budget_total_cents)
        ]
        if shot_id:
            targets.append((BudgetScope.SHOT.value, shot_id, settings.budget_per_shot_cents))
        if kind:
            targets.append((BudgetScope.KIND.value, kind, settings.budget_total_cents))

        # 任一层预算不足即拒绝，不部分扣费
        for scope, key, budget in targets:
            await self._ensure_ledger_row(project_id, scope, key, budget)
            result = await self.db.execute(
                text(
                    "UPDATE budget_ledger SET spent_cents = spent_cents + :cost "
                    "WHERE project_id = :pid AND scope = :scope AND scope_key = :key "
                    "AND spent_cents + :cost <= budget_cents"
                ),
                {"cost": cost_cents, "pid": project_id, "scope": scope, "key": key},
            )
            if cast(CursorResult, result).rowcount == 0:
                logger.warning(
                    "预算熔断 scope=%s key=%s cost=%s project=%s", scope, key, cost_cents, project_id
                )
                return False

        # 同步 Redis 热计数（允许短暂不一致，DB 为准）
        try:
            r = get_redis()
            await r.incrby(KEY_BUDGET_HOT.format(project_id=project_id), cost_cents)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis 热计数更新失败（不影响账本）: %s", exc)

        return True

    async def _ensure_ledger_row(
        self, project_id: str, scope: str, scope_key: str, budget_cents: int
    ) -> None:
        await self.db.execute(
            text(
                "INSERT IGNORE INTO budget_ledger "
                "(id, project_id, scope, scope_key, budget_cents, spent_cents) "
                "VALUES (UUID(), :pid, :scope, :key, :budget, 0)"
            ),
            {"pid": project_id, "scope": scope, "key": scope_key, "budget": budget_cents},
        )

    # ==================== 调用日志 ====================

    async def log_call(
        self,
        *,
        kind: str,
        provider: str,
        model: str,
        success: bool,
        cost_cents: int = 0,
        latency_ms: int | None = None,
        error_code: str | None = None,
        is_retry: bool = False,
        project_id: str | None = None,
        shot_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        """逐次调用记账。失败的调用也要记。"""
        await self.db.execute(
            text(
                "INSERT INTO api_call_logs "
                "(project_id, shot_id, attempt_id, kind, provider, model, success, "
                " cost_cents, latency_ms, error_code, is_retry) "
                "VALUES (:pid, :sid, :aid, :kind, :provider, :model, :success, "
                " :cost, :latency, :err, :retry)"
            ),
            {
                "pid": project_id,
                "sid": shot_id,
                "aid": attempt_id,
                "kind": kind,
                "provider": provider,
                "model": model,
                "success": success,
                "cost": cost_cents,
                "latency": latency_ms,
                "err": error_code,
                "retry": is_retry,
            },
        )

    # ==================== 报表（简历指标数据源）====================

    async def report_shot_cost(self, project_id: str) -> list[dict]:
        """单镜头成本与抽卡次数。"""
        rows = await self.db.execute(
            text(
                "SELECT shot_id, COUNT(*) AS attempts, SUM(cost_cents) AS cost_cents, "
                "SUM(success) AS ok_count "
                "FROM api_call_logs WHERE project_id = :pid AND shot_id IS NOT NULL "
                "GROUP BY shot_id ORDER BY cost_cents DESC"
            ),
            {"pid": project_id},
        )
        return [dict(r._mapping) for r in rows]  # noqa: SLF001

    async def report_provider_stats(self) -> list[dict]:
        """各供应商成功率与单价对比——路由策略的数据依据。"""
        rows = await self.db.execute(
            text(
                "SELECT provider, model, COUNT(*) AS calls, "
                "AVG(success) AS success_rate, "
                "AVG(CASE WHEN success = 1 THEN cost_cents END) AS avg_cost_cents, "
                "AVG(latency_ms) AS avg_latency_ms "
                "FROM api_call_logs GROUP BY provider, model ORDER BY calls DESC"
            )
        )
        return [dict(r._mapping) for r in rows]  # noqa: SLF001

    async def report_qc_savings(self, project_id: str, video_unit_cents: int = 140) -> dict:
        """质检拦截的无效重抽 = 省下的钱。

        逻辑：质检在图像层拦住一张废片，就等于省下它流到视频层的成本。
        这是"质检放图像层"这个架构决策的直接经济价值。
        """
        row = await self.db.execute(
            text(
                "SELECT COUNT(*) AS blocked FROM qc_reports q "
                "JOIN render_attempts a ON a.id = q.attempt_id "
                "JOIN shots s ON s.id = a.shot_id "
                "JOIN scenes sc ON sc.id = s.scene_id "
                "WHERE sc.project_id = :pid AND q.verdict IN ('reject', 'blocked')"
            ),
            {"pid": project_id},
        )
        blocked = int(row.scalar() or 0)
        return {
            "blocked_attempts": blocked,
            "saved_cents": blocked * video_unit_cents,
            "video_unit_cents": video_unit_cents,
        }

    # ==================== 内部 ====================

    async def _emit_budget_event(self, project_id: str, spent: int, need: int) -> None:
        """预算事件推给前端（SSE），不要等到 100% 才发现。"""
        try:
            from app.core.redis_client import publish_progress

            await publish_progress(
                project_id,
                "budget",
                {
                    "spent_cents": spent,
                    "need_cents": need,
                    "budget_cents": settings.budget_total_cents,
                    "ratio": round(spent / max(settings.budget_total_cents, 1), 3),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("预算事件推送失败: %s", exc)
