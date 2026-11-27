"""社交需求的纯计算和身体接线测试（D76）。"""

from datetime import UTC, datetime, timedelta
from math import exp
from pathlib import Path

import pytest

from aureus.body import Body, SocialNeedReading, compute_social_need
from aureus.common import BodyConfig, BudgetCalendar, ManualClock
from aureus.safety import Ledger

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
CONFIG = BodyConfig()


def test_disabled_social_need_is_zero() -> None:
    assert compute_social_need(
        None, NOW, BodyConfig(social_need_enabled=False)
    ) == SocialNeedReading(enabled=False)


def test_never_having_talked_is_the_highest_social_need() -> None:
    assert compute_social_need(None, NOW, CONFIG) == SocialNeedReading(
        enabled=True, level=1.0, high=True
    )


@pytest.mark.parametrize(("hours", "level"), [(0, 0.0), (12, 1 - exp(-1)), (120, 1 - exp(-10))])
def test_social_need_grows_with_time(hours: int, level: float) -> None:
    reading = compute_social_need(NOW - timedelta(hours=hours), NOW, CONFIG)
    assert reading.level == pytest.approx(level)
    assert 0 <= reading.level <= 1
    assert reading.hours_since == hours


def test_clock_moving_backwards_counts_as_just_talked() -> None:
    reading = compute_social_need(NOW + timedelta(hours=1), NOW, CONFIG)
    assert reading.level == reading.hours_since == 0
    assert not reading.high


def test_social_need_is_high_exactly_at_the_threshold() -> None:
    level = 1 - exp(-1)
    assert compute_social_need(
        NOW - timedelta(hours=12), NOW, BodyConfig(social_need_high_at=level)
    ).high
    assert not compute_social_need(
        NOW - timedelta(hours=12), NOW, BodyConfig(social_need_high_at=level + 0.001)
    ).high


def test_body_uses_the_supplied_conversation_time_and_clock(tmp_path: Path) -> None:
    clock = ManualClock(NOW)
    ledger = Ledger(tmp_path / "aureus.db", clock, calendar=BudgetCalendar(UTC))
    ledger.open()
    try:
        body = Body(CONFIG, None, ledger, clock, BudgetCalendar(UTC))
        assert body.read().social_need.level == 1
        assert body.read(last_conversation_at=NOW).social_need.level == 0
        clock.advance(timedelta(hours=12))
        assert body.read(last_conversation_at=NOW).social_need.level == pytest.approx(1 - exp(-1))
    finally:
        ledger.close()
