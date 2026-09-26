from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from app.core.config import settings
from app.models.tracking import ApiCallLog
from app.providers.siliconflow import (
    SiliconFlowImageProvider,
    SiliconFlowLLMProvider,
    SiliconFlowVisionProvider,
)


def _png() -> bytes:
    image = Image.new("RGB", (2, 2), "navy")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(settings, "siliconflow_api_key", "test-key")
    monkeypatch.setattr(settings, "siliconflow_base_url", "https://sf.test/v1")
    monkeypatch.setattr(settings, "storage_root", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_image_provider_decodes_base64_and_stores_asset(configured):
    encoded = base64.b64encode(_png()).decode("ascii")
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"data": [{"b64_json": encoded}]})

    provider = SiliconFlowImageProvider(object())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await provider._call(
            {"model": "Qwen/Qwen-Image-Edit-2509", "prompt": "test"},
            client,
            ApiCallLog(),
        )

    assert result.success and result.asset_path and result.asset_sha256
    assert (configured / result.asset_path).is_file()
    assert seen == {"path": "/v1/images/generations", "auth": "Bearer test-key"}


@pytest.mark.asyncio
async def test_vision_provider_sends_multimodal_json(configured, tmp_path):
    image_path = tmp_path / "frame.png"
    image_path.write_bytes(_png())
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"verdict":"pass"}'}}]},
        )

    provider = SiliconFlowVisionProvider(object())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await provider._call(
            {
                "model": "Qwen/Qwen3-VL-8B-Instruct",
                "system_prompt": "system",
                "user_prompt": "inspect",
                "image_paths": [str(image_path)],
            },
            client,
            ApiCallLog(),
        )

    assert result.success and result.text == '{"verdict":"pass"}'
    assert seen["body"]["messages"][1]["content"][-1] == {"type": "text", "text": "inspect"}
    assert seen["body"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_llm_provider_returns_text(configured):
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"scenes":[]}'}}]},
        )

    provider = SiliconFlowLLMProvider(object())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await provider._call(
            {"model": "Qwen/Qwen3-32B", "messages": [{"role": "user", "content": "x"}]},
            client,
            ApiCallLog(),
        )

    assert result.success and result.text == '{"scenes":[]}'
