"""SiliconFlow adapters for image generation and visual QC."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import logging
import mimetypes
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import settings
from app.core.errors import AppError, ProviderRejectedError
from app.models.enums import ProviderKind
from app.models.tracking import ApiCallLog
from app.providers.base import BaseProvider, ProviderResult

logger = logging.getLogger(__name__)


async def _data_uri(path: str) -> str:
    file_path = Path(path)
    if not await asyncio.to_thread(file_path.is_file):
        file_path = settings.storage_path / path
    if not await asyncio.to_thread(file_path.is_file):
        raise AppError("模型输入图片不存在")
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    raw = await asyncio.to_thread(file_path.read_bytes)
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def _store_generated(data: bytes) -> tuple[str, str]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            clean = ImageOps.exif_transpose(image).convert("RGB")
            output = io.BytesIO()
            clean.save(output, format="PNG")
            normalized = output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AppError("硅基流动返回的图片无法解码") from exc
    digest = hashlib.sha256(normalized).hexdigest()
    relative = f"generated/{digest}.png"
    target = settings.storage_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(normalized)
    return relative, digest


class SiliconFlowImageProvider(BaseProvider):
    name = "siliconflow"
    kind = ProviderKind.IMAGE

    def is_configured(self) -> bool:
        return bool(settings.siliconflow_api_key and settings.siliconflow_base_url)

    def estimate_cost(self, payload: dict[str, Any]) -> int:
        # Qwen-Image 系列当前按图计费，公开目录价为 ¥0.30/张。
        return 30 * max(1, int(payload.get("n", 1)))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.siliconflow_api_key}",
            "Content-Type": "application/json",
        }

    async def _payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Provider 请求只保留硅基流动图像接口字段；锚定等级、重试字段和 n
        # 已写入 RenderAttempt.request_payload，不应泄漏到供应商 schema。
        allowed = {
            "model",
            "prompt",
            "image_size",
            "num_inference_steps",
            "guidance_scale",
            "true_cfg_scale",
            "enable_safety_checker",
        }
        request = {key: value for key, value in payload.items() if key in allowed}
        paths = payload.get("image_paths") or []
        if paths:
            if not isinstance(paths, list) or len(paths) > 3:
                raise AppError("参考图最多发送 3 张")
            for index, path in enumerate(paths):
                request["image" if index == 0 else f"image{index + 1}"] = await _data_uri(str(path))
        if isinstance(payload.get("seed"), str):
            request["seed"] = int(hashlib.sha256(payload["seed"].encode()).hexdigest()[:8], 16)
        elif isinstance(payload.get("seed"), int):
            request["seed"] = payload["seed"]
        request.setdefault("model", settings.siliconflow_image_model)
        request.setdefault("prompt", "")
        return request

    async def _call(
        self, payload: dict[str, Any], client: httpx.AsyncClient, call: ApiCallLog
    ) -> ProviderResult:
        response = await client.post(
            f"{settings.siliconflow_base_url.rstrip('/')}/images/generations",
            headers=self._headers(),
            json=await self._payload(payload),
        )
        if response.status_code in {400, 401, 403, 404, 429}:
            logger.warning(
                "硅基流动图像请求被拒绝 status=%s body=%s",
                response.status_code,
                response.text[:500],
            )
            raise ProviderRejectedError("硅基流动图片请求未受理", detail={"status": response.status_code})
        response.raise_for_status()
        body = response.json()
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise AppError("硅基流动图片响应缺少 data")
        item = items[0]
        if isinstance(item.get("b64_json"), str):
            raw = base64.b64decode(item["b64_json"], validate=True)
        elif isinstance(item.get("url"), str):
            downloaded = await client.get(item["url"])
            downloaded.raise_for_status()
            raw = downloaded.content
        else:
            raise AppError("硅基流动图片响应缺少 url 或 b64_json")
        asset_path, digest = await asyncio.to_thread(_store_generated, raw)
        return ProviderResult(
            True,
            self.kind,
            self.name,
            str(payload.get("model", settings.siliconflow_image_model)),
            asset_path=asset_path,
            asset_sha256=digest, cost_cents=self.estimate_cost(payload),
        )


class SiliconFlowVisionProvider(BaseProvider):
    name = "siliconflow"
    kind = ProviderKind.VISION

    def is_configured(self) -> bool:
        return bool(settings.siliconflow_api_key and settings.siliconflow_base_url)

    def estimate_cost(self, payload: dict[str, Any]) -> int:
        # 视觉输入按 token 计费；1 分是当前单张关键帧 QC 的保守上界。
        return 1

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.siliconflow_api_key}",
            "Content-Type": "application/json",
        }

    async def _payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        paths = payload.get("image_paths") or []
        content: list[dict[str, Any]] = [
            {"type": "image_url", "image_url": {"url": await _data_uri(str(path))}}
            for path in paths
        ]
        content.append({"type": "text", "text": str(payload.get("user_prompt", ""))})
        return {
            "model": payload.get("model", settings.siliconflow_vision_model),
            "messages": [
                {"role": "system", "content": str(payload.get("system_prompt", ""))},
                {"role": "user", "content": content},
            ],
            "max_tokens": int(payload.get("max_tokens", 1200)),
            "response_format": {"type": "json_object"},
        }

    async def _call(
        self, payload: dict[str, Any], client: httpx.AsyncClient, call: ApiCallLog
    ) -> ProviderResult:
        response = await client.post(
            f"{settings.siliconflow_base_url.rstrip('/')}/chat/completions",
            headers=self._headers(),
            json=await self._payload(payload),
        )
        if response.status_code in {400, 401, 403, 404, 429}:
            raise ProviderRejectedError("硅基流动视觉请求未受理", detail={"status": response.status_code})
        response.raise_for_status()
        body = response.json()
        try:
            message = body["choices"][0]["message"]
            text = message.get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise AppError("硅基流动视觉响应缺少 choices") from exc
        parsed = None
        if isinstance(text, dict):
            parsed = text
            text = ""
        return ProviderResult(
            True,
            self.kind,
            self.name,
            str(payload.get("model", settings.siliconflow_vision_model)),
            text=str(text),
            parsed=parsed,
            cost_cents=self.estimate_cost(payload),
        )

    async def judge(
        self, *, system_prompt: str, user_prompt: str, image_paths: list[str]
    ) -> ProviderResult:
        """兼容 QCAgent 的直接调用接口；正式流水线应通过 ProviderRouter 记账。"""
        payload = {
            "model": settings.siliconflow_vision_model,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "image_paths": image_paths,
        }
        async with httpx.AsyncClient(timeout=settings.provider_timeout_s) as client:
            return await self._call(payload, client, ApiCallLog())


class SiliconFlowLLMProvider(BaseProvider):
    name = "siliconflow"
    kind = ProviderKind.LLM

    def is_configured(self) -> bool:
        return bool(settings.siliconflow_api_key and settings.siliconflow_base_url)

    def estimate_cost(self, payload: dict[str, Any]) -> int:
        return 2

    async def _call(
        self, payload: dict[str, Any], client: httpx.AsyncClient, call: ApiCallLog
    ) -> ProviderResult:
        body = {
            "model": payload.get("model", settings.siliconflow_llm_model),
            "messages": payload.get("messages", []),
            "max_tokens": int(payload.get("max_tokens", 1200)),
            "response_format": {"type": "json_object"},
        }
        response = await client.post(
            f"{settings.siliconflow_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.siliconflow_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if response.status_code in {400, 401, 403, 404, 429}:
            raise ProviderRejectedError("硅基流动文本请求未受理", detail={"status": response.status_code})
        response.raise_for_status()
        try:
            text = response.json()["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise AppError("硅基流动文本响应缺少 choices") from exc
        return ProviderResult(
            True,
            self.kind,
            self.name,
            str(body["model"]),
            text=str(text),
            cost_cents=self.estimate_cost(payload),
        )
