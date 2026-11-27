"""SQLite 连接的测试：被别的进程占着写锁时，等锁有上限（审查 M2）。"""

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Thread, current_thread
from typing import Any

import pytest

from aureus.common import BudgetCalendar, ManualClock
from aureus.common import sqlite as sqlite_support
from aureus.heartbeat import MessageRoute, Perception, TrustLevel
from aureus.heartbeat import inbox_store as inbox_module
from aureus.heartbeat.inbox_store import InboxStore
from aureus.organs import CallAttempt
from aureus.safety import Ledger
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope

START = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
# 测试里把等锁时间缩到很短，省得每个用例白等一秒。
SHORT_TIMEOUT_SECONDS = 0.1
# 允许比等锁上限多出的时间：只是给慢机器留余地，远小于原来的 5 秒默认值。
SLACK_SECONDS = 1.0


@contextmanager
def write_lock_held_elsewhere(db_path: Path) -> Iterator[None]:
    """另一个连接占着写锁不放，模拟别的进程在导出、导入或检查数据库。"""
    holder = sqlite3.connect(str(db_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        yield
    finally:
        holder.close()


@pytest.fixture
def short_busy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sqlite_support, "SQLITE_BUSY_TIMEOUT_SECONDS", SHORT_TIMEOUT_SECONDS)


def test_default_busy_timeout_is_shorter_than_one_heartbeat_tick() -> None:
    assert sqlite_support.SQLITE_BUSY_TIMEOUT_SECONDS <= 2.0  # 默认的 5 秒会顶掉一拍


def test_open_connection_returns_rows_addressable_by_name(tmp_path: Path) -> None:
    with closing(sqlite_support.open_connection(tmp_path / "x.db")) as conn:
        row = conn.execute("SELECT 7 AS seven").fetchone()
    assert row["seven"] == 7


@pytest.mark.usefixtures("short_busy_timeout")
def test_timeline_append_gives_up_when_the_write_lock_is_held(tmp_path: Path) -> None:
    db_path = tmp_path / "aureus.db"
    timeline = TimelineDatabase(db_path, clock=ManualClock(START))
    timeline.open()
    try:
        with write_lock_held_elsewhere(db_path):
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                timeline.append(event_type="message", content="你好", actor_type=ActorType.ME)
            assert time.monotonic() - started < SHORT_TIMEOUT_SECONDS + SLACK_SECONDS
        # 锁放开之后可以照常写：失败没有把连接弄坏。
        timeline.append(event_type="message", content="再来", actor_type=ActorType.ME)
    finally:
        timeline.close()


@pytest.mark.usefixtures("short_busy_timeout")
def test_ledger_record_gives_up_when_the_write_lock_is_held(tmp_path: Path) -> None:
    db_path = tmp_path / "aureus.db"
    ledger = Ledger(db_path, ManualClock(START), calendar=BudgetCalendar(UTC))
    ledger.open()
    try:
        attempt = CallAttempt(model="m", provider="p", prompt_tokens=1, completion_tokens=1)
        with write_lock_held_elsewhere(db_path):
            started = time.monotonic()
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                ledger.record("replyer", [attempt])
            assert time.monotonic() - started < SHORT_TIMEOUT_SECONDS + SLACK_SECONDS
        ledger.record("replyer", [attempt])
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_timeline_uses_one_writer_and_separate_read_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeline = TimelineDatabase(tmp_path / "timeline.db", ManualClock(START))
    timeline.open()
    writer_connections: list[tuple[Thread, sqlite3.Connection]] = []
    original_connection = timeline._connection
    readers_ready = Barrier(2)

    def track_connection(*, read_only: bool = True) -> sqlite3.Connection:
        connection = original_connection(read_only=read_only)
        if not read_only:
            writer_connections.append((current_thread(), connection))
        return connection

    def read() -> tuple[Thread, sqlite3.Connection]:
        result = current_thread(), original_connection()
        readers_ready.wait(timeout=5)
        timeline.query(TimelineQuery())
        assert original_connection() is result[1]
        return result

    monkeypatch.setattr(timeline, "_connection", track_connection)
    try:
        reader_tasks = [asyncio.create_task(timeline.async_read(read)) for _ in range(2)]
        records = await asyncio.gather(
            *(timeline.async_append("message", str(index), ActorType.ME) for index in range(8))
        )
        reader_connections = await asyncio.gather(*reader_tasks)
        assert [record.id for record in records] == list(range(1, 9))
        assert len(writer_connections) == 8
        assert len(set(writer_connections)) == 1
        workers = {*writer_connections, *reader_connections}
        assert len({thread for thread, _ in workers}) == 3
        assert len({connection for _, connection in workers}) == 3
        assert all(thread is not current_thread() and thread.is_alive() for thread, _ in workers)
    finally:
        timeline.close()

    for thread, connection in workers:
        assert not thread.is_alive()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


@pytest.mark.asyncio
async def test_inbox_operations_open_and_close_each_connection_on_the_calling_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "inbox.db"
    store = InboxStore(path, ManualClock(START))
    writers_ready = Barrier(2)

    class TrackedConnection(sqlite3.Connection):
        owner: Thread
        closed_on: Thread | None = None
        immediate = False

        def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
            if sql == "BEGIN IMMEDIATE":
                self.immediate = True
                writers_ready.wait(timeout=5)
            return super().execute(sql, *args)

        def close(self) -> None:
            super().close()
            self.closed_on = current_thread()

    connections: list[TrackedConnection] = []

    def connect(db_path: Path, *, read_only: bool = False) -> TrackedConnection:
        connection = sqlite3.connect(db_path, factory=TrackedConnection)
        connection.row_factory = sqlite3.Row
        connection.owner = current_thread()
        connections.append(connection)
        return connection

    def incoming(identifier: str) -> Perception:
        return Perception(
            "qq",
            "fixture-sender",
            identifier,
            VisibilityScope.GROUP,
            TrustLevel.VERIFIED_CONTACT,
            route=MessageRoute("fixture-bot", "group", "fixture-group", identifier),
        )

    monkeypatch.setattr(inbox_module, "open_connection", connect)
    messages = await asyncio.gather(
        asyncio.to_thread(store.add, incoming("first")),
        asyncio.to_thread(store.add, incoming("second")),
    )
    unread = await asyncio.to_thread(store.list_unread_conversations)
    assert all(message is not None for message in messages)
    assert len(unread) == 1 and unread[0].unread_count == 2
    assert len(connections) == 3
    assert len({connection.owner for connection in connections if connection.immediate}) == 2
    assert all(connection.owner is connection.closed_on for connection in connections)
