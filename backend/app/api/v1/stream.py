"""广播、游标重放、窗口缺口提示；MySQL 快照是断线恢复的事实来源。"""

import asyncio
import json
import re
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.core.redis_client import consume_progress, progress_bounds
from app.core.response import ok
from app.models.domain import PipelineRun
from app.schemas import RunOut

router = APIRouter()
MAX_STREAM_S = 1800


def _sse(data: dict, event: str, event_id: str | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines) + "\n\n"


def event_position(value: str) -> tuple[int, int]:
    first, second = value.split("-")
    return int(first), int(second)


def cursor_expired(cursor: str, first: str | None, last: str | None) -> bool:
    if cursor == "0-0":
        return False
    if first is None or last is None:
        return True
    return event_position(cursor) < event_position(first) or event_position(cursor) > event_position(last)


async def _event_stream(run_id: str, until_done: bool, cursor: str, snapshot: dict) -> AsyncIterator[str]:
    yield _sse(snapshot, "snapshot")
    deadline = asyncio.get_running_loop().time() + MAX_STREAM_S
    try:
        while asyncio.get_running_loop().time() < deadline:
            first, last = await progress_bounds(run_id)
            if cursor_expired(cursor, first, last):
                # 丢失历史时要求重新读取 MySQL；绝不静默声称完整重放。
                cursor = last or "0-0"
                yield _sse(
                    {"run_id": run_id, "reason": "history_expired", "refresh_required": True}, "reset", cursor
                )
            events = await consume_progress(run_id, cursor)
            if not events:
                yield ": heartbeat\n\n"
            for event_id, raw in events:
                cursor = event_id
                payload = json.loads(raw)
                yield _sse(payload, "progress", event_id)
                if until_done and payload.get("status") in ("completed", "failed", "canceled"):
                    yield _sse({"run_id": run_id, "status": payload["status"]}, "done", event_id)
                    return
            if until_done and snapshot.get("status") in ("completed", "failed", "canceled"):
                yield _sse({"run_id": run_id, "status": snapshot["status"]}, "done", cursor)
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        yield _sse({"run_id": run_id, "message": "进度连接暂不可用，请刷新运行状态"}, "unavailable")


async def run_snapshot(db: AsyncSession, run_id: str) -> dict:
    run = await db.get(PipelineRun, run_id)
    if run is None:
        raise NotFoundError("运行不存在")
    result = RunOut.model_validate(run).model_dump(mode="json")
    await db.commit()  # SSE 长连接期间不持有数据库事务。
    return result


@router.get("/{run_id}", summary="订阅进度，支持 Last-Event-ID 重放")
async def stream_progress(
    run_id: str,
    until_done: bool = Query(default=True),
    last_event_id: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    cursor = last_event_id or "0-0"
    if not re.fullmatch(r"[0-9]{1,20}-[0-9]{1,20}", cursor) or any(
        number > 2**64 - 1 for number in event_position(cursor)
    ):
        raise AppError("Last-Event-ID 不是有效的事件游标")
    snapshot = await run_snapshot(db, run_id)
    return StreamingResponse(
        _event_stream(run_id, until_done, cursor, snapshot),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/{run_id}/snapshot", summary="读取持久运行快照")
async def progress_snapshot(run_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    return ok(await run_snapshot(db, run_id))
