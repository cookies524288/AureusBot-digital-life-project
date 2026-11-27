"""观察台只读数据层的测试：读得到、缺表不报错、任何写入都失败（D64）。"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.body import BodySample, BodySampleStore
from aureus.common import BudgetCalendar, ManualClock
from aureus.identity import Identity, IdentityHistory, IdentityLayer
from aureus.monitor import MonitorError, MonitorReader
from aureus.organs import CallAttempt
from aureus.safety import Ledger
from aureus.timeline import ActorType, TimelineDatabase, VisibilityScope

START = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
CALENDAR = BudgetCalendar(UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "aureus.db"


@pytest.fixture
def reader(db_path: Path) -> Iterator[MonitorReader]:
    # 先让一个空库存在：观察台只读，自己不会建库。
    sqlite3.connect(db_path).close()
    opened = MonitorReader(db_path)
    opened.open()
    yield opened
    opened.close()


def make_sample(moment: datetime, inbox_count: int = 0) -> BodySample:
    return BodySample(
        timestamp=moment,
        energy_tier="正常",
        energy_fraction=0.8,
        used_today=100,
        daily_quota=500,
        fatigue_level=None,
        stress_level=0.1,
        night=False,
        focus="用户",
        inbox_count=inbox_count,
    )


def test_missing_database_is_reported_in_chinese(tmp_path: Path) -> None:
    with pytest.raises(MonitorError, match="找不到数据库"):
        MonitorReader(tmp_path / "nope.db").open()


def test_reader_does_not_create_the_database(tmp_path: Path) -> None:
    path = tmp_path / "nope.db"
    with pytest.raises(MonitorError):
        MonitorReader(path).open()
    assert not path.exists()


def test_use_before_open_raises(db_path: Path) -> None:
    with pytest.raises(RuntimeError):
        MonitorReader(db_path).latest_sample()


def test_empty_database_means_no_data_not_an_error(reader: MonitorReader) -> None:
    assert reader.latest_sample() is None
    assert reader.samples_since(START - timedelta(days=1)) == []
    assert reader.recent_timeline() == []
    assert reader.tokens_used(START - timedelta(days=1), START) == 0
    assert reader.identity_versions() == []


def test_open_twice_is_harmless(reader: MonitorReader) -> None:
    reader.open()
    assert reader.latest_sample() is None


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE intruder (x INTEGER)",
        "INSERT INTO body_samples (id) VALUES (1)",
        "DELETE FROM timeline",
        "UPDATE ledger SET cost = 0",
    ],
)
def test_every_write_is_refused(db_path: Path, sql: str) -> None:
    clock = ManualClock(START)
    store = BodySampleStore(db_path, clock)
    store.open()
    store.append(make_sample(START))
    store.close()
    reader = MonitorReader(db_path)
    reader.open()
    try:
        # 直接拿底层连接，验证连接本身是只读的。
        conn = reader._conn
        assert conn is not None
        with pytest.raises(sqlite3.Error):
            conn.execute(sql)
    finally:
        reader.close()
    # 被拒绝的写入没有留下任何东西。
    check = sqlite3.connect(db_path)
    try:
        tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master")}
        assert "intruder" not in tables
        assert check.execute("SELECT COUNT(*) FROM body_samples").fetchone()[0] == 1
    finally:
        check.close()


def test_samples_are_read_back_old_to_new(db_path: Path) -> None:
    clock = ManualClock(START)
    store = BodySampleStore(db_path, clock)
    store.open()
    for minute in range(4):
        store.append(make_sample(START + timedelta(minutes=minute), inbox_count=minute))
    store.close()
    reader = MonitorReader(db_path)
    reader.open()
    try:
        latest = reader.latest_sample()
        assert latest is not None and latest.inbox_count == 3
        # 关掉的信号读回来仍是 None，不是 0。
        assert latest.fatigue_level is None
        rows = reader.samples_since(START + timedelta(minutes=1))
        assert [row.inbox_count for row in rows] == [1, 2, 3]
        assert [row.inbox_count for row in reader.samples_since(START, limit=2)] == [0, 1]
    finally:
        reader.close()


def test_recent_timeline_is_old_to_new_and_limited(db_path: Path) -> None:
    timeline = TimelineDatabase(db_path, ManualClock(START))
    timeline.open()
    for number in range(5):
        timeline.append(
            event_type="message",
            content=f"第{number}条",
            actor_type=ActorType.USER,
            visibility=VisibilityScope.USER_ONLY,
        )
    timeline.close()
    reader = MonitorReader(db_path)
    reader.open()
    try:
        entries = reader.recent_timeline(limit=3)
        assert [entry.content for entry in entries] == ["第2条", "第3条", "第4条"]
        assert entries[0].actor_type == "user" and entries[0].event_type == "message"
        assert entries[0].visibility == "user_only"
    finally:
        reader.close()


def test_tokens_used_sums_input_and_output_inside_the_range(db_path: Path) -> None:
    clock = ManualClock(START)
    ledger = Ledger(db_path, clock, calendar=CALENDAR)
    ledger.open()
    attempt = CallAttempt(model="m", provider="p", prompt_tokens=100, completion_tokens=50)
    ledger.record("replyer", [attempt])
    clock.advance(timedelta(days=1))
    ledger.record("replyer", [attempt])
    ledger.close()
    reader = MonitorReader(db_path)
    reader.open()
    try:
        day = timedelta(days=1)
        assert reader.tokens_used(START - day, START + day / 2) == 150
        assert reader.tokens_used(START - day, START + day * 2) == 300
        assert reader.tokens_used(START + day * 3, START + day * 4) == 0
    finally:
        reader.close()


def test_identity_versions_are_read_back(db_path: Path) -> None:
    history = IdentityHistory(db_path, ManualClock(START))
    history.open()
    history.sync_from_file(
        Identity.model_validate(
            {
                "facts": {"name": "小绿", "birthday": "2008-01-01"},
                "user": {"name": "阿明"},
                "persona": {"immutable": "温和。", "expression_style": "口语化。"},
            }
        )
    )
    history.close()
    reader = MonitorReader(db_path)
    reader.open()
    try:
        versions = reader.identity_versions()
        assert versions
        styles = [v for v in versions if v.layer is IdentityLayer.EXPRESSION_STYLE]
        assert styles and styles[-1].content == "口语化。"
        assert [v.id for v in versions] == sorted(v.id for v in versions)
        assert len(reader.identity_versions(limit=1)) == 1
    finally:
        reader.close()
