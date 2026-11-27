"""时间线读写离开事件循环，提交和通知仍各自按追加顺序。"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, get_ident

import pytest

from aureus.common import ManualClock
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, TimelineRecord


@pytest.fixture
def timeline(tmp_path: Path) -> Iterator[TimelineDatabase]:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    timeline = TimelineDatabase(tmp_path / "timeline.sqlite", clock)
    timeline.open()
    try:
        yield timeline
    finally:
        timeline.close()


@pytest.mark.asyncio
async def test_async_append_preserves_order(timeline: TimelineDatabase) -> None:
    seen: list[TimelineRecord] = []
    timeline.add_append_listener(seen.append)
    records = await asyncio.gather(
        *(timeline.async_append("message", str(index), ActorType.USER) for index in range(50))
    )
    assert [row.id for row in records] == list(range(1, 51))
    assert [row.content for row in timeline.query(TimelineQuery())] == [
        str(index) for index in range(50)
    ]
    assert seen == records


@pytest.mark.asyncio
async def test_append_listener_runs_on_event_loop(timeline: TimelineDatabase) -> None:
    notifications: list[tuple[int, int, asyncio.AbstractEventLoop]] = []
    loop = asyncio.get_running_loop()
    thread = get_ident()

    def on_append(record: TimelineRecord) -> None:
        notifications.append((record.id, get_ident(), asyncio.get_running_loop()))

    timeline.add_append_listener(on_append)
    async_record = await timeline.async_append("message", "一", ActorType.USER)
    sync_record = await asyncio.to_thread(timeline.append, "message", "二", ActorType.USER)
    assert notifications == [(async_record.id, thread, loop), (sync_record.id, thread, loop)]


@pytest.mark.asyncio
async def test_event_loop_not_blocked_by_slow_db(
    timeline: TimelineDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = Event()
    read = timeline.query

    def slow_read(query: TimelineQuery) -> list[TimelineRecord]:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return read(query)

    monkeypatch.setattr(timeline, "query", slow_read)
    pending = asyncio.create_task(timeline.async_query(TimelineQuery()))
    try:
        async with asyncio.timeout(5):
            await started.wait()
            row = await timeline.async_append("message", "查询等待时仍可追加", ActorType.USER)
            assert not pending.done()
    finally:
        release.set()
        await pending
    assert pending.result() == [row]


@pytest.mark.asyncio
async def test_concurrent_read_write(timeline: TimelineDatabase) -> None:
    async def observe() -> None:
        for _ in range(30):
            ids = [row.id for row in await timeline.async_query(TimelineQuery(limit=-1))]
            assert ids == list(range(1, len(ids) + 1))

    await asyncio.gather(
        observe(),
        observe(),
        observe(),
        *(timeline.async_append("message", str(index), ActorType.USER) for index in range(50)),
    )
    assert timeline.count_records() == 50
