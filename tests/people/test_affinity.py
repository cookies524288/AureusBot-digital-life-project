"""好感账的测试（路线图 2.4，D66 第 3 条、D71）。"""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aureus.common import BudgetCalendar, ManualClock, PeopleConfig
from aureus.people import (
    AffinityBook,
    AffinityEvent,
    AffinityReason,
    PeopleDirectory,
    describe_affinity,
)
from aureus.people.affinity import fall, rise

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
CALENDAR = BudgetCalendar(UTC)  # 一天从凌晨 4 点开始
MIN_CONFIDENCE = 0.3
# 不受上限干扰的配置，单独测公式用。
UNCAPPED = PeopleConfig(daily_rise_cap=1000.0, daily_fall_cap=1000.0, interaction_amount=0.0)


class Env:
    def __init__(self, tmp_path: Path, config: PeopleConfig) -> None:
        self.clock = ManualClock(NOW)
        db = tmp_path / "aureus.db"
        self.people = PeopleDirectory(db, self.clock)
        self.people.open()
        self.user = self.people.ensure_user("阿明", [("terminal", "local")])
        self.stranger = self.people.meet("qq", "10001")
        self.book = AffinityBook(db, self.clock, config, CALENDAR, min_confidence=MIN_CONFIDENCE)
        self.book.open()

    def close(self) -> None:
        self.book.close()
        self.people.close()


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path, UNCAPPED)
    yield environment
    environment.close()


@pytest.fixture
def capped(tmp_path: Path) -> Iterator[Env]:
    environment = Env(tmp_path, PeopleConfig())
    yield environment
    environment.close()


def _event(
    reason: AffinityReason, intensity: float = 1.0, confidence: float = 1.0
) -> AffinityEvent:
    return AffinityEvent(reason, intensity, confidence)


def test_initial_values_differ_for_user_and_others(env: Env) -> None:
    assert env.book.current(env.user.id) == 80.0
    assert env.book.current(env.stranger.id) == 30.0


def test_formula_is_harder_to_rise_when_high_and_harder_to_fall_when_low() -> None:
    assert rise(80.0, 3.0) == pytest.approx(0.6)
    assert rise(30.0, 3.0) == pytest.approx(2.1)
    assert fall(80.0, 4.0) == pytest.approx(3.2)
    assert fall(30.0, 4.0) == pytest.approx(1.2)


def test_praise_and_offense_change_affinity_by_the_formula(env: Env) -> None:
    [change] = env.book.record_judgment(
        env.stranger.id, [_event(AffinityReason.PRAISE, 0.5, 0.8)], source_record_id=7
    )
    # 3 × 0.5 × 0.8 = 1.2，在 30 上打折 0.7
    assert change.delta == pytest.approx(0.84)
    assert change.value == pytest.approx(30.84)
    assert change.source_record_id == 7
    assert env.book.current(env.stranger.id) == pytest.approx(30.84)

    [offense] = env.book.record_judgment(env.user.id, [_event(AffinityReason.OFFENSE)], None)
    assert offense.delta == pytest.approx(-3.2)
    assert env.book.current(env.user.id) == pytest.approx(76.8)


def test_low_confidence_events_are_ignored(env: Env) -> None:
    changes = env.book.record_judgment(
        env.user.id, [_event(AffinityReason.OFFENSE, confidence=MIN_CONFIDENCE - 0.01)], None
    )
    assert changes == []
    assert env.book.current(env.user.id) == 80.0


def test_never_reaches_either_end(env: Env) -> None:
    for _ in range(500):
        env.book.record_judgment(env.user.id, [_event(AffinityReason.PRAISE)], None)
    assert 99.0 < env.book.current(env.user.id) < 100.0
    for _ in range(2000):
        env.book.record_judgment(env.stranger.id, [_event(AffinityReason.OFFENSE)], None)
    assert 0.0 < env.book.current(env.stranger.id) < 1.0


def test_interaction_counts_once_per_day(capped: Env) -> None:
    book, person = capped.book, capped.stranger.id
    [first] = book.record_judgment(person, [], None)
    assert first.reason is AffinityReason.INTERACTION
    assert first.delta == pytest.approx(0.5 * 0.7)
    assert book.record_judgment(person, [], None) == []

    capped.clock.advance(timedelta(days=1))
    [next_day] = book.record_judgment(person, [], None)
    assert next_day.reason is AffinityReason.INTERACTION


def test_daily_caps_stop_spamming(capped: Env) -> None:
    book, person = capped.book, capped.stranger.id
    config = book.config
    for _ in range(20):
        book.record_judgment(person, [_event(AffinityReason.PRAISE)], None)
    rose = sum(change.delta for change in book.history(person))
    assert rose == pytest.approx(config.daily_rise_cap)

    for _ in range(20):
        book.record_judgment(person, [_event(AffinityReason.OFFENSE)], None)
    fell = -sum(change.delta for change in book.history(person) if change.delta < 0)
    assert fell == pytest.approx(config.daily_fall_cap)

    # 新的一天上限重新算
    capped.clock.advance(timedelta(days=1))
    changes = book.record_judgment(person, [_event(AffinityReason.OFFENSE)], None)
    assert any(change.delta < 0 for change in changes)


def test_day_boundary_follows_the_calendar(capped: Env) -> None:
    book, person = capped.book, capped.stranger.id
    capped.clock.advance(timedelta(hours=15, minutes=59))  # 次日 3:59，还是同一天
    assert len(book.record_judgment(person, [], None)) == 1
    capped.clock.advance(timedelta(minutes=2))  # 过了凌晨 4 点，新的一天
    assert len(book.record_judgment(person, [], None)) == 1


def test_changes_are_append_only(env: Env, tmp_path: Path) -> None:
    env.book.record_judgment(env.user.id, [_event(AffinityReason.PRAISE)], None)
    conn = sqlite3.connect(tmp_path / "aureus.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE affinity_changes SET value = 99")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM affinity_changes")
    finally:
        conn.close()


def test_value_survives_reopening(env: Env, tmp_path: Path) -> None:
    env.book.record_judgment(env.user.id, [_event(AffinityReason.OFFENSE)], None)
    env.book.close()
    reopened = AffinityBook(
        tmp_path / "aureus.db", env.clock, PeopleConfig(), CALENDAR, min_confidence=0.3
    )
    reopened.open()
    try:
        assert reopened.current(env.user.id) == pytest.approx(76.8)
    finally:
        reopened.close()
    env.book.open()


def test_changing_initial_value_does_not_rewrite_existing_affinity(
    env: Env, tmp_path: Path
) -> None:
    env.book.record_judgment(env.user.id, [_event(AffinityReason.PRAISE)], None)
    before = env.book.current(env.user.id)
    other = AffinityBook(
        tmp_path / "aureus.db",
        env.clock,
        PeopleConfig(user_initial_affinity=10.0),
        CALENDAR,
        min_confidence=0.3,
    )
    other.open()
    try:
        assert other.current(env.user.id) == before
    finally:
        other.close()


def test_unopened_book_raises(tmp_path: Path) -> None:
    book = AffinityBook(
        tmp_path / "x.db", ManualClock(NOW), PeopleConfig(), CALENDAR, min_confidence=0.3
    )
    with pytest.raises(RuntimeError):
        book.current(1)


def test_describe_affinity() -> None:
    assert "你对用户的好感：80.0 / 100" in describe_affinity(80.0, is_user=True)
    assert "你对这个人的好感：30.5 / 100" in describe_affinity(30.5, is_user=False)
