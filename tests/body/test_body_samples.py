"""身体信号样本存储的测试：存取、关掉的信号存 NULL、清理过期（D64）。"""

import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from aureus.body import (
    UNLIMITED_READING,
    BodyReading,
    BodySample,
    BodySampleStore,
    FatigueReading,
    SleepReading,
    StressReading,
    sample_from_reading,
)
from aureus.common import ManualClock

START = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def make_sample(moment: datetime = START, **overrides: Any) -> BodySample:
    sample = BodySample(
        timestamp=moment,
        energy_tier="正常",
        energy_fraction=0.8,
        used_today=100,
        daily_quota=500,
        fatigue_level=0.2,
        stress_level=0.1,
        night=False,
        focus="用户",
        inbox_count=2,
    )
    return replace(sample, **overrides)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(START)


@pytest.fixture
def store(tmp_path: Path, clock: ManualClock) -> Iterator[BodySampleStore]:
    opened = BodySampleStore(tmp_path / "aureus.db", clock)
    opened.open()
    yield opened
    opened.close()


def test_empty_store_has_no_latest(store: BodySampleStore) -> None:
    assert store.latest() is None
    assert store.since(START - timedelta(days=1)) == []


def test_append_and_latest_round_trip(store: BodySampleStore) -> None:
    sample = make_sample()
    store.append(sample)
    assert store.latest() == sample


def test_none_values_stay_none_not_zero(store: BodySampleStore) -> None:
    sample = make_sample(
        energy_tier="不限",
        energy_fraction=None,
        used_today=None,
        daily_quota=None,
        fatigue_level=None,
        stress_level=None,
        night=None,
    )
    store.append(sample)
    saved = store.latest()
    assert saved == sample
    assert saved is not None and saved.night is None and saved.fatigue_level is None


def test_night_true_round_trips(store: BodySampleStore) -> None:
    store.append(make_sample(night=True))
    saved = store.latest()
    assert saved is not None and saved.night is True


def test_since_returns_old_to_new_and_respects_limit(store: BodySampleStore) -> None:
    for minute in range(5):
        store.append(make_sample(START + timedelta(minutes=minute), inbox_count=minute))
    rows = store.since(START + timedelta(minutes=1))
    assert [row.inbox_count for row in rows] == [1, 2, 3, 4]
    assert [row.inbox_count for row in store.since(START, limit=2)] == [0, 1]


def test_prune_removes_only_expired_rows(store: BodySampleStore) -> None:
    store.append(make_sample(START - timedelta(days=31), inbox_count=1))
    store.append(make_sample(START - timedelta(days=29), inbox_count=2))
    store.append(make_sample(START, inbox_count=3))
    assert store.prune(30) == 1
    assert [row.inbox_count for row in store.since(START - timedelta(days=60))] == [2, 3]


def test_prune_with_nothing_expired_deletes_nothing(store: BodySampleStore) -> None:
    store.append(make_sample())
    assert store.prune(30) == 0


def test_prune_follows_the_clock(store: BodySampleStore, clock: ManualClock) -> None:
    store.append(make_sample())
    clock.advance(timedelta(days=31))
    assert store.prune(30) == 1
    assert store.latest() is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"energy_fraction": 1.5},
        {"fatigue_level": -0.1},
        {"stress_level": 2.0},
        {"used_today": -1},
        {"inbox_count": -1},
    ],
)
def test_database_rejects_out_of_range_values(
    store: BodySampleStore, overrides: dict[str, Any]
) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.append(make_sample(**overrides))
    assert store.latest() is None


def test_open_twice_keeps_data(store: BodySampleStore) -> None:
    store.append(make_sample())
    store.open()
    assert store.latest() is not None


def test_reopen_recreates_nothing_lost(tmp_path: Path, clock: ManualClock) -> None:
    path = tmp_path / "aureus.db"
    first = BodySampleStore(path, clock)
    first.open()
    first.append(make_sample())
    first.close()
    second = BodySampleStore(path, clock)
    second.open()
    try:
        assert second.latest() == make_sample()
    finally:
        second.close()


def test_use_before_open_raises(tmp_path: Path, clock: ManualClock) -> None:
    closed = BodySampleStore(tmp_path / "aureus.db", clock)
    with pytest.raises(RuntimeError):
        closed.latest()
    with pytest.raises(RuntimeError):
        closed.append(make_sample())


def test_sample_from_reading_copies_enabled_signals() -> None:
    reading = BodyReading(
        energy=UNLIMITED_READING,
        fatigue=FatigueReading(enabled=True, level=0.4, calls_in_window=24),
        stress=StressReading(enabled=True, level=0.3),
        sleep=SleepReading(enabled=True, night=True),
    )
    sample = sample_from_reading(START, reading, focus="用户", inbox_count=3)
    assert sample.energy_tier == "不限"
    assert sample.energy_fraction is None and sample.daily_quota is None
    assert (sample.fatigue_level, sample.stress_level) == (0.4, 0.3)
    assert sample.night is True
    assert (sample.focus, sample.inbox_count) == ("用户", 3)


def test_sample_from_reading_stores_none_for_disabled_signals() -> None:
    reading = BodyReading(
        energy=UNLIMITED_READING,
        fatigue=FatigueReading(enabled=False),
        stress=StressReading(enabled=False),
        sleep=SleepReading(enabled=False),
    )
    sample = sample_from_reading(START, reading, focus="", inbox_count=0)
    assert sample.fatigue_level is None
    assert sample.stress_level is None
    assert sample.night is None


def test_legacy_backlog_column_is_kept_but_not_read(store: BodySampleStore, tmp_path: Path) -> None:
    # D153：旧行里的 backlog 原样保留，新行写 NULL；BodySample 不再读它。
    conn = sqlite3.connect(tmp_path / "aureus.db")
    try:
        conn.execute(
            "INSERT INTO body_samples (timestamp, energy_tier, backlog, focus, inbox_count)"
            " VALUES (?, '正常', 7, '', 5)",
            (START.isoformat(timespec="microseconds"),),
        )
        conn.commit()
    finally:
        conn.close()
    store.append(make_sample(START + timedelta(minutes=1), inbox_count=3))
    conn = sqlite3.connect(tmp_path / "aureus.db")
    try:
        rows = conn.execute("SELECT backlog, inbox_count FROM body_samples ORDER BY id").fetchall()
    finally:
        conn.close()
    assert rows == [(7, 5), (None, 3)]
    assert [sample.inbox_count for sample in store.since(START)] == [5, 3]


def test_sample_from_reading_clamps_negative_inbox_count() -> None:
    reading = BodyReading(
        energy=UNLIMITED_READING,
        fatigue=FatigueReading(enabled=False),
        stress=StressReading(enabled=False),
        sleep=SleepReading(enabled=False),
    )
    assert sample_from_reading(START, reading, focus="", inbox_count=-5).inbox_count == 0
