"""账本的测试：只追加、按月统计、读写失败的边界（D39，06 号文档 12.2、12.4 节）。"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from aureus.common import ManualClock
from aureus.organs import CallAttempt
from aureus.safety import Ledger, month_bounds
from aureus.timeline import ActorType, TimelineDatabase

START = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def make_attempt(prompt: int = 100, completion: int = 50, **overrides: Any) -> CallAttempt:
    fields: dict[str, Any] = {
        "model": "model-a",
        "provider": "provider-a",
        "prompt_tokens": prompt,
        "completion_tokens": completion,
    }
    fields.update(overrides)
    return CallAttempt(**fields)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "aureus.db"


@pytest.fixture
def ledger(db_path: Path, clock: ManualClock) -> Iterator[Ledger]:
    opened = Ledger(db_path, clock)
    opened.open()
    yield opened
    opened.close()


def raw_execute(db_path: Path, sql: str) -> None:
    """绕过账本对象，直接对数据库文件执行 SQL，用来检验数据库自己的保护。"""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(sql)
        conn.commit()
    finally:
        conn.close()


# ---------- 记录和读取 ----------


def test_record_round_trips_every_field(ledger: Ledger) -> None:
    ledger.record(
        "planner",
        [
            make_attempt(
                120,
                30,
                cached_tokens=100,
                estimated=True,
                succeeded=False,
                failure_reason="超时",
                cost=0.25,
            )
        ],
    )
    (entry,) = ledger.entries()
    assert entry.timestamp == START
    assert entry.organ_id == "planner"
    assert (entry.model, entry.provider) == ("model-a", "provider-a")
    assert (entry.prompt_tokens, entry.completion_tokens, entry.cached_tokens) == (120, 30, 100)
    assert entry.estimated is True
    assert entry.succeeded is False
    assert entry.failure_reason == "超时"
    assert entry.cost == 0.25


def test_unknown_cost_stays_unknown_not_zero(ledger: Ledger) -> None:
    ledger.record("replyer", [make_attempt(cost=None)])
    assert ledger.entries()[0].cost is None  # D37：算不出来不是 0


def test_record_with_no_attempts_writes_nothing(ledger: Ledger) -> None:
    ledger.record("fake_replyer", [])
    assert ledger.entries() == []


def test_timestamp_comes_from_the_clock(ledger: Ledger, clock: ManualClock) -> None:
    clock.advance(timedelta(hours=5))
    ledger.record("replyer", [make_attempt()])
    assert ledger.entries()[0].timestamp == START + timedelta(hours=5)


def test_entries_support_paging(ledger: Ledger) -> None:
    for _ in range(5):
        ledger.record("replyer", [make_attempt()])
    first_page = ledger.entries(limit=2)
    assert [entry.id for entry in first_page] == [1, 2]
    assert [entry.id for entry in ledger.entries(since_id=2, limit=10)] == [3, 4, 5]


def test_records_survive_reopening(db_path: Path, clock: ManualClock) -> None:
    first = Ledger(db_path, clock)
    first.open()
    first.record("replyer", [make_attempt(10, 5)])
    first.close()
    second = Ledger(db_path, clock)
    second.open()
    try:
        assert second.tokens_used_in_month(START) == 15
    finally:
        second.close()


def test_ledger_and_timeline_can_share_one_database_file(db_path: Path, clock: ManualClock) -> None:
    ledger = Ledger(db_path, clock)
    timeline = TimelineDatabase(db_path, clock)
    ledger.open()
    timeline.open()
    try:
        ledger.record("replyer", [make_attempt(10, 5)])
        timeline.append("note", "同一个文件", ActorType.ME)
        assert ledger.tokens_used_in_month(START) == 15
        assert timeline.count_records() == 1
    finally:
        ledger.close()
        timeline.close()


# ---------- 只追加 ----------


def test_update_is_forbidden_by_the_database(ledger: Ledger, db_path: Path) -> None:
    ledger.record("replyer", [make_attempt()])
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        raw_execute(db_path, "UPDATE ledger SET prompt_tokens = 0")
    assert ledger.entries()[0].prompt_tokens == 100


def test_delete_is_forbidden_by_the_database(ledger: Ledger, db_path: Path) -> None:
    ledger.record("replyer", [make_attempt()])
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        raw_execute(db_path, "DELETE FROM ledger")
    assert len(ledger.entries()) == 1


def test_insert_or_replace_cannot_overwrite_a_record(ledger: Ledger, db_path: Path) -> None:
    # 审查 S3：REPLACE 先删后插，不触发 DELETE 触发器，要靠 BEFORE INSERT 触发器拦住。
    ledger.record("replyer", [make_attempt()])
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        raw_execute(
            db_path,
            "INSERT OR REPLACE INTO ledger "
            "(id, timestamp, organ_id, model, provider, prompt_tokens, completion_tokens,"
            " cached_tokens, estimated, succeeded) "
            "VALUES (1, '2026-11-16T12:00:00+00:00', 'x', 'x', 'x', 0, 0, 0, 0, 1)",
        )
    [entry] = ledger.entries()
    assert entry.prompt_tokens == 100


def test_appending_after_the_trigger_still_works(ledger: Ledger) -> None:
    ledger.record("replyer", [make_attempt()])
    ledger.record("replyer", [make_attempt()])
    assert len(ledger.entries()) == 2


@pytest.mark.parametrize("column", ["prompt_tokens", "completion_tokens", "cached_tokens"])
def test_database_rejects_negative_token_counts(ledger: Ledger, db_path: Path, column: str) -> None:
    values = {"prompt_tokens": 1, "completion_tokens": 1, "cached_tokens": 1, column: -1}
    sql = (
        "INSERT INTO ledger (timestamp, organ_id, model, provider, prompt_tokens, "
        "completion_tokens, cached_tokens, estimated, succeeded) "
        f"VALUES ('2026-11-16T00:00:00.000000+00:00', 'o', 'm', 'p', {values['prompt_tokens']}, "
        f"{values['completion_tokens']}, {values['cached_tokens']}, 0, 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        raw_execute(db_path, sql)


def test_record_is_all_or_nothing(ledger: Ledger) -> None:
    good = make_attempt(10, 5)
    # model 不能是空值：第二条写不进去时，第一条也不能留下。
    bad = make_attempt(model=cast(Any, None))
    with pytest.raises(sqlite3.IntegrityError):
        ledger.record("replyer", [good, bad])
    assert ledger.entries() == []


# ---------- 按月统计 ----------


def test_budget_tokens_are_prompt_plus_completion_without_double_counting_cache(
    ledger: Ledger,
) -> None:
    ledger.record("replyer", [make_attempt(1000, 200, cached_tokens=900)])
    assert ledger.tokens_used_in_month(START) == 1200


def test_failed_and_estimated_attempts_count_toward_the_month(ledger: Ledger) -> None:
    ledger.record(
        "replyer",
        [
            make_attempt(10, 0, succeeded=False, failure_reason="认证失败"),
            make_attempt(20, 8192, estimated=True, succeeded=False, failure_reason="超时"),
            make_attempt(30, 3),
        ],
    )
    assert ledger.tokens_used_in_month(START) == 10 + 20 + 8192 + 30 + 3


def test_empty_ledger_has_used_nothing(ledger: Ledger) -> None:
    assert ledger.tokens_used_in_month(START) == 0


def test_months_are_counted_separately_in_utc(ledger: Ledger, clock: ManualClock) -> None:
    clock.advance(datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC) - START)
    ledger.record("replyer", [make_attempt(100, 0)])
    clock.advance(timedelta(seconds=1))  # 进入 12 月
    ledger.record("replyer", [make_attempt(7, 0)])
    assert ledger.tokens_used_in_month(datetime(2026, 11, 15, tzinfo=UTC)) == 100
    assert ledger.tokens_used_in_month(datetime(2026, 12, 15, tzinfo=UTC)) == 7


def test_first_instant_of_the_month_belongs_to_the_new_month(
    ledger: Ledger, clock: ManualClock
) -> None:
    clock.advance(datetime(2026, 12, 1, tzinfo=UTC) - START)
    ledger.record("replyer", [make_attempt(50, 0)])
    assert ledger.tokens_used_in_month(datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC)) == 0
    assert ledger.tokens_used_in_month(datetime(2026, 12, 1, tzinfo=UTC)) == 50


def test_month_is_decided_in_utc_not_by_the_given_offset(ledger: Ledger) -> None:
    ledger.record("replyer", [make_attempt(10, 0)])  # UTC 11 月 16 日
    beijing = timezone(timedelta(hours=8))
    # 北京时间 12 月 1 日 7 点 = UTC 11 月 30 日 23 点，还是 11 月。
    assert ledger.tokens_used_in_month(datetime(2026, 12, 1, 7, tzinfo=beijing)) == 10


def test_naive_time_is_rejected(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="时区"):
        ledger.tokens_used_in_month(datetime(2026, 11, 16, 12, 0))


@pytest.mark.parametrize(
    ("moment", "start", "end"),
    [
        (
            datetime(2026, 11, 16, tzinfo=UTC),
            datetime(2026, 11, 1, tzinfo=UTC),
            datetime(2026, 12, 1, tzinfo=UTC),
        ),
        (
            datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
            datetime(2026, 12, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
        ),
        (
            datetime(2028, 2, 29, tzinfo=UTC),
            datetime(2028, 2, 1, tzinfo=UTC),
            datetime(2028, 3, 1, tzinfo=UTC),
        ),
    ],
)
def test_month_bounds(moment: datetime, start: datetime, end: datetime) -> None:
    assert month_bounds(moment) == (start, end)


# ---------- 没打开、被破坏 ----------


def test_using_a_closed_ledger_raises(db_path: Path, clock: ManualClock) -> None:
    closed = Ledger(db_path, clock)
    with pytest.raises(RuntimeError, match="没有打开"):
        closed.tokens_used_in_month(START)
    with pytest.raises(RuntimeError, match="没有打开"):
        closed.record("replyer", [make_attempt()])


def test_open_twice_is_harmless(ledger: Ledger) -> None:
    ledger.record("replyer", [make_attempt(1, 1)])
    ledger.open()
    assert len(ledger.entries()) == 1


def test_open_creates_missing_parent_directories(tmp_path: Path, clock: ManualClock) -> None:
    nested = Ledger(tmp_path / "a" / "b" / "aureus.db", clock)
    nested.open()
    nested.close()
    assert (tmp_path / "a" / "b" / "aureus.db").exists()
