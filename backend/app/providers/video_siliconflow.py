"""Wan I2V: submit once, persist the task, recover by querying only."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings
from app.core.errors import AppError, ProviderRejectedError
from app.models.enums import ProviderKind
from app.models.tracking import ApiCallLog
from app.providers.base import BaseProvider, ProviderResult
from app.providers.siliconflow import _data_uri


async def probe_video(path: Path) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        settings.ffprobe_bin,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 30)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise AppError("视频文件无法解码")
    data = json.loads(stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    duration = float(data.get("format", {}).get("duration", 0))
    if not video or not 0 < duration <= 120:
        raise AppError("视频缺少有效画面或时长")
    return {
        "duration_ms": round(duration * 1000),
        "width": video["width"],
        "height": video["height"],
        "codec": video["codec_name"],
    }


class SiliconFlowVideoProvider(BaseProvider):
    name = "siliconflow"
    kind = ProviderKind.VIDEO

    def is_configured(self) -> bool:
        return bool(settings.siliconflow_api_key and settings.siliconflow_base_url)

    def estimate_cost(self, payload: dict[str, Any]) -> int:
        if payload.get("model") != "Wan-AI/Wan2.2-I2V-A14B":
            raise AppError("视频报价仅适用于 Wan2.2-I2V-A14B")
        if payload.get("image_size") not in {"1280x720", "720x1280", "960x960"}:
            raise AppError("不支持的视频尺寸")
        return settings.siliconflow_video_price_cents

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {settings.siliconflow_api_key}"}

    async def _call(
        self, payload: dict[str, Any], client: httpx.AsyncClient, call: ApiCallLog
    ) -> ProviderResult:
        request = {key: payload[key] for key in ("model", "prompt", "image_size")}
        request["image"] = await _data_uri(payload["image_path"])
        request["negative_prompt"] = payload.get("negative_prompt", "")
        request["seed"] = int(hashlib.sha256(str(payload["seed"]).encode()).hexdigest()[:8], 16)
        response = await client.post(
            f"{settings.siliconflow_base_url.rstrip('/')}/video/submit",
            headers={**self._headers(), "X-Trace-Id": str(call.idempotency_key)},
            json=request,
        )
        if response.status_code in {400, 401, 403, 404, 422, 429}:
            raise ProviderRejectedError("视频请求未受理", detail={"status": response.status_code})
        response.raise_for_status()
        task_id = response.json().get("requestId")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
            raise AppError("视频响应缺少任务号，禁止自动重发")
        await self.cost.attach_task(call.id, task_id)
        call.provider_task_id = task_id
        while True:
            result = await self._query(client, call)
            if result is not None:
                return result
            await asyncio.sleep(5)

    async def _query(self, client: httpx.AsyncClient, call: ApiCallLog) -> ProviderResult | None:
        if not call.provider_task_id:
            return None
        response = await client.post(
            f"{settings.siliconflow_base_url.rstrip('/')}/video/status",
            headers=self._headers(),
            json={"requestId": call.provider_task_id},
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") in {"InQueue", "InProgress"}:
            return None
        if data.get("status") != "Succeed":
            # Failed does not prove zero billing. Retain reservation for reconciliation.
            raise AppError("供应商视频未成功，需核实任务和账单；禁止自动重发")
        url = data["results"]["videos"][0]["url"]
        if not isinstance(url, str) or not url.startswith("https://"):
            raise AppError("视频下载地址无效")
        downloaded = await client.get(url, follow_redirects=True)
        downloaded.raise_for_status()
        raw = downloaded.content
        digest = self.sha256_bytes(raw)
        relative = f"generated/{digest}.mp4"
        path = settings.storage_path / relative
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, raw)
        metadata = await probe_video(path)
        return ProviderResult(
            True,
            self.kind,
            self.name,
            call.model,
            asset_path=relative,
            asset_sha256=digest,
            cost_cents=call.quoted_cents,
            parsed={
                **metadata,
                "billing_source": "catalog_quote_pending_invoice",
                "provider_task_id": call.provider_task_id,
            },
        )
