"""长期记忆的状态、共享检索和派生索引重建。"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aureus.common import ManualClock
from aureus.memory import HistoricalMemory, MemoryEntry, MemoryEvidence, MemoryIndex, MemorySource
from aureus.timeline import ActorType, TimelineDatabase

NOW = datetime(2026, 11, 22, tzinfo=UTC)
THEN = NOW - timedelta(days=31)


def source(key: int, table: str = "mai_messages") -> MemorySource:
    return MemorySource("fixture", "main", table, (key,))


def test_shared_history_person_date_active_status_and_rebuild(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    path = tmp_path / "aureus.db"
    timeline = TimelineDatabase(path, clock)
    timeline.open()
    index = MemoryIndex(path, timeline, clock)
    index.open()
    linked = MemoryEvidence("message", "1", source(1))
    orphan = MemoryEvidence("message", "missing")
    records = [
        HistoricalMemory(source(1), "群聊约定木卫二", "阿明", "qq:one", THEN, "秒"),
        HistoricalMemory(source(2), "私聊约定木卫二", "阿明", "qq:one", THEN, "秒"),
        HistoricalMemory(
            source(3), "木卫二约定", "阿明", "qq:one", THEN, "秒", evidence=(linked, orphan)
        ),
        HistoricalMemory(source(4), "同名者聊木卫二", "阿明", "qq:other", THEN),
        HistoricalMemory(source(5), "木卫二旧说法", status="deleted"),
        HistoricalMemory(source(6), "木卫二矛盾说法", status="conflicted"),
        HistoricalMemory(source(7), "木卫二过期说法", valid_to=NOW),
        HistoricalMemory(source(8), "木卫二失活说法", status="inactive"),
        HistoricalMemory(source(9), "木卫二替代说法", status="superseded"),
        HistoricalMemory(source(10), "木卫二生效前说法", valid_from=NOW + timedelta(seconds=1)),
    ]
    assert index.publish(records) == len(records)
    assert index.publish(records) == 0
    assert index.get_history(source(3)) == records[2]
    assert index.get_history(source(5)) == records[4]
    live = timeline.append("message", "新的木卫二计划", ActorType.USER)
    index.index_more(10, lambda row: MemoryEntry("阿明", row.content, "qq:one"))
    assert [hit.record_id for hit in index.search(["木卫二"], (), 10)] == [
        live.id,
        None,
        None,
        None,
        None,
    ]
    filters = {"person_key": "qq:one", "since": THEN, "before": NOW}
    before = index.search(["木卫二"], (), 10, **filters)
    assert [hit.source for hit in before] == [source(3), source(1), source(2)]
    assert before[0].evidence == (linked, orphan)
    assert before[0].sparse_score < before[-1].sparse_score < 0
    assert all(hit.recorded_at == THEN for hit in before)
    index.close()
    with closing(sqlite3.connect(path)) as database:
        database.execute("DROP TABLE memory_history_fts")
    index.open()
    assert index.search(["木卫二"], (), 10, **filters) == before
    index.close()
    timeline.close()


def test_history_source_update_invalidates_recall(tmp_path: Path) -> None:
    path = tmp_path / "aureus.db"
    timeline = TimelineDatabase(path, ManualClock(NOW))
    index = MemoryIndex(path, timeline)
    index.open()
    old = HistoricalMemory(source(1), "过去的木卫二")
    assert index.publish([old]) == 1
    assert index.search(["木卫二"], (), 2)[0].source == source(1)
    deleted = HistoricalMemory(source(1), "过去的木卫二", status="deleted")
    assert index.publish([deleted]) == 1
    assert index.get_history(source(1)) == deleted
    assert not index.search(["木卫二"], (), 2)
    index.close()
