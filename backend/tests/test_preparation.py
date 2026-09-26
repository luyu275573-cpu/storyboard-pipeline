"""真实 MySQL 上验证生成前闭环、图片边界与版本失效。"""

import asyncio
import io

import pytest

from tests.test_catalog_api import create_character, create_project, run_id

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("redis_test")]


@pytest.fixture
def asset_root(monkeypatch, tmp_path):
    from app.core.config import settings

    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    return tmp_path


def image_bytes(color="navy", size=(96, 128)):
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


async def upload(client, char, data=None, ref_type="front_half"):
    return await client.post(
        f"/api/v1/characters/{char['id']}/refs",
        params={"ref_type": ref_type, "anchor_version": char["anchor_version"]},
        content=data if data is not None else image_bytes(),
        headers={"Content-Type": "image/png"},
    )


async def review(client, ref, char, rid, **overrides):
    return await client.post(
        f"/api/v1/characters/refs/{ref['id']}/review",
        json={
            "run_id": rid,
            "anchor_version": char["anchor_version"],
            "expected_review_version": ref["review_version"],
            "passed": True,
            "is_primary": True,
            "reviewer": "人工测试",
            "note": "已对照当前版本核对形状、颜色与肢体结构",
            **overrides,
        },
    )


async def prepared(client):
    project = await create_project(client)
    char, char_body = await create_character(client, project)
    rid = await run_id(client, project)
    assert (
        await client.post(
            f"/api/v1/characters/{char['id']}/confirm",
            json={
                "run_id": rid,
                "anchor_version": 1,
                "reviewer": "测试",
            },
        )
    ).status_code == 200
    response = await upload(client, char)
    assert response.status_code == 201, response.text
    ref = response.json()["data"]
    response = await review(client, ref, char, rid)
    assert response.status_code == 200, response.text
    scene = (
        await client.post(
            "/api/v1/scenes",
            json={
                "project_id": project["id"],
                "seq": 1,
                "location": "茶馆",
            },
        )
    ).json()["data"]
    shot_body = {
        "scene_id": scene["id"],
        "seq": 1,
        "composition": "中心构图",
        "action_text": "推门走进",
        "character_ids": [char["id"]],
    }
    shot = (await client.post("/api/v1/shots", json=shot_body)).json()["data"]
    return project, char, char_body, rid, ref, scene, shot, shot_body


async def board(client, project, rid):
    response = await client.get(f"/api/v1/projects/{project['id']}/storyboard", params={"run_id": rid})
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def decide(client, project, rid, version, **overrides):
    return await client.post(
        f"/api/v1/projects/{project['id']}/storyboard/review",
        json={
            "run_id": rid,
            "storyboard_version": version,
            "status": "approved",
            "reviewer": "分镜审核人",
            "note": "镜头规模和时长可实现",
            **overrides,
        },
    )


async def test_preparation_to_model_boundary_and_restart(client, asset_root):
    project, char, _, rid, _, scene, shot, _ = await prepared(client)
    current = await board(client, project, rid)
    assert current["blockers"] == []
    results = await asyncio.gather(
        *(decide(client, project, rid, current["storyboard_version"]) for _ in range(2))
    )
    assert [r.status_code for r in results] == [200, 200]
    assert results[0].json()["data"]["id"] == results[1].json()["data"]["id"]
    # 新 HTTP/数据库会话从持久快照恢复，不创建第二个运行或付费请求。
    for _ in range(2):
        result = await client.post(f"/api/v1/projects/{project['id']}/runs/{rid}/resume")
        assert result.status_code == 200, result.text
        assert result.json()["data"]["status"] == "waiting_model"
        assert result.json()["data"]["graph_state"]["pending_shot_ids"] == [shot["id"]]
    runs = (await client.get(f"/api/v1/projects/{project['id']}/runs")).json()["data"]
    assert len(runs) == 1
    assert (await client.get(f"/api/v1/stream/{rid}/snapshot")).json()["data"]["status"] == "waiting_model"
    gates = (await client.get(f"/api/v1/projects/{project['id']}/gates")).json()["data"]
    frozen = next(g["snapshot"] for g in gates if g["gate_type"] == "storyboard")
    assert frozen["characters"][0]["id"] == char["id"]
    assert frozen["scenes"][0]["id"] == scene["id"]
    assert frozen["references"][0]["is_primary"]
    budget = (await client.get(f"/api/v1/budget/{project['id']}")).json()["data"]
    assert budget["spent_cents"] == budget["reserved_cents"] == 0


async def test_upstream_edit_invalidates_but_keeps_audit(client, asset_root):
    project, char, char_body, rid, _, _, shot, shot_body = await prepared(client)
    old = await board(client, project, rid)
    assert (await decide(client, project, rid, old["storyboard_version"])).status_code == 200
    updated = await client.put(
        f"/api/v1/shots/{shot['id']}",
        json={
            **shot_body,
            "action_text": "坐下",
            "expected_version": 1,
        },
    )
    assert updated.status_code == 200, updated.text
    current = await board(client, project, rid)
    assert current["storyboard_version"] > old["storyboard_version"] and current["gate"] is None
    assert (await decide(client, project, rid, old["storyboard_version"])).status_code == 409
    assert (await decide(client, project, rid, current["storyboard_version"])).status_code == 200
    assert (
        await client.put(
            f"/api/v1/characters/{char['id']}",
            json={
                **char_body,
                "expected_version": 1,
                "hair_features": {"发色": "白色"},
            },
        )
    ).status_code == 200
    current = await board(client, project, rid)
    assert not current["characters_ready"] and len(current["blockers"]) == 2
    assert not current["references"][0]["qc_passed"]
    assert (await decide(client, project, rid, current["storyboard_version"])).status_code == 423
    gates = (await client.get(f"/api/v1/projects/{project['id']}/gates")).json()["data"]
    snapshots = [g["snapshot"] for g in gates if g["gate_type"] == "storyboard"]
    assert len(snapshots) == 2
    assert {s["shots"][0]["action_text"] for s in snapshots} == {"推门走进", "坐下"}
    assert all(s["characters"][0]["anchor_version"] == 1 for s in snapshots)


async def test_reference_dedup_reviews_and_primary_uniqueness(client, asset_root):
    project = await create_project(client)
    char, _ = await create_character(client, project)
    rid = await run_id(client, project)
    responses = await asyncio.gather(*(upload(client, char) for _ in range(2)))
    assert [r.status_code for r in responses] == [201, 201]
    ref = responses[0].json()["data"]
    assert ref["id"] == responses[1].json()["data"]["id"]
    assert not ref["qc_passed"]
    assert (await client.get(f"/api/v1/characters/refs/{ref['id']}/asset")).headers[
        "content-type"
    ] == "image/png"
    responses = await asyncio.gather(
        review(client, ref, char, rid), review(client, ref, char, rid, passed=False, is_primary=False)
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    second = (await upload(client, char, image_bytes("green"))).json()["data"]
    assert (await review(client, second, char, rid)).status_code == 200
    refs = (await client.get(f"/api/v1/characters/{char['id']}/refs")).json()["data"]
    assert [r["id"] for r in refs if r["is_primary"]] == [second["id"]]
    assert len(list(asset_root.rglob("*.png"))) == 2
    assert (await upload(client, char, ref_type="side_half")).status_code == 409


@pytest.mark.parametrize("data", [b"<svg onload='alert(1)'></svg>", b"not an image", b"\x89PNG\r\n\x1a\n"])
async def test_invalid_image_rejected(client, asset_root, data):
    project = await create_project(client)
    char, _ = await create_character(client, project)
    assert (await upload(client, char, data)).status_code == 400
    assert not list(asset_root.rglob("*.png"))


async def test_image_size_stale_version_and_cross_project(client, asset_root, monkeypatch):
    from app.core.config import settings

    project, other = await create_project(client), await create_project(client)
    char, char_body = await create_character(client, project)
    rid = await run_id(client, project)
    assert (await upload(client, char, image_bytes(size=(1, 1)))).status_code == 400
    ref = (await upload(client, char, ref_type="side_half")).json()["data"]
    assert (await review(client, ref, char, rid)).status_code == 400
    assert (await review(client, ref, char, await run_id(client, other), is_primary=False)).status_code == 404
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    assert (await upload(client, char)).status_code == 400
    monkeypatch.setattr(settings, "max_upload_mb", 20)
    await client.put(f"/api/v1/characters/{char['id']}", json={**char_body, "expected_version": 1})
    assert (await upload(client, char)).status_code == 409
    assert (await review(client, ref, char, rid, is_primary=False)).status_code == 409


async def test_shot_and_scene_concurrency_and_scope(client, asset_root):
    project, _, _, rid, _, scene, shot, body = await prepared(client)
    responses = await asyncio.gather(
        *(
            client.put(
                f"/api/v1/shots/{shot['id']}",
                json={
                    **body,
                    "expected_version": 1,
                    "action_text": action,
                },
            )
            for action in ("打开窗户", "走到门口")
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert (await client.post("/api/v1/shots", json=body)).status_code == 409
    other = await create_project(client)
    foreign, _ = await create_character(client, other)
    assert (
        await client.post("/api/v1/shots", json={**body, "seq": 2, "character_ids": [foreign["id"]]})
    ).status_code == 400
    assert (await client.post("/api/v1/shots", json={**body, "shot_size": "unknown"})).status_code == 422
    assert (await client.post("/api/v1/shots", json={**body, "composition": " "})).status_code == 422
    current = await board(client, project, rid)
    assert (
        await client.delete(
            f"/api/v1/scenes/{scene['id']}",
            params={
                "expected_storyboard_version": current["storyboard_version"],
            },
        )
    ).status_code == 409
    assert (await client.delete(f"/api/v1/shots/{shot['id']}?expected_version=1")).status_code == 409
    assert (await client.delete(f"/api/v1/shots/{shot['id']}?expected_version=2")).status_code == 200
    current = await board(client, project, rid)
    assert (
        await client.delete(
            f"/api/v1/scenes/{scene['id']}",
            params={
                "expected_storyboard_version": current["storyboard_version"],
            },
        )
    ).status_code == 200


async def test_rejected_board_cannot_be_overwritten_and_foreign_run_rejected(client, asset_root):
    project, _, _, rid, _, _, _, _ = await prepared(client)
    current = await board(client, project, rid)
    assert (
        await decide(client, project, rid, current["storyboard_version"], status="rejected")
    ).status_code == 200
    assert (await decide(client, project, rid, current["storyboard_version"])).status_code == 409
    other = await create_project(client)
    assert (
        await decide(client, project, await run_id(client, other), current["storyboard_version"])
    ).status_code == 404
    result = await client.post(f"/api/v1/projects/{project['id']}/runs")
    assert result.json()["data"]["status"] == "waiting_gate"


async def test_pending_provider_request_prevents_upstream_changes(client, mysql_sessions, asset_root):
    from app.models.tracking import ProviderRequest

    project, _, _, rid, _, _, shot, body = await prepared(client)
    async with mysql_sessions.begin() as db:
        db.add(
            ProviderRequest(
                project_id=project["id"],
                run_id=rid,
                operation_key="pending-test",
                kind="image",
                idempotency_key=project["id"],
                request_hash="a" * 64,
                owner_token="test",
                status="unknown",
            )
        )
    assert (
        await client.put(f"/api/v1/shots/{shot['id']}", json={**body, "expected_version": 1})
    ).status_code == 409
    assert (await client.delete(f"/api/v1/shots/{shot['id']}?expected_version=1")).status_code == 409


async def test_stream_validates_run_and_cursor(client):
    assert (await client.get("/api/v1/stream/missing")).status_code == 404
    assert (await client.get("/api/v1/stream/missing/snapshot")).status_code == 404
    assert (
        await client.get("/api/v1/stream/missing", headers={"Last-Event-ID": "bad\nvalue"})
    ).status_code == 400
