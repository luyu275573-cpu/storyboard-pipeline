"""自然语言角色描述到结构化锚定特征的边界。"""

import pytest

from app.models.enums import ProviderKind
from app.providers import ProviderResult
from tests.test_catalog_api import create_project

pytestmark = pytest.mark.integration


class FakeLLMRouter:
    async def generate(self, kind, payload, context):
        assert kind is ProviderKind.LLM
        assert context.operation_key.startswith("character-parse:")
        assert payload["messages"][1]["content"].startswith("项目画风：")
        return ProviderResult(
            True,
            ProviderKind.LLM,
            "test",
            "test-llm",
            text=(
                '{"face_features":{"脸型":"方脸","眼睛":"深棕色单眼皮"},'
                '"hair_features":{"发型":"短黑发"},'
                '"body_features":{"身高":"178cm"},'
                '"outfit_features":{"服饰":"青灰色道袍"},'
                '"style_lock":{"画风":"港风写实"}}'
            ),
            cost_cents=2,
        )


async def test_parse_character_description_returns_reviewable_features(client, monkeypatch):
    project = await create_project(client)
    monkeypatch.setattr("app.api.v1.characters.build_router", lambda _: FakeLLMRouter())
    response = await client.post(
        "/api/v1/characters/parse",
        json={
            "project_id": project["id"],
            "name": "林九",
            "description": "一个身材高瘦的道士，穿旧青灰道袍，短黑发，眼神坚定。",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["face_features"]["脸型"] == "方脸"
    assert data["source_description"].startswith("一个身材")
    assert data["anchor_prompt"] and data["cost_cents"] == 2


async def test_parse_character_description_requires_current_character_stage(client):
    project = await create_project(client)
    response = await client.post(
        "/api/v1/characters/parse",
        json={
            "project_id": project["id"],
            "name": "短描述",
            "description": "太短",
        },
    )
    assert response.status_code == 422
