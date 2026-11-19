"""网页登录状态的测试（D63 第 7 条）。"""

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


def test_created_token_is_valid(store: SessionStore) -> None:
    token = store.create()
    assert store.touch(token) is True


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


@pytest.mark.parametrize("bad", ["", "nonsense", "x" * 1000])
def test_unknown_empty_and_oversized_tokens_are_rejected(store: SessionStore, bad: str) -> None:
    store.create()
    assert store.touch(bad) is False


def test_each_use_renews_from_the_last_activity(store: SessionStore, clock: ManualClock) -> None:
    token = store.create()
    for _ in range(3):  # 每 6 天用一次：总共 18 天，仍然有效
        clock.advance(timedelta(days=SESSION_DAYS - 1))
        assert store.touch(token) is True


def test_token_expires_after_the_lifetime_without_activity(
    store: SessionStore, clock: ManualClock
) -> None:
    token = store.create()
    clock.advance(timedelta(days=SESSION_DAYS) - timedelta(seconds=1))
    assert store.touch(token) is True  # 差一秒到期，还有效，并续期
    clock.advance(timedelta(days=SESSION_DAYS))
    assert store.touch(token) is False
    assert store.touch(token) is False  # 过期的已经删掉，再试仍然无效


def test_expiry_boundary_is_exact(store: SessionStore, clock: ManualClock) -> None:
    token = store.create()
    clock.advance(timedelta(days=SESSION_DAYS))
    assert store.touch(token) is False  # 刚好满 7 天就算过期，不多给


def test_devices_are_timed_independently(store: SessionStore, clock: ManualClock) -> None:
    phone = store.create()
    laptop = store.create()
    clock.advance(timedelta(days=5))
    assert store.touch(laptop) is True  # 只有电脑在用
    clock.advance(timedelta(days=3))  # 手机已经 8 天没动
    assert store.touch(phone) is False
    assert store.touch(laptop) is True


def test_revoke_ends_only_that_device(store: SessionStore) -> None:
    phone = store.create()
    laptop = store.create()
    store.revoke(phone)
    assert store.touch(phone) is False
    assert store.touch(laptop) is True


def test_revoke_of_unknown_token_is_harmless(store: SessionStore) -> None:
    store.revoke("never-issued")


def test_revoke_all_ends_every_device(store: SessionStore) -> None:
    tokens = [store.create() for _ in range(3)]
    assert store.revoke_all() == 3
    assert not any(store.touch(token) for token in tokens)
    assert store.revoke_all() == 0


def test_purge_removes_only_expired_sessions(store: SessionStore, clock: ManualClock) -> None:
    old = store.create()
    clock.advance(timedelta(days=SESSION_DAYS + 1))
    fresh = store.create()  # 新建时也会顺手清理
    assert store.purge_expired() == 0
    assert store.touch(old) is False
    assert store.touch(fresh) is True


def test_sessions_survive_a_restart(tmp_path: Path, clock: ManualClock) -> None:
    first = SessionStore(tmp_path / "aureus.db", clock, SESSION_DAYS)
    first.open()
    token = first.create()
    first.close()
    second = SessionStore(tmp_path / "aureus.db", clock, SESSION_DAYS)
    second.open()
    try:
        assert second.touch(token) is True
    finally:
        second.close()


def test_open_twice_is_harmless(store: SessionStore) -> None:
    store.open()
    assert store.touch(store.create()) is True


def test_using_a_store_that_is_not_open_raises(tmp_path: Path, clock: ManualClock) -> None:
    closed = SessionStore(tmp_path / "aureus.db", clock, SESSION_DAYS)
    with pytest.raises(RuntimeError, match="还没有打开"):
        closed.create()
