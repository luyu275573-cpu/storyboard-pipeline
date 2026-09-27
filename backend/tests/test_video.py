"""Paid video safety and acceptance boundaries, with no real provider calls."""

import json
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import AppError, ConflictError
from app.models.domain import PipelineRun, Project, ReviewGate, Scene, Shot
from app.models.tracking import ApiCallLog, BudgetLedger, RenderAttempt
from app.providers import ProviderResult
from app.providers.video_siliconflow import SiliconFlowVideoProvider
from app.schemas import MediaDecision
from app.services.video import decide_video, prepare_video, run_video
from app.services.video_export import prepare_export


async def test_submit_persists_task_before_query_and_recovery_never_submits(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "siliconflow_api_key", "fake")
    frame = tmp_path / "frame.png"
    Image.new("RGB", (16, 16)).save(frame)
    cost = AsyncMock()
    provider = SiliconFlowVideoProvider(cost)
    call = ApiCallLog(id=1, model=settings.siliconflow_video_model, quoted_cents=200)
    seen = []

    async def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/submit"):
            payload = json.loads(request.content)
            assert payload["image"].startswith("data:image/png;base64,")
            assert set(payload) == {"model", "prompt", "image_size", "image", "negative_prompt", "seed"}
            return httpx.Response(200, json={"requestId": "remote-1"})
        if request.url.path.endswith("/status"):
            cost.attach_task.assert_awaited_once_with(1, "remote-1")
            assert json.loads(request.content) == {"requestId": "remote-1"}
            return httpx.Response(
                200,
                json={
                    "status": "Succeed",
                    "results": {"videos": [{"url": "https://download.test/result.mp4"}]},
                },
            )
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"mock video")

    monkeypatch.setattr(
        "app.providers.video_siliconflow.probe_video",
        AsyncMock(return_value={"duration_ms": 5000, "width": 960, "height": 960, "codec": "h264"}),
    )
    payload = {
        "model": settings.siliconflow_video_model,
        "prompt": "推门",
        "image_size": "960x960",
        "image_path": str(frame),
        "seed": "stable",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await provider._call(payload, client, call)
        assert result.success and result.cost_cents == 200
        assert (tmp_path / result.asset_path).read_bytes() == b"mock video"
        recovered = await provider._query(client, call)
        assert recovered.asset_sha256 == result.asset_sha256
    assert sum(p.endswith("/submit") for p in seen) == 1


@pytest.mark.parametrize("status", ["InProgress", "InQueue", "Failed"])
async def test_nonterminal_or_failed_never_reports_zero_billing(status):
    provider = SiliconFlowVideoProvider(AsyncMock())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": status}))
    ) as client:
        call = ApiCallLog(provider_task_id="existing", model=settings.siliconflow_video_model)
        if status == "Failed":
            with pytest.raises(AppError):
                await provider._query(client, call)
        else:
            assert await provider._query(client, call) is None


async def seed_demo(sessions, root):
    generated = root / "generated"
    generated.mkdir(exist_ok=True)
    Image.new("RGB", (32, 32)).save(generated / "frame.png")
    async with sessions.begin() as db:
        project = Project(title="视频隔离测试", budget_cents=20000)
        db.add(project)
        await db.flush()
        run = PipelineRun(
            project_id=project.id, status="waiting_model", graph_state={"storyboard_version": 1}
        )
        scene = Scene(project_id=project.id, seq=1, location="茶馆")
        db.add_all([run, scene])
        await db.flush()
        shot = Shot(scene_id=scene.id, seq=1, composition="中心", action_text="推门", status="video")
        db.add(shot)
        await db.flush()
        image = RenderAttempt(
            shot_id=shot.id,
            attempt_no=1,
            stage="image",
            provider="test",
            model="test",
            status="succeeded",
            asset_path="generated/frame.png",
            idempotency_key=uuid.uuid4().hex,
        )
        db.add(image)
        await db.flush()
        shot.locked_attempt_id = image.id
        db.add_all(
            [
                ReviewGate(
                    run_id=run.id,
                    gate_type="storyboard",
                    status="approved",
                    snapshot={"storyboard_version": 1},
                ),
                ReviewGate(
                    run_id=run.id,
                    gate_type="compliance",
                    status="approved",
                    snapshot={"shot_id": shot.id, "attempt_id": image.id},
                ),
            ]
        )
    return project.id, run.id, shot.id, image.id


async def test_video_replay_budget_and_review_boundaries(mysql_sessions, redis_test, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    monkeypatch.setattr(settings, "siliconflow_api_key", "fake")
    project_id, run_id, shot_id, image_id = await seed_demo(mysql_sessions, tmp_path)
    async with mysql_sessions.begin() as db:
        attempt = await prepare_video(db, shot_id)
        attempt_id = attempt.id
    async with mysql_sessions.begin() as db:
        assert (await prepare_video(db, shot_id)).id == attempt_id
    calls = []

    async def fake_call(self, payload, client, call):
        calls.append(call.id)
        await self.cost.attach_task(call.id, "task-1")
        return ProviderResult(
            True, self.kind, self.name, call.model, asset_path="generated/test.mp4", cost_cents=200
        )

    monkeypatch.setattr(SiliconFlowVideoProvider, "_call", fake_call)
    ctx = {"session_factory": mysql_sessions}
    await run_video(ctx, attempt_id)
    await run_video(ctx, attempt_id)
    assert len(calls) == 1
    async with mysql_sessions.begin() as db:
        ledger = await db.scalar(
            select(BudgetLedger).where(BudgetLedger.project_id == project_id, BudgetLedger.scope == "project")
        )
        assert ledger.spent_cents == 200 and ledger.reserved_cents == 0
        with pytest.raises(AppError):
            await prepare_export(db, project_id, run_id, False)
        preview = await prepare_export(db, project_id, run_id, True)
        assert preview.metrics["preview"] is True
        shot = await db.get(Shot, shot_id)
        assert shot.locked_attempt_id == image_id and shot.accepted_video_attempt_id is None
        body = MediaDecision(
            run_id=run_id,
            attempt_id=attempt_id,
            expected_version=shot.version,
            status="approved",
            reviewer="测试审核",
            note="仅隔离数据库测试",
        )
        with pytest.raises(ConflictError):
            await decide_video(db, shot_id, body.model_copy(update={"attempt_id": image_id}))
        with pytest.raises(ConflictError):
            await decide_video(db, shot_id, body.model_copy(update={"expected_version": shot.version + 1}))
        await decide_video(db, shot_id, body)
        assert shot.locked_attempt_id == image_id and shot.accepted_video_attempt_id == attempt_id
        assert not (await prepare_export(db, project_id, run_id, False)).metrics["preview"]


async def test_video_api_returns_created_timestamp_and_replays(client, mysql_sessions, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    queued = AsyncMock(return_value="job")
    monkeypatch.setattr("app.core.queue.enqueue_job", queued)
    _, _, shot_id, _ = await seed_demo(mysql_sessions, tmp_path)
    first = await client.post(f"/api/v1/shots/{shot_id}/video")
    assert first.status_code == 200, first.text
    row = first.json()["data"]
    assert row["created_at"] and row["stage"] == "video"
    second = await client.post(f"/api/v1/shots/{shot_id}/video")
    assert second.status_code == 200 and second.json()["data"]["id"] == row["id"]
