"""真实临时 SQLite 的增量索引、转义、排序和事务行为（D80）。"""

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.memory import MemoryEntry, MemoryIndex
from aureus.timeline import ActorType, TimelineDatabase, TimelineRecord


@pytest.fixture
def opened(tmp_path: Path) -> Iterator[tuple[TimelineDatabase, MemoryIndex]]:
    timeline = TimelineDatabase(
        tmp_path / "aureus.db", ManualClock(datetime(2026, 11, 20, tzinfo=UTC))
    )
    timeline.open()
    index = MemoryIndex(tmp_path / "aureus.db", timeline)
    index.open()
    yield timeline, index
    index.close()
    timeline.close()


def describe(record: TimelineRecord) -> MemoryEntry | None:
    return MemoryEntry("用户", record.content) if record.event_type == "diary_written" else None


def test_incremental_batches_skip_and_resume_cursor(
    opened: tuple[TimelineDatabase, MemoryIndex],
) -> None:
    timeline, index = opened
    first = timeline.append("diary_written", "木卫二有冰下海洋", ActorType.USER)
    skipped = timeline.append("state", "不收", ActorType.SYSTEM)
    assert index.index_more(1, describe) == 1
    assert [h.record_id for h in index.search(["冰下海洋"], (), 5)] == [first.id]
    assert index.index_more(1, describe) == 1
    index.close()
    index.open()
    seen: list[int] = []

    def counted(record: TimelineRecord) -> MemoryEntry | None:
        seen.append(record.id)
        return describe(record)

    second = timeline.append("diary_written", "今晚看海洋纪录片", ActorType.USER)
    assert index.index_more(10, counted) == 1
    assert seen == [second.id] and skipped.id not in seen
    assert [h.record_id for h in index.search(["海洋"], (), 5)] == [second.id, first.id]
    assert index.index_more(10, counted) == 0
    assert not index.search(["不收"], (), 5)


@pytest.mark.parametrize("keyword", ["木卫二", "海洋", 'a"b', "%木", "_木", "!木", '"木'])
def test_keywords_are_literal_and_searchable(
    opened: tuple[TimelineDatabase, MemoryIndex], keyword: str
) -> None:
    timeline, index = opened
    expected = timeline.append("diary_written", f"只有这里含有{keyword}字样", ActorType.USER)
    timeline.append("diary_written", "假木和别的字样", ActorType.USER)
    index.index_more(20, describe)
    hits = index.search([keyword], (), 5)
    assert [h.record_id for h in hits] == [expected.id]


def test_or_exclusion_newest_limit_and_truncation(
    opened: tuple[TimelineDatabase, MemoryIndex],
) -> None:
    timeline, index = opened
    first = timeline.append("diary_written", "木卫二" + "长" * 250, ActorType.USER)
    second = timeline.append("diary_written", "晚上海洋", ActorType.USER)
    third = timeline.append("diary_written", "海洋木卫二", ActorType.USER)
    index.index_more(100, describe)
    assert [h.record_id for h in index.search(["木卫二", "海洋"], (), 10)] == [
        third.id,
        second.id,
        first.id,
    ]
    hits = index.search(["木卫二", "海洋"], [third.id], 1)
    assert [h.record_id for h in hits] == [second.id]
    [hit] = index.search(["木卫二"], [third.id], 5)
    assert len(hit.text) == 180 and hit.actor_label == "用户" and hit.recorded_at == first.timestamp
    assert index.search([], (), 5) == []


def test_failed_description_rolls_back_entries_and_cursor(
    opened: tuple[TimelineDatabase, MemoryIndex],
) -> None:
    timeline, index = opened
    first = timeline.append("diary_written", "第一次木卫二", ActorType.USER)
    timeline.append("diary_written", "第二次木卫二", ActorType.USER)

    def broken(record: TimelineRecord) -> MemoryEntry:
        if record.id != first.id:
            raise OSError("假档案故障")
        return MemoryEntry("用户", record.content)

    with pytest.raises(OSError):
        index.index_more(20, broken)
    assert not index.search(["木卫二"], (), 5)
    assert index.index_more(20, describe) == 2
    assert len(index.search(["木卫二"], (), 5)) == 2


@pytest.mark.asyncio
async def test_search_opens_connection_in_worker(
    opened: tuple[TimelineDatabase, MemoryIndex],
) -> None:
    timeline, index = opened
    timeline.append("diary_written", "后台查木卫二", ActorType.USER)
    index.index_more(20, describe)
    assert len(await asyncio.to_thread(index.search, ["木卫二"], (), 5)) == 1


def test_old_index_migration_and_rebuild_preserve_consolidation_progress(
    opened: tuple[TimelineDatabase, MemoryIndex], tmp_path: Path
) -> None:
    timeline, index = opened
    timeline.append("state", "未索引事件", ActorType.SYSTEM)
    first = timeline.append("diary_written", "第一条经历", ActorType.USER)
    index.index_more(10, describe)
    index.close()
    with closing(sqlite3.connect(tmp_path / "aureus.db")) as conn, conn:
        conn.execute("ALTER TABLE memory_index_state DROP COLUMN stable_rowid")
        conn.execute("UPDATE memory_index SET rowid=1 WHERE record_id=?", (first.id,))
    index.open()
    assert index.index_more(10, describe) == 0
    batch = index.consolidation_batch(1)
    assert batch is not None and batch.end == (0, first.id)
    assert index.save_consolidation(batch, [])
    assert not index.save_consolidation(batch, [])
    index.close()
    with closing(sqlite3.connect(tmp_path / "aureus.db")) as conn, conn:
        conn.execute("DROP TABLE memory_index")
    index.open()
    assert index.index_more(10, describe) == 2
    assert index.consolidation_batch(1) is None
    second = timeline.append("diary_written", "第二条经历", ActorType.USER)
    assert index.index_more(10, describe) == 1
    batch = index.consolidation_batch(1)
    assert batch is not None
    assert [item.reference for item in batch.items] == [f"timeline:{second.id}"]
