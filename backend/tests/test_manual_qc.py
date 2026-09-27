"""人工接管视觉模型未知结果的边界。"""

import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.models.domain import PipelineRun, Project, ReviewGate, Scene, Shot
from app.models.tracking import ApiCallLog, BudgetLedger, ProviderRequest, QCReport, RenderAttempt

pytestmark = pytest.mark.integration


async def seed_unknown_qc(sessions, root):
    generated = root / "generated"
    generated.mkdir(exist_ok=True)
    Image.new("RGB", (32, 32), "navy").save(generated / "frame.png")
    async with sessions.begin() as db:
        project = Project(title="人工 QC 测试", budget_cents=20000)
        db.add(project)
        await db.flush()
        run = PipelineRun(
            project_id=project.id,
            status="waiting_model",
            graph_state={"storyboard_version": project.storyboard_version},
        )
        scene = Scene(project_id=project.id, seq=1, location="测试场景")
        db.add_all([run, scene])
        await db.flush()
        shot = Shot(scene_id=scene.id, seq=1, composition="中心", action_text="停留", status="suspended")
        db.add(shot)
        await db.flush()
        attempt = RenderAttempt(
            shot_id=shot.id,
            attempt_no=1,
            stage="image",
            provider="test",
            model="test-image",
            status="succeeded",
            asset_path="generated/frame.png",
            idempotency_key=uuid.uuid4().hex,
        )
        db.add(attempt)
        await db.flush()
        request = ProviderRequest(
            project_id=project.id,
            run_id=run.id,
            operation_key=f"qc:{shot.id}:{attempt.id}",
            kind="vision",
            idempotency_key=uuid.uuid4().hex,
            request_hash="a" * 64,
            owner_token="test",
            status="unknown",
        )
        db.add(request)
        await db.flush()
        call = ApiCallLog(
            request_id=request.id,
            status="unknown",
            quoted_cents=1,
            reserved_cents=1,
            project_id=project.id,
            shot_id=shot.id,
            attempt_id=attempt.id,
            kind="vision",
            provider="test",
            model="test-vision",
            success=False,
            cost_cents=0,
        )
        db.add(call)
        db.add(
            BudgetLedger(
                project_id=project.id,
                scope="project",
                scope_key="total",
                budget_cents=project.budget_cents,
                reserved_cents=1,
            )
        )
        db.add(
            BudgetLedger(
                project_id=project.id,
                scope="kind",
                scope_key="vision",
                budget_cents=project.budget_cents,
                reserved_cents=1,
            )
        )
        db.add(
            ReviewGate(
                run_id=run.id,
                gate_type="storyboard",
                status="approved",
                snapshot={"storyboard_version": project.storyboard_version},
            )
        )
    return project.id, run.id, shot.id, attempt.id


async def test_manual_qc_pass_keeps_unknown_reservation_and_version(
    client, mysql_sessions, tmp_path, monkeypatch
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    project_id, run_id, shot_id, attempt_id = await seed_unknown_qc(mysql_sessions, tmp_path)
    body = {
        "run_id": run_id,
        "attempt_id": attempt_id,
        "expected_version": 1,
        "status": "pass",
        "reviewer": "人工测试",
        "note": "关键帧构图、角色和画面质量均可接受",
    }
    response = await client.post(f"/api/v1/shots/{shot_id}/qc/manual", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "review"

    async with mysql_sessions.begin() as db:
        shot = await db.get(Shot, shot_id)
        report = await db.scalar(select(QCReport).where(QCReport.attempt_id == attempt_id))
        request = await db.scalar(select(ProviderRequest).where(ProviderRequest.run_id == run_id))
        call = await db.scalar(select(ApiCallLog).where(ApiCallLog.attempt_id == attempt_id))
        ledgers = list(await db.scalars(select(BudgetLedger).where(BudgetLedger.project_id == project_id)))
        assert shot and shot.status == "review" and shot.version == 1
        assert report and report.verdict == "pass" and report.human_verdict == "pass"
        assert request and request.status == "unknown"
        assert call and call.status == "unknown" and call.reserved_cents == 1
        assert all(ledger.reserved_cents == 1 for ledger in ledgers)

    duplicate = await client.post(f"/api/v1/shots/{shot_id}/qc/manual", json=body)
    assert duplicate.status_code == 409
    stale = await client.post(
        f"/api/v1/shots/{shot_id}/qc/manual", json={**body, "expected_version": 2}
    )
    assert stale.status_code == 409


async def test_manual_qc_reject_keeps_shot_suspended(client, mysql_sessions, tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    _, run_id, shot_id, attempt_id = await seed_unknown_qc(mysql_sessions, tmp_path)
    response = await client.post(
        f"/api/v1/shots/{shot_id}/qc/manual",
        json={
            "run_id": run_id,
            "attempt_id": attempt_id,
            "expected_version": 1,
            "status": "reject",
            "reviewer": "人工测试",
            "note": "发现画面不合格，需要重新生成",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "suspended"
