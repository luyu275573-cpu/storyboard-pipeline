from types import SimpleNamespace

import pytest

from app.models.enums import AttemptStatus, ProviderKind
from app.providers.base import ProviderResult
from app.services.rendering import (
    RenderSpec,
    apply_provider_result,
    build_image_payload,
    prepare_attempt,
    render_image,
)


def _snapshot():
    return (
        SimpleNamespace(style="厚涂", global_negative_prompt="模糊"),
        SimpleNamespace(location="教室", time_of_day="傍晚", mood="安静", background_prompt="窗边"),
        SimpleNamespace(
            shot_size="中景",
            composition="主体居中",
            action_text="抬头",
            dialogue="你好",
            negative_prompt="多余手指",
        ),
        [SimpleNamespace(name="林晚", anchor_prompt="角色[林晚] 面部=鹅蛋脸")],
    )


def test_build_image_payload_keeps_locked_prompt_inputs():
    project, scene, shot, characters = _snapshot()
    payload = build_image_payload(
        project=project,
        scene=scene,
        shot=shot,
        characters=characters,
        reference_paths=["refs/a.png"],
        seed="seed-1",
    )
    assert payload["image_paths"] == ["refs/a.png"]
    assert payload["seed"] == "seed-1"
    assert "林晚" in payload["prompt"] and "抬头" in payload["prompt"]
    assert payload["negative_prompt"] == "模糊；多余手指"


def test_prepare_attempt_identity_is_stable_and_traceable():
    spec = RenderSpec("project", "run", "shot", 2, 3, seed="seed-2")
    attempt, context = prepare_attempt(spec=spec, payload={"model": "image-v1", "prompt": "x"})
    assert context.kind is ProviderKind.IMAGE
    assert context.operation_key == "render:shot:2"
    assert attempt.status == AttemptStatus.PENDING.value
    assert attempt.anchor_version == 3
    assert len(attempt.idempotency_key) == 64


def test_apply_provider_result_maps_asset_and_cost():
    attempt, _ = prepare_attempt(
        spec=RenderSpec("project", "run", "shot", 1, 1), payload={"model": "image-v1", "prompt": "x"}
    )
    apply_provider_result(
        attempt,
        ProviderResult(
            True,
            ProviderKind.IMAGE,
            "siliconflow",
            "image-v1",
            asset_path="generated/a.png",
            asset_sha256="a" * 64,
            cost_cents=30,
            latency_ms=120,
        ),
    )
    assert attempt.status == AttemptStatus.SUCCEEDED.value
    assert attempt.asset_path == "generated/a.png"
    assert attempt.cost_cents == 30 and attempt.latency_ms == 120


@pytest.mark.asyncio
async def test_render_image_uses_image_router():
    calls = []

    class FakeRouter:
        async def generate(self, kind, payload, context):
            calls.append((kind, payload, context))
            return ProviderResult(True, ProviderKind.IMAGE, "fake", payload["model"], cost_cents=0)

    result = await render_image(
        FakeRouter(), spec=RenderSpec("project", "run", "shot", 1, 1), payload={"model": "image-v1"}
    )
    assert result.success
    assert calls[0][0] is ProviderKind.IMAGE
    assert calls[0][2].operation_key == "render:shot:1"
