"""在专用 MySQL 测试库验证 HTTP、事务和版本竞争，不用 SQLite 替代。"""

import asyncio
import uuid

import pytest

pytestmark = pytest.mark.integration


async def create_project(client, budget=2500):
    response = await client.post(
        "/api/v1/projects",
        json={
            "title": f"测试故事 {uuid.uuid4().hex[:8]}",
            "budget_cents": budget,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def create_character(client, project):
    body = {
        "project_id": project["id"],
        "name": "林晚",
        "face_features": {"脸型": "鹅蛋脸"},
        "hair_features": {"发色": "#1A1A1A"},
    }
    response = await client.post("/api/v1/characters", json=body)
    assert response.status_code == 201, response.text
    return response.json()["data"], body


async def run_id(client, project):
    response = await client.get(f"/api/v1/projects/{project['id']}/runs")
    run = response.json()["data"][0]
    assert run["status"] == "waiting_gate" and run["current_stage"] == "character"
    return run["id"]


@pytest.mark.parametrize("budget", [0, 2500, 20000])
async def test_project_budget_and_search(client, budget):
    project = await create_project(client, budget)
    data = (await client.get(f"/api/v1/budget/{project['id']}")).json()["data"]
    assert data["budget_cents"] == data["remaining_cents"] == budget
    assert data["spent_cents"] == 0 and len(data["ledgers"]) == 5
    assert sum(row["scope"] == "project" for row in data["ledgers"]) == 1
    result = (await client.get("/api/v1/projects", params={"q": project["title"]})).json()["data"]
    assert result["total"] == 1 and result["items"][0]["id"] == project["id"]
    await run_id(client, project)
    assert (await client.get("/api/v1/budget/config")).status_code == 200
    assert (await client.get("/api/v1/projects?page=0")).status_code == 422
    assert (await client.get("/api/v1/projects/missing")).status_code == 404
    assert (await client.post("/api/v1/projects", json={"title": "   "})).status_code == 422
    assert (
        await client.post("/api/v1/projects", json={"title": "超预算", "budget_cents": 20001})
    ).status_code == 422


async def test_character_approval_version_and_history(client):
    project = await create_project(client)
    character, body = await create_character(client, project)
    path = f"/api/v1/characters/{character['id']}"
    assert character["anchor_version"] == 1 and not character["confirmed"]
    assert (await client.post("/api/v1/characters", json=body)).status_code == 409
    preview = (await client.post(path + "/anchor-preview", json={})).json()["data"]
    assert preview["anchor_prompt"] == character["anchor_prompt"]
    approval = {"run_id": await run_id(client, project), "anchor_version": 1, "reviewer": "测试审核人"}
    responses = await asyncio.gather(*(client.post(path + "/confirm", json=approval) for _ in range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    history = (await client.get(f"/api/v1/projects/{project['id']}/gates")).json()["data"]
    assert len(history) == 1
    assert history[0]["snapshot"]["anchor_version"] == 1
    updated = await client.put(
        path, json={**body, "expected_version": 1, "hair_features": {"发色": "#445566"}}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["data"]["anchor_version"] == 2
    assert not updated.json()["data"]["confirmed"]
    assert (await client.post(path + "/confirm", json=approval)).status_code == 409
    assert (await client.put(path, json={**body, "expected_version": 1})).status_code == 409
    history = (await client.get(f"/api/v1/projects/{project['id']}/gates")).json()["data"]
    assert history[0]["snapshot"]["hair_features"]["发色"] == "#1A1A1A"


async def test_concurrent_updates_only_one_wins(client):
    project = await create_project(client)
    character, body = await create_character(client, project)
    responses = await asyncio.gather(
        *(
            client.put(
                f"/api/v1/characters/{character['id']}",
                json={
                    **body,
                    "expected_version": 1,
                    "hair_features": {"发色": color},
                },
            )
            for color in ("#111111", "#222222")
        )
    )
    assert sorted(r.status_code for r in responses) == [200, 409]
    latest = (await client.get(f"/api/v1/characters/{character['id']}")).json()["data"]
    winner = next(r.json()["data"] for r in responses if r.status_code == 200)
    assert latest["anchor_version"] == 2 and latest["hair_features"] == winner["hair_features"]


async def test_cross_project_and_invalid_inputs_rejected(client):
    project, other = await create_project(client), await create_project(client)
    character, body = await create_character(client, project)
    path = f"/api/v1/characters/{character['id']}"
    response = await client.post(
        path + "/confirm",
        json={
            "run_id": await run_id(client, other),
            "anchor_version": 1,
            "reviewer": "测试",
        },
    )
    assert response.status_code == 400
    assert (
        await client.put(path, json={**body, "project_id": other["id"], "expected_version": 1})
    ).status_code == 400
    assert (await client.post(path + "/anchor-preview", json={"level": "unknown"})).status_code == 422
    assert (
        await client.post("/api/v1/characters", json={**body, "face_features": {"": "x"}})
    ).status_code == 422
    oversized = {str(index): "长" * 500 for index in range(30)}
    assert (
        await client.put(
            path,
            json={**body, "expected_version": 1, "face_features": oversized, "hair_features": oversized},
        )
    ).status_code == 400
    current = (await client.get(path)).json()["data"]
    assert {key: current[key] for key in character} == character  # 失败不能部分写入旧特征与版本。
    empty = (
        await client.post("/api/v1/characters", json={"name": "未填写", "project_id": project["id"]})
    ).json()["data"]
    assert (
        await client.post(
            f"/api/v1/characters/{empty['id']}/confirm",
            json={
                "run_id": await run_id(client, project),
                "anchor_version": 1,
                "reviewer": "测试",
            },
        )
    ).status_code == 400
