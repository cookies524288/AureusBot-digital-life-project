from datetime import UTC, datetime, timedelta, timezone

import pytest

from aureus.common.time_format import format_duration, format_local_time

MOMENT = datetime(2026, 11, 17, 1, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        (UTC, "2026-11-17 01:05（UTC+00:00）"),
        (timezone(timedelta(hours=8)), "2026-11-17 09:05（UTC+08:00）"),
        (timezone(-timedelta(hours=3, minutes=30)), "2026-11-16 21:35（UTC-03:30）"),
    ],
)
def test_local_time_always_shows_the_offset(zone: timezone, expected: str) -> None:
    assert format_local_time(MOMENT, zone) == expected


def test_local_time_without_zone_uses_the_computer_setting() -> None:
    assert "（UTC" in format_local_time(MOMENT)


def test_local_time_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="时区"):
        format_local_time(datetime(2026, 11, 17), UTC)


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (timedelta(0), "不到 1 分钟"),
        (timedelta(seconds=59), "不到 1 分钟"),
        (timedelta(minutes=1), "1 分钟"),
        (timedelta(minutes=59, seconds=59), "59 分钟"),
        (timedelta(hours=1), "1 小时"),
        (timedelta(hours=10, minutes=5), "10 小时 5 分钟"),
        (timedelta(days=1), "1 天"),
        (timedelta(days=3, hours=2, minutes=40), "3 天 2 小时"),
        (timedelta(days=400), "400 天"),
    ],
)
def test_duration_uses_at_most_two_units(duration: timedelta, expected: str) -> None:
    assert format_duration(duration) == expected


def test_negative_duration_is_rejected() -> None:
    with pytest.raises(ValueError, match="负数"):
        format_duration(timedelta(seconds=-1))
