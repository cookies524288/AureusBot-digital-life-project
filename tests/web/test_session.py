"""网页登录状态的测试（D63 第 7 条）。"""

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import ManualClock
from aureus.web import SessionStore

SESSION_DAYS = 7
START = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def store(tmp_path: Path, clock: ManualClock) -> Iterator[SessionStore]:
    opened = SessionStore(tmp_path / "aureus.db", clock, SESSION_DAYS)
    opened.open()
    yield opened
    opened.close()


@pytest.mark.asyncio
async def test_created_token_is_valid(store: SessionStore) -> None:
    token = store.create()
    assert await store.touch(token) is True


def test_tokens_are_unique_and_unguessable(store: SessionStore) -> None:
    tokens = {store.create() for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(token) >= 40 for token in tokens)


def test_database_never_holds_the_plain_token(tmp_path: Path, store: SessionStore) -> None:
    token = store.create()
    conn = sqlite3.connect(tmp_path / "aureus.db")
    try:
        rows = conn.execute("SELECT token_hash, created_at, last_active_at FROM web_sessions")
        dumped = " ".join(str(value) for row in rows for value in row)
    finally:
        conn.close()
    assert token not in dumped


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "nonsense", "x" * 1000])
async def test_unknown_empty_and_oversized_tokens_are_rejected(
    store: SessionStore, bad: str
) -> None:
    store.create()
    assert await store.touch(bad) is False


@pytest.mark.asyncio
async def test_each_use_renews_from_the_last_activity(
    store: SessionStore, clock: ManualClock
) -> None:
    token = store.create()
    for _ in range(3):  # 每 6 天用一次：总共 18 天，仍然有效
        clock.advance(timedelta(days=SESSION_DAYS - 1))
        assert await store.touch(token) is True


@pytest.mark.asyncio
async def test_token_expires_after_the_lifetime_without_activity(
    store: SessionStore, clock: ManualClock
) -> None:
    token = store.create()
    clock.advance(timedelta(days=SESSION_DAYS) - timedelta(seconds=1))
    assert await store.touch(token) is True  # 差一秒到期，还有效，并续期
    clock.advance(timedelta(days=SESSION_DAYS))
    assert await store.touch(token) is False
    assert await store.touch(token) is False  # 过期的已经删掉，再试仍然无效


@pytest.mark.asyncio
async def test_expiry_boundary_is_exact(store: SessionStore, clock: ManualClock) -> None:
    token = store.create()
    clock.advance(timedelta(days=SESSION_DAYS))
    assert await store.touch(token) is False  # 刚好满 7 天就算过期，不多给


@pytest.mark.asyncio
async def test_devices_are_timed_independently(store: SessionStore, clock: ManualClock) -> None:
    phone = store.create()
    laptop = store.create()
    clock.advance(timedelta(days=5))
    assert await store.touch(laptop) is True  # 只有电脑在用
    clock.advance(timedelta(days=3))  # 手机已经 8 天没动
    assert await store.touch(phone) is False
    assert await store.touch(laptop) is True


@pytest.mark.asyncio
async def test_revoke_ends_only_that_device(store: SessionStore) -> None:
    phone = store.create()
    laptop = store.create()
    store.revoke(phone)
    assert await store.touch(phone) is False
    assert await store.touch(laptop) is True


def test_revoke_of_unknown_token_is_harmless(store: SessionStore) -> None:
    store.revoke("never-issued")


@pytest.mark.asyncio
async def test_revoke_all_ends_every_device(store: SessionStore) -> None:
    tokens = [store.create() for _ in range(3)]
    assert store.revoke_all() == 3
    for token in tokens:
        assert await store.touch(token) is False
    assert store.revoke_all() == 0


@pytest.mark.asyncio
async def test_purge_removes_only_expired_sessions(store: SessionStore, clock: ManualClock) -> None:
    old = store.create()
    clock.advance(timedelta(days=SESSION_DAYS + 1))
    fresh = store.create()  # 新建时也会顺手清理
    assert store.purge_expired() == 0
    assert await store.touch(old) is False
    assert await store.touch(fresh) is True


@pytest.mark.asyncio
async def test_sessions_survive_a_restart(tmp_path: Path, clock: ManualClock) -> None:
    first = SessionStore(tmp_path / "aureus.db", clock, SESSION_DAYS)
    first.open()
    token = first.create()
    first.close()
    second = SessionStore(tmp_path / "aureus.db", clock, SESSION_DAYS)
    second.open()
    try:
        assert await second.touch(token) is True
    finally:
        second.close()


@pytest.mark.asyncio
async def test_open_twice_is_harmless(store: SessionStore) -> None:
    store.open()
    assert await store.touch(store.create()) is True


def test_using_a_store_that_is_not_open_raises(tmp_path: Path, clock: ManualClock) -> None:
    closed = SessionStore(tmp_path / "aureus.db", clock, SESSION_DAYS)
    with pytest.raises(RuntimeError, match="还没有打开"):
        closed.create()


@pytest.mark.asyncio
async def test_touch_does_not_block_event_loop(tmp_path: Path, clock: ManualClock) -> None:
    store = SessionStore(tmp_path / "aureus.db", clock, SESSION_DAYS)
    store.open()
    blocker = sqlite3.connect(tmp_path / "aureus.db", timeout=1)
    try:
        token = store.create()
        clock.advance(timedelta(seconds=31))  # 越过合并窗口，这次 touch 会写库
        blocker.execute("BEGIN IMMEDIATE")
        waiting = asyncio.create_task(store.touch(token))
        ticks = 0
        for _ in range(10):
            assert not waiting.done()  # 写锁还被占着，touch 在等
            ticks += 1
            await asyncio.sleep(0.01)
        blocker.rollback()
        assert await asyncio.wait_for(waiting, 2.0) is True
        assert ticks == 10
    finally:
        store.close()
        blocker.close()


@pytest.mark.asyncio
async def test_touch_writes_at_most_once_per_throttle_window(
    store: SessionStore,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = store.create()
    writes: list[datetime] = []
    original = store._write_last_active

    def counting(session_id: int, moment: datetime) -> None:
        writes.append(moment)
        original(session_id, moment)

    monkeypatch.setattr(store, "_write_last_active", counting)
    clock.advance(timedelta(seconds=31))
    for _ in range(5):
        assert await store.touch(token) is True
        clock.advance(timedelta(seconds=5))
    assert len(writes) == 1
    clock.advance(timedelta(seconds=10))  # 距上次写入正好 30 秒
    assert await store.touch(token) is True
    assert len(writes) == 2


@pytest.mark.asyncio
async def test_touch_expiry_uses_the_latest_activity_in_memory(
    store: SessionStore,
    clock: ManualClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = store.create()

    def no_write(_session_id: int, _moment: datetime) -> None:
        pass  # 让库里的时间停在创建时，只看内存判断

    monkeypatch.setattr(store, "_write_last_active", no_write)
    clock.advance(timedelta(days=SESSION_DAYS - 1))
    assert await store.touch(token) is True
    clock.advance(timedelta(days=SESSION_DAYS - 1))
    assert await store.touch(token) is True  # 库里已 12 天没动，内存只过了 6 天
    clock.advance(timedelta(days=SESSION_DAYS, seconds=1))
    assert await store.touch(token) is False  # 从内存里的最近活动算满 7 天
