"""时间线子系统的测试。"""

import json
import tempfile
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from aureus.common import AcceleratedClock, SystemClock
from aureus.timeline import (
    ActorType,
    TimelineDatabase,
    TimelineQuery,
    TimelineRecord,
    VisibilityScope,
)
from aureus.timeline.database import UNREADABLE_METADATA_KEY


@pytest.fixture
def temp_db_path() -> Generator[Path, None, None]:
    """创建临时数据库文件路径。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "timeline.db"


@pytest.fixture
def database(temp_db_path: Path) -> Generator[TimelineDatabase, None, None]:
    """创建并打开时间线数据库。"""
    db = TimelineDatabase(temp_db_path)
    db.open()
    yield db
    db.close()


def test_database_create_and_open(temp_db_path: Path):
    """测试数据库创建和打开。"""
    db = TimelineDatabase(temp_db_path)
    db.open()
    assert temp_db_path.exists()
    db.close()


def test_append_single_record(database: TimelineDatabase):
    """测试追加单条记录。"""
    record = database.append(
        event_type="message",
        content="Hello world",
        actor_type=ActorType.ME,
        visibility=VisibilityScope.PRIVATE,
    )

    assert record.id == 1
    assert record.event_type == "message"
    assert record.content == "Hello world"
    assert record.actor_type == ActorType.ME
    assert record.visibility == VisibilityScope.PRIVATE


def test_append_multiple_records(database: TimelineDatabase):
    """测试追加多条记录，验证 ID 自增。"""
    record1 = database.append(
        event_type="message",
        content="First",
        actor_type=ActorType.ME,
    )
    record2 = database.append(
        event_type="message",
        content="Second",
        actor_type=ActorType.USER,
        actor_id="user123",
    )

    assert record1.id == 1
    assert record2.id == 2
    assert database.count_records() == 2


def test_append_with_actor_id(database: TimelineDatabase):
    """测试追加带 actor_id 的记录。"""
    record = database.append(
        event_type="message",
        content="From person",
        actor_type=ActorType.PERSON,
        actor_id="person_alice",
    )

    assert record.actor_type == ActorType.PERSON
    assert record.actor_id == "person_alice"


def test_append_with_metadata(database: TimelineDatabase):
    """测试追加带元数据的记录。"""
    metadata = {"channel": "qq", "group_id": "123456"}
    record = database.append(
        event_type="message",
        content="From QQ",
        actor_type=ActorType.USER,
        visibility=VisibilityScope.GROUP,
        metadata=metadata,
    )

    assert record.metadata == metadata


def test_query_all_records(database: TimelineDatabase):
    """测试查询所有记录。"""
    database.append("message", "First", ActorType.ME)
    database.append("message", "Second", ActorType.USER)
    database.append("action", "Action", ActorType.ME)

    query = TimelineQuery(limit=100)
    results = database.query(query)

    assert len(results) == 3
    assert results[0].content == "First"
    assert results[2].event_type == "action"


def test_query_by_event_type(database: TimelineDatabase):
    """测试按事件类型过滤。"""
    database.append("message", "Msg1", ActorType.ME)
    database.append("action", "Act1", ActorType.ME)
    database.append("message", "Msg2", ActorType.USER)

    query = TimelineQuery(event_type="message", limit=100)
    results = database.query(query)

    assert len(results) == 2
    assert all(r.event_type == "message" for r in results)


def test_query_by_actor_type(database: TimelineDatabase):
    """测试按执行者类型过滤。"""
    database.append("message", "From me", ActorType.ME)
    database.append("message", "From user", ActorType.USER)
    database.append("message", "From system", ActorType.SYSTEM)

    query = TimelineQuery(actor_type=ActorType.USER, limit=100)
    results = database.query(query)

    assert len(results) == 1
    assert results[0].actor_type == ActorType.USER


@pytest.mark.parametrize("value", [True, False, 1, 1.25, "true", None])
def test_query_metadata_matches_scalar_type_and_value(
    database: TimelineDatabase, value: str | int | float | bool | None
) -> None:
    candidates = [True, False, 1, 1.25, "true", None]
    expected: list[int] = []
    for candidate in candidates:
        row = database.append("message", "记录", ActorType.ME, metadata={"flag": candidate})
        if type(candidate) is type(value) and candidate == value:
            expected.append(row.id)
    database.append("message", "缺少这个字段", ActorType.ME)
    rows = database.query(TimelineQuery(metadata={"flag": value}))
    assert [row.id for row in rows] == expected


def test_metadata_filter_precedes_latest_limit_and_combines_conditions(
    database: TimelineDatabase,
) -> None:
    first = database.append(
        "message", "主动", ActorType.ME, metadata={"initiative": True, "channel": "terminal"}
    )
    for _ in range(60):
        database.append("message", "普通回复", ActorType.ME)
    database.append("message", "用户", ActorType.USER, metadata={"initiative": True})
    database.append("action", "其他事件", ActorType.ME, metadata={"initiative": True})
    database.append(
        "message", "其他通道", ActorType.ME, metadata={"initiative": True, "channel": "web"}
    )
    rows = database.query(
        TimelineQuery(
            actor_type=ActorType.ME,
            event_type="message",
            latest=True,
            limit=1,
            metadata={"initiative": True, "channel": "terminal"},
        )
    )
    assert [row.id for row in rows] == [first.id]


def test_query_since_id(database: TimelineDatabase):
    """测试从指定 ID 之后查询。"""
    for i in range(1, 6):
        database.append("message", f"Message {i}", ActorType.ME)

    query = TimelineQuery(since_id=2, limit=100)
    results = database.query(query)

    assert len(results) == 3
    assert results[0].id == 3
    assert results[-1].id == 5


def test_query_limit(database: TimelineDatabase):
    """测试查询结果数量限制。"""
    for i in range(1, 11):
        database.append("message", f"Message {i}", ActorType.ME)

    query = TimelineQuery(limit=5)
    results = database.query(query)

    assert len(results) == 5


def test_query_visible_to_user(database: TimelineDatabase):
    """测试用户可见性过滤。"""
    database.append("message", "Private", ActorType.ME, visibility=VisibilityScope.PRIVATE)
    database.append("message", "User only", ActorType.ME, visibility=VisibilityScope.USER_ONLY)
    database.append("message", "Group", ActorType.ME, visibility=VisibilityScope.GROUP)
    database.append("message", "Public", ActorType.ME, visibility=VisibilityScope.PUBLIC)

    query = TimelineQuery(visible_to_current_user=True, limit=100)
    results = database.query(query)

    assert len(results) == 3  # user_only, group, public
    visibilities = {r.visibility for r in results}
    assert VisibilityScope.PRIVATE not in visibilities


def test_get_latest_id(database: TimelineDatabase):
    """测试获取最大 ID。"""
    assert database.get_latest_id() == 0

    database.append("message", "First", ActorType.ME)
    assert database.get_latest_id() == 1

    database.append("message", "Second", ActorType.ME)
    assert database.get_latest_id() == 2


def test_immutability_update_blocked(database: TimelineDatabase):
    """测试 UPDATE 被数据库层面禁止。"""
    database.append("message", "Original", ActorType.ME)

    # 尝试直接 UPDATE 应该被触发器阻止
    assert database._conn is not None
    with pytest.raises(Exception) as exc_info:
        database._conn.execute("UPDATE timeline SET content = 'Modified' WHERE id = 1")
        database._conn.commit()

    assert "immutable" in str(exc_info.value).lower()


def test_immutability_insert_or_replace_blocked(database: TimelineDatabase):
    """测试 INSERT OR REPLACE 不能覆盖已有的行（审查 S3：REPLACE 不触发 DELETE 触发器）。"""
    database.append("message", "Original", ActorType.ME)

    assert database._conn is not None
    with pytest.raises(Exception) as exc_info:
        database._conn.execute(
            "INSERT OR REPLACE INTO timeline "
            "(id, timestamp, actor_type, event_type, visibility) "
            "VALUES (1, '2026-11-16T12:00:00+00:00', 'me', 'message', 'private')"
        )
        database._conn.commit()

    assert "immutable" in str(exc_info.value).lower()
    assert database.count_records() == 1
    assert database.query(TimelineQuery())[0].content == "Original"


def test_immutability_delete_blocked(database: TimelineDatabase):
    """测试 DELETE 被数据库层面禁止。"""
    database.append("message", "Original", ActorType.ME)

    # 尝试直接 DELETE 应该被触发器阻止
    assert database._conn is not None
    with pytest.raises(Exception) as exc_info:
        database._conn.execute("DELETE FROM timeline WHERE id = 1")
        database._conn.commit()

    assert "immutable" in str(exc_info.value).lower()


def test_with_accelerated_clock(temp_db_path: Path):
    """测试使用加速时钟。"""
    start_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    real_clock = SystemClock()
    clock = AcceleratedClock(start=start_time, speed=3600.0, real_clock=real_clock)
    db = TimelineDatabase(temp_db_path, clock=clock)
    db.open()

    record1 = db.append("message", "First", ActorType.ME)

    time.sleep(0.01)  # 真实过 0.01 秒 = 加速后过 36 秒
    record2 = db.append("message", "Second", ActorType.ME)

    # 两条记录的时间差应该大于 30 秒（真实只过了 0.01 秒）
    diff = (record2.timestamp - record1.timestamp).total_seconds()
    assert diff > 30, f"Expected accelerated time difference > 30s, got {diff}s"

    db.close()

    db.close()


def test_timeline_record_summary():
    """测试记录摘要生成。"""
    record = TimelineRecord(
        id=42,
        timestamp=datetime(2026, 11, 16, 10, 30, 0, tzinfo=UTC),
        actor_type=ActorType.USER,
        actor_id="user123",
        event_type="message",
        content="This is a very long message that should be truncated in the summary",
        visibility=VisibilityScope.USER_ONLY,
    )

    summary = record.summary()
    assert "[42]" in summary
    assert "2026-11-16T10:30:00" in summary
    assert "message" in summary
    assert "用户" in summary


# ---------- metadata 改成 JSON 存取（D41 第 9 条） ----------


def _insert_raw_metadata(database: TimelineDatabase, raw: str) -> None:
    """绕过 append，直接写一条带指定 metadata 原文的记录，模拟改成 JSON 之前的旧数据。"""
    assert database._conn is not None
    database._conn.execute(
        "INSERT INTO timeline (timestamp, actor_type, event_type, visibility, metadata) "
        "VALUES (?, ?, ?, ?, ?)",
        ("2026-11-16T00:00:00+00:00", "me", "legacy", "private", raw),
    )
    database._conn.commit()


def test_metadata_is_stored_as_json(database: TimelineDatabase):
    metadata = {"层次": {"能量": 0.5, "障碍": ["能量不足"]}, "空": None, "开关": True}
    database.append("cognitive_state", "x", ActorType.ME, metadata=metadata)

    assert database._conn is not None
    raw = database._conn.execute("SELECT metadata FROM timeline").fetchone()[0]
    assert json.loads(raw) == metadata
    assert "能量" in raw  # 中文按原样存，不转成 \u 转义
    assert database.query(TimelineQuery())[0].metadata == metadata


def test_metadata_with_non_json_value_is_rejected_before_writing(database: TimelineDatabase):
    with pytest.raises(ValueError, match="JSON"):
        database.append(
            "message", "x", ActorType.ME, metadata={"when": datetime(2026, 11, 17, tzinfo=UTC)}
        )
    assert database.count_records() == 0


def test_legacy_python_repr_metadata_is_still_readable(database: TimelineDatabase):
    _insert_raw_metadata(database, str({"organ_id": "replyer", "count": 3, "ok": True}))

    record = database.query(TimelineQuery())[0]

    assert record.metadata == {"organ_id": "replyer", "count": 3, "ok": True}


@pytest.mark.parametrize(
    "raw",
    [
        "__import__('os').getcwd()",  # 原来的 eval() 会执行它
        "{not valid",
        "[1, 2, 3]",  # 合法，但不是字典
    ],
)
def test_unreadable_metadata_is_kept_verbatim_without_executing(
    database: TimelineDatabase, raw: str
):
    _insert_raw_metadata(database, raw)

    record = database.query(TimelineQuery())[0]

    assert record.metadata == {UNREADABLE_METADATA_KEY: raw}


# ---------- 只取最新几条 ----------


def test_query_latest_returns_newest_records_in_ascending_order(database: TimelineDatabase):
    for i in range(1, 6):
        database.append("message", f"Message {i}", ActorType.ME)

    results = database.query(TimelineQuery(limit=2, latest=True))

    assert [r.content for r in results] == ["Message 4", "Message 5"]


def test_query_latest_respects_filters(database: TimelineDatabase):
    database.append("message", "old", ActorType.USER)
    database.append("message", "new", ActorType.USER)
    database.append("cognitive_state", "thinking", ActorType.ME)

    results = database.query(TimelineQuery(limit=1, latest=True, event_type="message"))

    assert [r.content for r in results] == ["new"]


def test_query_latest_on_empty_timeline(database: TimelineDatabase):
    assert database.query(TimelineQuery(limit=1, latest=True)) == []


def test_query_by_exact_visibility(database: TimelineDatabase):
    database.append("message", "private chat", ActorType.USER, visibility=VisibilityScope.USER_ONLY)
    database.append("message", "group chat", ActorType.PERSON, visibility=VisibilityScope.GROUP)
    database.append("message", "inner", ActorType.ME, visibility=VisibilityScope.PRIVATE)

    results = database.query(TimelineQuery(visibility=VisibilityScope.USER_ONLY))

    assert [r.content for r in results] == ["private chat"]


class LocalTimeClock:
    """返回非 UTC 时区的时钟：模拟将来有人传进本地时间。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


def test_timestamps_are_stored_in_utc_even_if_the_clock_is_not(temp_db_path: Path):
    """审查 M4：时间线按字符串比较时间，存进去的必须统一是 UTC。"""
    china = timezone(timedelta(hours=8))
    clock = LocalTimeClock(datetime(2026, 11, 18, 8, 0, tzinfo=china))
    db = TimelineDatabase(temp_db_path, clock=clock)
    db.open()
    try:
        record = db.append(event_type="message", content="早", actor_type=ActorType.ME)
        [stored] = db.query(TimelineQuery(limit=10))
    finally:
        db.close()

    assert record.timestamp == datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
    assert stored.timestamp.utcoffset() == timedelta(0)
    assert stored.timestamp == record.timestamp
