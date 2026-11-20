"""兴趣去重、容量、事务和起始同步（D79）。"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from aureus.agenda.interests import InterestStore
from aureus.common import InterestsConfig, ManualClock
from tests.agenda.test_agenda_store import NOW


@pytest.fixture
def interests(tmp_path: Path) -> Iterator[InterestStore]:
    store = InterestStore(tmp_path / "aureus.db", ManualClock(NOW), InterestsConfig(max_active=2))
    store.open()
    yield store
    store.close()


def test_interest_add_trim_duplicate_and_capacity(interests: InterestStore) -> None:
    assert interests.add(" 天文 ", "seed") is not None
    assert interests.add("天文", "self", 4) is None
    assert interests.add("植物", "self", 4) is not None
    assert interests.add("音乐", "seed") is None
    assert interests.add("字" * 31, "self", 4) is None
    assert [item.text for item in interests.active()] == ["天文", "植物"]


def test_interest_replace_is_atomic_and_remove_only_once(interests: InterestStore) -> None:
    first = interests.add("天文", "seed")
    assert first is not None
    interests.add("植物", "seed")
    assert interests.replace(first.id, "植物", 7) is None
    assert interests.get(first.id) == first
    new = interests.replace(first.id, "音乐", 7)
    assert new is not None and new.source_record_id == 7 and new.origin == "self"
    old = interests.get(first.id)
    assert old is not None and old.replaced_by == new.id and old.end_reason == "replaced"
    assert len(interests.active()) == 2
    assert interests.remove(first.id, "user_removed") is None
    assert interests.remove(new.id, "user_removed") is not None
    assert interests.remove(new.id, "user_removed") is None


def test_interest_replace_rolls_back_when_ending_old_row_fails(interests: InterestStore) -> None:
    first = interests.add("天文", "seed")
    assert first is not None
    conn = interests._require_open()
    conn.execute(
        "CREATE TRIGGER fail_end BEFORE UPDATE ON interests BEGIN SELECT RAISE(ABORT, 'fail'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        interests.replace(first.id, "音乐", 9)
    assert interests.active() == [first]
    assert conn.execute("SELECT COUNT(*) FROM interests").fetchone()[0] == 1


def test_seed_sync_never_resurrects_ended_or_self_origin(interests: InterestStore, caplog) -> None:
    assert interests.sync_seeds(["天文", "植物", "音乐"]) == (2, 0)
    assert "跳过" in caplog.text
    first, second = interests.active()
    interests.remove(first.id, "self_removed")
    assert interests.sync_seeds(["天文"]) == (0, 1)
    removed = interests.get(second.id)
    assert removed is not None and removed.end_reason == "user_removed"
    item = interests.add("电影", "self", 9)
    assert item is not None
    interests.remove(item.id, "user_removed")
    assert interests.sync_seeds(["电影", "新兴趣"]) == (1, 0)
    assert [item.text for item in interests.active()] == ["新兴趣"]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM interests",
        "UPDATE interests SET text = '改字'",
        "UPDATE interests SET origin = 'self'",
        "UPDATE interests SET created_at = 'x'",
        "UPDATE interests SET source_record_id = 8",
        "UPDATE interests SET id = 8",
        "INSERT OR REPLACE INTO interests(id,text,origin,created_at) VALUES(1,'改字','self','x')",
    ],
)
def test_interest_triggers_reject_rewrite(interests: InterestStore, sql: str) -> None:
    interests.add("天文", "seed")
    with pytest.raises(sqlite3.IntegrityError), interests._require_open() as conn:
        conn.execute(sql)


def test_removed_interest_is_fully_immutable(interests: InterestStore) -> None:
    item = interests.add("天文", "seed")
    assert item is not None
    interests.remove(item.id, "user_removed")
    for sql in [
        "UPDATE interests SET status = 'active'",
        "UPDATE interests SET end_reason = 'self_removed'",
    ]:
        with pytest.raises(sqlite3.IntegrityError), interests._require_open() as conn:
            conn.execute(sql)
