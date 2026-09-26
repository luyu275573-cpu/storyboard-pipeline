"""Redis Stream 广播、重放及保留窗口恢复。"""

import asyncio
import json

import pytest

pytestmark = pytest.mark.integration


async def test_broadcast_and_replay(redis_test):
    from app.core.redis_client import consume_progress, publish_progress

    first = await publish_progress("run", "character", {"status": "waiting_gate"})
    left, right = await asyncio.gather(consume_progress("run"), consume_progress("run"))
    assert left == right and left[0][0] == first
    second = await publish_progress("run", "storyboard", {"status": "waiting_gate"})
    replay = await consume_progress("run", first)
    assert len(replay) == 1 and replay[0][0] == second
    assert json.loads(replay[0][1])["stage"] == "storyboard"
    assert await consume_progress("run", second, timeout_s=0.001) == []


async def test_retention_gap_and_reset_event(redis_test):
    from app.api.v1.stream import _event_stream, cursor_expired
    from app.core.redis_client import progress_bounds, progress_keys, publish_progress

    first = await publish_progress("run", "storyboard", {"status": "waiting_gate"})
    last = first
    for i in range(501):
        last = await publish_progress("run", "storyboard", {"value": i})
    head, tail = await progress_bounds("run")
    assert tail == last and cursor_expired(first, head, tail)
    assert not cursor_expired(last, head, tail)
    assert await redis_test.xlen(progress_keys("run")[1]) == 500
    stream = _event_stream("run", False, first, {"status": "waiting_gate"})
    assert "event: snapshot" in await anext(stream)
    reset = await anext(stream)
    assert "event: reset" in reset and f"id: {last}" in reset
    await stream.aclose()
    await redis_test.delete(*progress_keys("run"))
    assert cursor_expired(last, *(await progress_bounds("run")))


async def test_sse_contains_ids_and_terminal_event(redis_test):
    from app.api.v1.stream import _event_stream
    from app.core.redis_client import publish_progress

    cursor = await publish_progress("finished", "metrics", {"status": "completed", "message": "一行\n两行"})
    chunks = [chunk async for chunk in _event_stream("finished", True, "0-0", {"status": "running"})]
    assert len(chunks) == 3
    assert "event: progress" in chunks[1] and f"id: {cursor}" in chunks[1]
    assert "一行\\n两行" in chunks[1]
    assert "event: done" in chunks[2]
