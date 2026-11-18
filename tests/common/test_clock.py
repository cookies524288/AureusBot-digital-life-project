"""时钟的测试：真实时钟、手动时钟、加速时钟，以及"必须带时区"的规则。"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from aureus.common import AcceleratedClock, ManualClock, SystemClock
from aureus.common.clock import ensure_aware

START = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_system_clock_returns_aware_utc_time() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    # 能读到的应当是现实中的时间，而不是 1970 年之类的默认值。
    assert now.year >= START.year


def test_manual_clock_stays_still_until_advanced() -> None:
    clock = ManualClock(START)
    assert clock.now() == START
    assert clock.now() == START
    clock.advance(timedelta(minutes=5))
    assert clock.now() == START + timedelta(minutes=5)


def test_manual_clock_rejects_going_backwards() -> None:
    clock = ManualClock(START)
    with pytest.raises(ValueError, match="往后拨"):
        clock.advance(timedelta(seconds=-1))


def test_naive_time_is_rejected() -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)  # 故意构造不带时区的时间
    with pytest.raises(ValueError, match="时区"):
        ManualClock(naive)
    with pytest.raises(ValueError, match="时区"):
        ensure_aware(naive)


def test_other_timezones_are_converted_to_utc() -> None:
    beijing = timezone(timedelta(hours=8))
    moment = datetime(2026, 1, 1, 20, 0, 0, tzinfo=beijing)
    assert ensure_aware(moment) == START
    assert ensure_aware(moment).utcoffset() == timedelta(0)


def test_accelerated_clock_starts_at_start_time() -> None:
    real = ManualClock(datetime(2030, 6, 1, tzinfo=UTC))
    clock = AcceleratedClock(start=START, speed=3600, real_clock=real)
    assert clock.now() == START


def test_accelerated_clock_one_real_second_is_one_simulated_hour() -> None:
    real = ManualClock(datetime(2030, 6, 1, tzinfo=UTC))
    clock = AcceleratedClock(start=START, speed=3600, real_clock=real)
    real.advance(timedelta(seconds=1))
    assert clock.now() == START + timedelta(hours=1)
    real.advance(timedelta(seconds=23))
    assert clock.now() == START + timedelta(hours=24)


def test_accelerated_clock_with_speed_one_matches_real_time() -> None:
    real = ManualClock(datetime(2030, 6, 1, tzinfo=UTC))
    clock = AcceleratedClock(start=START, speed=1, real_clock=real)
    real.advance(timedelta(minutes=7))
    assert clock.now() == START + timedelta(minutes=7)


@pytest.mark.parametrize("speed", [0, -1, -3600])
def test_accelerated_clock_rejects_non_positive_speed(speed: float) -> None:
    real = ManualClock(START)
    with pytest.raises(ValueError, match="大于 0"):
        AcceleratedClock(start=START, speed=speed, real_clock=real)
