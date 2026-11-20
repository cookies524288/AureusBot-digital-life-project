"""日程存储的测试（路线图 2.7，D74）：记、查、到期、结束、不许改、上下文摘要。"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from aureus.agenda import (
    AgendaError,
    AgendaStore,
    ItemStatus,
    describe_agenda,
    parse_due,
)
from aureus.common import AgendaConfig, ManualClock

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
CHINA = timezone(timedelta(hours=8))


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: ManualClock) -> Iterator[AgendaStore]:
    opened = AgendaStore(tmp_path / "aureus.db", clock, AgendaConfig(max_open_per_person=3))
    opened.open()
    yield opened
    opened.close()


def _add(
    store: AgendaStore, person: int = 1, due: datetime | None = None, what: str = "帮他查机票"
):
    item = store.add(person, what, due_at=due, source_record_id=9, feedback_id=4)
    assert item is not None
    return item


def test_add_and_read_back(store: AgendaStore) -> None:
    item = _add(store, due=NOW + timedelta(hours=2))
    assert item.status is ItemStatus.OPEN
    assert item.content == "帮他查机票"
    assert item.due_at == NOW + timedelta(hours=2)
    assert (item.source_record_id, item.feedback_id) == (9, 4)
    assert store.get(item.id) == item


def test_content_is_flattened_and_capped(store: AgendaStore) -> None:
    item = _add(store, what="  第一行\n第二行  " + "很" * 300)
    assert item.content.startswith("第一行 第二行")
    assert len(item.content) == 200
    with pytest.raises(AgendaError):
        store.add(1, "   ", due_at=None, source_record_id=None, feedback_id=None)


def test_open_items_per_person_are_capped(store: AgendaStore) -> None:
    for _ in range(3):
        _add(store)
    assert store.add(1, "第四件", due_at=None, source_record_id=None, feedback_id=None) is None
    assert _add(store, person=2) is not None  # 别人不受影响


def test_open_items_sorted_by_due_with_undated_last(store: AgendaStore) -> None:
    undated = _add(store)
    late = _add(store, due=NOW + timedelta(days=2))
    soon = _add(store, due=NOW + timedelta(hours=1))
    _add(store, person=2)
    assert [item.id for item in store.open_items(1)] == [soon.id, late.id, undated.id]
    assert len(store.open_items()) == 4


def test_finish_only_once_and_dropped_needs_a_reason(store: AgendaStore) -> None:
    item = _add(store)
    with pytest.raises(AgendaError):
        store.finish(item.id, ItemStatus.DROPPED, "  ")
    with pytest.raises(AgendaError):
        store.finish(item.id, ItemStatus.OPEN)
    done = store.finish(item.id, ItemStatus.DONE)
    assert done is not None and done.status is ItemStatus.DONE and done.closed_at == NOW
    assert store.finish(item.id, ItemStatus.DROPPED, "改主意") is None
    assert store.finish(999, ItemStatus.DONE) is None
    assert store.open_items(1) == []


def test_due_unnoticed_is_reported_once(store: AgendaStore, clock: ManualClock) -> None:
    item = _add(store, due=NOW + timedelta(hours=1))
    assert store.due_unnoticed(clock.now()) == []
    clock.advance(timedelta(hours=2))
    [due] = store.due_unnoticed(clock.now())
    assert due.id == item.id and due.is_due(clock.now())
    store.mark_due_noticed(item.id)
    assert store.due_unnoticed(clock.now()) == []


def test_overdue_after_grace(store: AgendaStore, clock: ManualClock) -> None:
    item = _add(store, due=NOW + timedelta(hours=1))
    _add(store)  # 没有到期时间的永远不过期
    clock.advance(timedelta(days=3))
    assert store.overdue(clock.now()) == []
    clock.advance(timedelta(hours=2))
    assert [found.id for found in store.overdue(clock.now())] == [item.id]


def test_database_forbids_rewriting_and_deleting(store: AgendaStore, tmp_path: Path) -> None:
    item = _add(store)
    store.finish(item.id, ItemStatus.DONE)
    conn = sqlite3.connect(tmp_path / "aureus.db")
    try:
        for sql in (
            "UPDATE agenda_items SET content = 'x'",
            "UPDATE agenda_items SET status = 'open'",
            "UPDATE agenda_items SET due_at = '2030-01-01'",
            "DELETE FROM agenda_items",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-21 09:30", datetime(2026, 11, 21, 1, 30, tzinfo=UTC)),
        (" 2026-11-20 20:00 ", datetime(2026, 11, 20, 12, 0, tzinfo=UTC)),  # 正好是现在
        ("2026-11-20 19:59", None),  # 已经过去
        ("2027-03-01 10:00", None),  # 超过 90 天
        ("明天下午", None),
        ("2026/11/21 09:30", None),
        (None, None),
        (20261121, None),
    ],
)
def test_parse_due(text: object, expected: datetime | None) -> None:
    assert parse_due(text, NOW, CHINA, 90) == expected


def test_describe_agenda_lists_items_and_marks_due(store: AgendaStore, clock: ManualClock) -> None:
    due = _add(store, due=NOW + timedelta(hours=1), what="提醒他吃药")
    later = _add(store, due=NOW + timedelta(days=1), what="一起看电影")
    undated = _add(store, what="推荐一本书")
    clock.advance(timedelta(hours=2))
    text = describe_agenda(store.open_items(1), clock.now(), CHINA, limit=2)
    assert f"#{due.id} 提醒他吃药" in text and "已经到期" in text
    assert f"#{later.id} 一起看电影（到期 2026-11-21 20:00）" in text
    assert f"#{undated.id}" not in text and "还有 1 条" in text
    assert describe_agenda([], NOW, CHINA, limit=5) == ""
