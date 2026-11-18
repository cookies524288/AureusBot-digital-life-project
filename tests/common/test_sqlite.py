"""SQLite 连接的测试：被别的进程占着写锁时，等锁有上限（审查 M2）。"""

import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aureus.common import BudgetCalendar, ManualClock
from aureus.common import sqlite as sqlite_support
from aureus.organs import CallAttempt
from aureus.safety import Ledger
from aureus.timeline import ActorType, TimelineDatabase

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
