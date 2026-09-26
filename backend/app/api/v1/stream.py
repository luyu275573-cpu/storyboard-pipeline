"""实时进度 SSE 路由。

视频生成是分钟级长任务，前端必须能看到逐镜头进度，否则用户以为卡死了。
Nginx 侧已对本路径关闭 proxy_buffering（见 deploy/nginx.conf），
否则事件会被攒在缓冲区里不下发 —— 这是上一个项目踩过的坑。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.core.redis_client import consume_progress, get_redis, KEY_RUN_PROGRESS

logger = logging.getLogger(__name__)

router = APIRouter()

# SSE 心跳间隔：防中间层（Nginx/云 LB）因空闲超时断开连接
HEARTBEAT_S = 15
# 单次连接最长存活时间，避免僵尸连接累积
MAX_STREAM_S = 1800


def _sse(data: dict, event: str | None = None) -> str:
    """按 SSE 协议格式化。data 必须是单行 JSON（换行会被协议截断）。"""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"


async def _event_stream(run_id: str, until_done: bool) -> AsyncIterator[str]:
    """事件生成器：从 Redis List 阻塞读取进度事件并转发。"""
    r = get_redis()
    pkey = KEY_RUN_PROGRESS.format(run_id=run_id)
    stream_key = f"{pkey}:events"

    # 连接建立即发一次当前快照，前端不必等下一个事件才知道状态
    try:
        snapshot = await r.hgetall(pkey)
        if snapshot:
            yield _sse({"run_id": run_id, "snapshot": snapshot}, event="snapshot")
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取进度快照失败: %s", exc)

    yield _sse({"run_id": run_id, "message": "已连接"}, event="connected")

    elapsed = 0.0
    try:
        while elapsed < MAX_STREAM_S:
            events = await consume_progress(run_id, timeout_s=float(HEARTBEAT_S))
            elapsed += HEARTBEAT_S

            if events:
                for raw in events:
                    try:
                        yield _sse(json.loads(raw), event="progress")
                    except json.JSONDecodeError:
                        logger.warning("进度事件不是合法 JSON，已跳过: %s", raw[:120])
            else:
                # 心跳：注释行，SSE 规范允许，客户端会忽略但连接保活
                yield ": heartbeat\n\n"

            # 终态判定：run 结束则收尾退出
            state = await r.hget(pkey, "__status__")
            if state in ("completed", "failed", "canceled", "suspended"):
                yield _sse({"run_id": run_id, "status": state}, event="done")
                if until_done:
                    break
    except asyncio.CancelledError:
        # 客户端断开是正常情况，记 debug 即可，不要当错误刷日志
        logger.debug("SSE 客户端断开 run_id=%s", run_id)
        raise
    finally:
        logger.info("SSE 连接结束 run_id=%s elapsed=%.0fs", run_id, elapsed)


@router.get("/{run_id}", summary="订阅流水线实时进度")
async def stream_progress(
    run_id: str,
    until_done: bool = Query(default=True, description="运行结束后是否自动关闭连接"),
) -> StreamingResponse:
    """SSE 进度流。

    响应头说明：
      Cache-Control: no-cache     禁止缓存事件流
      X-Accel-Buffering: no       显式告知 Nginx 不缓冲（双保险，配置里也关了）
      Connection: keep-alive      保持长连接
    """
    return StreamingResponse(
        _event_stream(run_id, until_done),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/{run_id}/snapshot", summary="进度快照（非流式）")
async def progress_snapshot(run_id: str) -> dict:
    """一次性读取当前进度，供轮询或断线重连后补状态。

    已实现：纯读 Redis，无副作用。
    """
    r = get_redis()
    pkey = KEY_RUN_PROGRESS.format(run_id=run_id)
    data = await r.hgetall(pkey)
    return {
        "code": "OK",
        "message": "success",
        "data": {"run_id": run_id, "stages": data, "status": data.get("__status__")},
    }
