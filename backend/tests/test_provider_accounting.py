"""真实 MySQL/Redis + 假供应商：测试钱、崩溃窗口和远端结果不确定的行为。"""

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from app.core.errors import (
    AppError,
    BudgetExceededError,
    ConflictError,
    ProviderPendingError,
    ProviderRejectedError,
    ProviderUncertainError,
)
from app.core.redis_client import ProviderLease, provider_semaphore
from app.models.domain import PipelineRun, Scene, Shot
from app.models.enums import ProviderKind
from app.models.tracking import ApiCallLog, BudgetLedger
from app.providers.base import BaseProvider, CallContext, ProviderResult, ProviderRouter
from app.services.cost_service import CostService

pytestmark = pytest.mark.integration
PAYLOAD = {"model": "fake-v1", "prompt": "固定角色的一个镜头"}


async def context(client, budget=100, operation="frame:1"):
    project = (
        await client.post("/api/v1/projects", json={"title": "M2 测试", "budget_cents": budget})
    ).json()["data"]
    run = (await client.get(f"/api/v1/projects/{project['id']}/runs")).json()["data"][0]
    return CallContext(ProviderKind.IMAGE, project["id"], run["id"], operation)


class FakeProvider(BaseProvider):
    def __init__(self, cost, name="fake", *, kind=ProviderKind.IMAGE, quote=30, actual=20, mode="ok"):
        self.name, self.kind = name, kind
        super().__init__(cost)
        self.quote, self.actual, self.mode = quote, actual, mode
        self.calls = 0
        self.entered = asyncio.Event()
        self.continue_call = asyncio.Event()
        self.query_result = None

    def is_configured(self):
        return True

    def estimate_cost(self, payload):
        return self.quote

    def result(self, success=True):
        return ProviderResult(
            success,
            self.kind,
            self.name,
            PAYLOAD["model"],
            text="已生成",
            cost_cents=self.actual,
            error_code=None if success else "REMOTE_FAILED",
        )

    async def _call(self, payload, client, call):
        self.calls += 1
        # 持久化任务号后模拟：供应商已受理，本地随后断网/退出。
        await self.cost.attach_task(call.id, f"remote-{call.id}")
        self.entered.set()
        if self.mode == "timeout":
            raise TimeoutError("模拟响应丢失")
        if self.mode == "reject":
            raise ProviderRejectedError("模拟明确未受理")
        if self.mode == "wait":
            await self.continue_call.wait()
        return self.result(success=self.mode != "failed")

    async def _query(self, client, call):
        assert call.provider_task_id or self.calls == 0
        return self.query_result


async def budget(client, ctx):
    response = await client.get(f"/api/v1/budget/{ctx.project_id}")
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def request_for(cost, ctx):
    return await cost.lookup(*ctx.identity(PAYLOAD))


async def reserve(cost, ctx, quote=30, owner="owner"):
    key, digest = ctx.identity(PAYLOAD)
    return await cost.begin_call(
        ctx,
        key=key,
        request_hash=digest,
        owner=owner,
        provider="fake",
        model=PAYLOAD["model"],
        quoted_cents=quote,
    )


async def test_multilevel_reservation_is_atomic_under_contention(client, mysql_sessions):
    ctx = await context(client)
    cost = CostService(mysql_sessions)
    async with mysql_sessions.begin() as db:
        scene = Scene(project_id=ctx.project_id, seq=1)
        db.add(scene)
        await db.flush()
        shot = Shot(scene_id=scene.id, seq=1)
        db.add(shot)
        await db.flush()
        ctx = replace(ctx, shot_id=shot.id)
        db.add(
            BudgetLedger(
                project_id=ctx.project_id,
                scope="shot",
                scope_key=shot.id,
                budget_cents=65,
                spent_cents=0,
                reserved_cents=0,
            )
        )
    results = await asyncio.gather(
        *(reserve(cost, replace(ctx, operation_key=f"frame:{i}")) for i in range(8)), return_exceptions=True
    )
    accepted = [item for item in results if isinstance(item, tuple)]
    assert len(accepted) == 2
    assert all(isinstance(item, (tuple, BudgetExceededError)) for item in results)
    current = await budget(client, ctx)
    assert current["reserved_cents"] == 60 and current["remaining_cents"] == 40
    assert {
        r["reserved_cents"] for r in current["ledgers"] if r["scope_key"] in ("total", "image", ctx.shot_id)
    } == {60}
    for _, call in accepted:
        await cost.finish(call.id, FakeProvider(cost).result().dump())
    current = await budget(client, ctx)
    assert (current["spent_cents"], current["reserved_cents"], current["remaining_cents"]) == (40, 0, 60)
    async with mysql_sessions() as db:
        assert (await db.get(PipelineRun, ctx.run_id)).spent_cents == 40


async def test_kind_limit_rolls_back_every_ledger_and_request(client, mysql_sessions):
    ctx = await context(client)
    async with mysql_sessions.begin() as db:
        row = await db.scalar(
            select(BudgetLedger).where(
                BudgetLedger.project_id == ctx.project_id, BudgetLedger.scope_key == "image"
            )
        )
        row.budget_cents = 10
    cost = CostService(mysql_sessions)
    with pytest.raises(BudgetExceededError):
        await reserve(cost, ctx)
    assert await request_for(cost, ctx) is None
    assert all(
        row["reserved_cents"] == row["spent_cents"] == 0 for row in (await budget(client, ctx))["ledgers"]
    )
    async with mysql_sessions() as db:
        assert not list(await db.scalars(select(ApiCallLog).where(ApiCallLog.project_id == ctx.project_id)))


async def test_replay_returns_fallback_result_without_another_charge(client, mysql_sessions, redis_test):
    ctx = await context(client)
    cost = CostService(mysql_sessions)
    first = FakeProvider(cost, "first", actual=7, mode="failed")
    second = FakeProvider(cost, "second", actual=11)
    router = ProviderRouter([first, second], cost)
    result = await router.generate(ctx.kind, PAYLOAD, ctx)
    assert result.provider == "second" and result.success
    restored = await ProviderRouter([second, first], cost).generate(ctx.kind, dict(PAYLOAD), ctx)
    assert restored == result and (first.calls, second.calls) == (1, 1)
    with pytest.raises(ConflictError):
        await router.generate(ctx.kind, {**PAYLOAD, "prompt": "不同内容"}, ctx)
    current = await budget(client, ctx)
    assert (current["spent_cents"], current["reserved_cents"]) == (18, 0)
    rows = (await client.get(f"/api/v1/budget/{ctx.project_id}/calls?limit=1")).json()["data"]
    assert rows["has_more"] and rows["items"][0]["call_no"] == 2
    assert rows["items"][0]["is_retry"]
    failed = (await client.get(f"/api/v1/budget/{ctx.project_id}/calls?success=false")).json()["data"][
        "items"
    ]
    assert len(failed) == 1 and failed[0]["cost_cents"] == 7


async def test_definite_rejection_releases_before_fallback(client, mysql_sessions, redis_test):
    ctx = await context(client, budget=30)
    cost = CostService(mysql_sessions)
    first, second = FakeProvider(cost, "first", mode="reject"), FakeProvider(cost, "second")
    tasks_before = asyncio.all_tasks()
    result = await ProviderRouter([first, second], cost).generate(ctx.kind, PAYLOAD, ctx)
    await asyncio.sleep(0)
    assert asyncio.all_tasks() <= tasks_before
    assert result.success and first.calls == second.calls == 1
    assert (await budget(client, ctx))["spent_cents"] == 20


async def test_timeout_holds_budget_and_concurrent_reconciliation_settles_once(
    client, mysql_sessions, redis_test
):
    ctx = await context(client)
    cost = CostService(mysql_sessions)
    first, second = FakeProvider(cost, "first", mode="timeout"), FakeProvider(cost, "second")
    router = ProviderRouter([first, second], cost)
    with pytest.raises(ProviderUncertainError):
        await router.generate(ctx.kind, PAYLOAD, ctx)
    request = await request_for(cost, ctx)
    assert request.status == "unknown" and second.calls == 0
    with pytest.raises(ProviderUncertainError):
        await router.generate(ctx.kind, PAYLOAD, ctx)
    assert await router.reconcile(request.id) is None
    current = await budget(client, ctx)
    assert (current["spent_cents"], current["reserved_cents"], current["remaining_cents"]) == (0, 30, 70)
    assert not (await client.get(f"/api/v1/budget/{ctx.project_id}/calls?success=false")).json()["data"][
        "items"
    ]
    first.query_result = first.result()
    results = await asyncio.gather(router.reconcile(request.id), router.reconcile(request.id))
    assert results[0] == results[1] == first.query_result
    replay = await router.generate(ctx.kind, PAYLOAD, ctx)
    assert replay == first.query_result and first.calls == 1
    current = await budget(client, ctx)
    assert (current["spent_cents"], current["reserved_cents"]) == (20, 0)
    async with mysql_sessions() as db:
        assert (await db.get(PipelineRun, ctx.run_id)).spent_cents == 20


async def test_committed_request_survives_worker_loss_and_cannot_be_resent(
    client, mysql_sessions, redis_test
):
    ctx = await context(client)
    cost = CostService(mysql_sessions)
    request, _ = await reserve(cost, ctx)
    recovered_cost = CostService(mysql_sessions)
    provider = FakeProvider(recovered_cost, actual=0)
    router = ProviderRouter([provider], recovered_cost)
    with pytest.raises(ProviderPendingError):
        await router.generate(ctx.kind, PAYLOAD, ctx)
    assert await router.reconcile(request.id) is None and provider.calls == 0
    assert (await budget(client, ctx))["reserved_cents"] == 30
    provider.query_result = provider.result(success=False)
    result = await router.reconcile(request.id)
    assert not result.success
    assert (await budget(client, ctx))["reserved_cents"] == 0
    assert await router.generate(ctx.kind, PAYLOAD, ctx) == result and provider.calls == 0


async def test_duplicate_request_during_call_does_not_dispatch_twice(client, mysql_sessions, redis_test):
    ctx = await context(client)
    cost = CostService(mysql_sessions)
    provider = FakeProvider(cost, mode="wait")
    router = ProviderRouter([provider], cost)
    task = asyncio.create_task(router.generate(ctx.kind, PAYLOAD, ctx))
    try:
        await asyncio.wait_for(provider.entered.wait(), 5)
        with pytest.raises(ProviderPendingError):
            await router.generate(ctx.kind, PAYLOAD, ctx)
    finally:
        provider.continue_call.set()
        await task
    assert provider.calls == 1 and (await budget(client, ctx))["spent_cents"] == 20


async def test_cancelled_call_keeps_reservation(client, mysql_sessions, redis_test):
    ctx = await context(client)
    cost = CostService(mysql_sessions)
    provider = FakeProvider(cost, mode="wait")
    router = ProviderRouter([provider], cost)
    task = asyncio.create_task(router.generate(ctx.kind, PAYLOAD, ctx))
    await asyncio.wait_for(provider.entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    request = await request_for(cost, ctx)
    assert request.status == "unknown" and (await budget(client, ctx))["reserved_cents"] == 30
    provider.actual = 5
    provider.query_result = provider.result(success=False)
    assert not (await router.reconcile(request.id)).success
    current = await budget(client, ctx)
    assert (current["spent_cents"], current["reserved_cents"]) == (5, 0)


async def test_capability_and_run_are_part_of_identity(client, mysql_sessions, redis_test):
    ctx = await context(client)
    cost = CostService(mysql_sessions)
    image = FakeProvider(cost, "same-vendor")
    video = FakeProvider(cost, "same-vendor", kind=ProviderKind.VIDEO)
    router = ProviderRouter([video, image], cost)
    assert (await router.generate(ctx.kind, PAYLOAD, ctx)).kind == ProviderKind.IMAGE
    video_ctx = replace(ctx, kind=ProviderKind.VIDEO)
    assert (await router.generate(video_ctx.kind, PAYLOAD, video_ctx)).kind == ProviderKind.VIDEO
    other_ctx = await context(client)
    await router.generate(other_ctx.kind, PAYLOAD, other_ctx)
    async with mysql_sessions.begin() as db:
        next_run = PipelineRun(project_id=ctx.project_id, status="running")
        db.add(next_run)
        await db.flush()
        new_run_ctx = replace(ctx, run_id=next_run.id)
    await router.generate(new_run_ctx.kind, PAYLOAD, new_run_ctx)
    assert image.calls == 3 and video.calls == 1
    with pytest.raises(AppError):
        await router.generate(ProviderKind.VIDEO, PAYLOAD, ctx)
    with pytest.raises(AppError):
        await router.generate(
            ctx.kind, PAYLOAD, replace(ctx, run_id=other_ctx.run_id, operation_key="wrong-run")
        )
    assert image.calls == 3


@pytest.mark.parametrize("quote", [-1, True, 1.5])
async def test_invalid_quote_never_dispatches(client, mysql_sessions, redis_test, quote):
    ctx = await context(client)
    cost = CostService(mysql_sessions)
    provider = FakeProvider(cost, quote=quote)
    with pytest.raises(AppError):
        await provider.generate(PAYLOAD, ctx)
    assert provider.calls == 0 and await request_for(cost, ctx) is None


async def test_zero_budget_accepts_only_guaranteed_free_call(client, mysql_sessions, redis_test):
    ctx = await context(client, budget=0)
    cost = CostService(mysql_sessions)
    with pytest.raises(BudgetExceededError):
        await FakeProvider(cost).generate(PAYLOAD, ctx)
    assert (await FakeProvider(cost, quote=0, actual=0).generate(PAYLOAD, ctx)).success
    assert (await budget(client, ctx))["remaining_cents"] == 0


async def test_bill_above_quote_is_visible_and_freezes_project(client, mysql_sessions, redis_test):
    ctx = await context(client)
    cost = CostService(mysql_sessions)
    provider = FakeProvider(cost, actual=40)
    with pytest.raises(ProviderUncertainError):
        await provider.generate(PAYLOAD, ctx)
    current = await budget(client, ctx)
    assert current["billing_disputed"] and current["reserved_cents"] == 30 and current["spent_cents"] == 0
    calls = (await client.get(f"/api/v1/budget/{ctx.project_id}/calls")).json()["data"]["items"]
    assert calls[0]["reported_cost_cents"] == 40 and calls[0]["error_code"] == "BUDGET_QUOTE_MISMATCH"
    with pytest.raises(ProviderUncertainError):
        await provider.generate(PAYLOAD, replace(ctx, operation_key="another-call"))
    assert provider.calls == 1


async def test_database_rejects_budget_invariant_violation(client, mysql_sessions):
    ctx = await context(client)
    with pytest.raises(DBAPIError) as error:
        async with mysql_sessions.begin() as db:
            row = await db.scalar(
                select(BudgetLedger).where(
                    BudgetLedger.project_id == ctx.project_id, BudgetLedger.scope_key == "total"
                )
            )
            row.reserved_cents = 101
    assert error.value.orig.args[0] == 3819  # MySQL CHECK 约束确实拦截，而非其他 SQL 异常。
    assert (await budget(client, ctx))["reserved_cents"] == 0


async def test_renewal_keeps_long_task_slot_and_release_allows_next(redis_test):
    async with provider_semaphore("long", lease_ttl_s=0.6) as lease:
        assert lease
        await lease.run(asyncio.sleep(1.4))
        async with provider_semaphore("long", wait_timeout_s=0) as blocked:
            assert blocked is None
    async with provider_semaphore("long", wait_timeout_s=0) as successor:
        assert successor


async def test_stale_owner_cannot_renew_or_release_successor(redis_test):
    async with provider_semaphore("stale") as old:
        await redis_test.zadd(old.key, {old.token: 0})
        async with provider_semaphore("stale", wait_timeout_s=0) as new:
            assert new and new.token != old.token
            assert not await old.renew()
            await old.release()
            assert await redis_test.zscore(new.key, new.token) is not None
            async with provider_semaphore("stale", wait_timeout_s=0) as blocked:
                assert blocked is None


async def test_lost_lease_cancels_call_and_preserves_budget(client, mysql_sessions, redis_test, monkeypatch):
    import app.providers.base as base

    original = base.provider_semaphore
    monkeypatch.setattr(base, "provider_semaphore", lambda name: original(name, lease_ttl_s=0.3))

    async def lost(self):
        return False

    monkeypatch.setattr(ProviderLease, "renew", lost)
    ctx = await context(client)
    cost = CostService(mysql_sessions)
    provider = FakeProvider(cost, mode="wait")
    with pytest.raises(ProviderUncertainError):
        await asyncio.wait_for(provider.generate(PAYLOAD, ctx), 5)
    assert (await request_for(cost, ctx)).status == "unknown"
    assert (await budget(client, ctx))["reserved_cents"] == 30
