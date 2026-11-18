"""日历的测试：凌晨 4 点分界、跨月、夏令时、换时区（D53 第 4 条，路线图 1.2）。"""

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from aureus.common import BudgetCalendar, load_timezone

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
BEIJING_FIXED = timezone(timedelta(hours=8))


def shanghai(*args: int) -> datetime:
    return datetime(*args, tzinfo=SHANGHAI)


# ---------- 一天 ----------


def test_before_four_in_the_morning_still_belongs_to_the_previous_day() -> None:
    calendar = BudgetCalendar(SHANGHAI)
    assert calendar.day_of(shanghai(2026, 11, 18, 3, 59, 59)) == date(2026, 11, 17)
    assert calendar.day_of(shanghai(2026, 11, 18, 4)) == date(2026, 11, 18)
    assert calendar.day_of(shanghai(2026, 11, 18, 23, 59)) == date(2026, 11, 18)


def test_day_bounds_are_returned_in_utc() -> None:
    start, end = BudgetCalendar(SHANGHAI).day_bounds(shanghai(2026, 11, 18, 10))
    assert start == datetime(2026, 11, 17, 20, tzinfo=UTC)  # 当地 18 日凌晨 4 点
    assert end == datetime(2026, 11, 18, 20, tzinfo=UTC)
    assert start.utcoffset() == timedelta(0)


def test_the_same_instant_is_the_same_day_whatever_offset_it_is_written_in() -> None:
    calendar = BudgetCalendar(SHANGHAI)
    moment = datetime(2026, 11, 17, 19, 30, tzinfo=UTC)  # 当地 18 日 3:30
    assert calendar.day_of(moment) == date(2026, 11, 17)
    assert calendar.day_of(moment.astimezone(NEW_YORK)) == date(2026, 11, 17)


def test_custom_day_start_hour() -> None:
    calendar = BudgetCalendar(SHANGHAI, day_start_hour=0)
    assert calendar.day_of(shanghai(2026, 11, 18, 0)) == date(2026, 11, 18)
    assert calendar.day_of(shanghai(2026, 11, 17, 23, 59)) == date(2026, 11, 17)


@pytest.mark.parametrize("hour", [-1, 24, 100])
def test_day_start_hour_must_be_a_real_hour(hour: int) -> None:
    with pytest.raises(ValueError, match="0 到 23"):
        BudgetCalendar(SHANGHAI, day_start_hour=hour)


def test_naive_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="时区"):
        BudgetCalendar(SHANGHAI).day_of(datetime(2026, 11, 18, 12))


# ---------- 一个月 ----------


def test_the_month_changes_at_four_in_the_morning_on_the_first() -> None:
    calendar = BudgetCalendar(SHANGHAI)
    before = shanghai(2026, 12, 1, 3, 59, 59)
    after = shanghai(2026, 12, 1, 4)
    assert calendar.month_start_day(before) == date(2026, 11, 1)
    assert calendar.month_start_day(after) == date(2026, 12, 1)
    start, end = calendar.month_bounds(after)
    assert start == after.astimezone(UTC)
    assert end == shanghai(2027, 1, 1, 4).astimezone(UTC)


def test_month_bounds_contain_the_moment_and_chain_without_gaps() -> None:
    calendar = BudgetCalendar(SHANGHAI)
    moment = datetime(2026, 11, 1, tzinfo=UTC)
    for _ in range(30):
        start, end = calendar.month_bounds(moment)
        assert start <= moment < end
        # 下一个月的开始正好是这个月的结束，中间没有缝也没有重叠。
        assert calendar.month_bounds(end)[0] == end
        assert calendar.month_bounds(end - timedelta(microseconds=1)) == (start, end)
        moment += timedelta(days=11, hours=7)


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (shanghai(2026, 11, 1, 4), 30),  # 11 月第一天：整个月 30 天
        (shanghai(2026, 11, 30, 10), 1),  # 最后一天：只剩今天
        (shanghai(2026, 12, 1, 3), 1),  # 12 月 1 日凌晨 3 点还是 11 月 30 日
        (shanghai(2026, 12, 1, 4), 31),
        (shanghai(2028, 2, 10, 10), 20),  # 闰年 2 月 29 天，10 日起还剩 20 天（含今天）
        (shanghai(2027, 2, 10, 10), 19),  # 平年 2 月 28 天
    ],
)
def test_days_left_in_month_includes_today(moment: datetime, expected: int) -> None:
    assert BudgetCalendar(SHANGHAI).days_left_in_month(moment) == expected


def test_year_end_rolls_over_to_january() -> None:
    calendar = BudgetCalendar(SHANGHAI)
    start, end = calendar.month_bounds(shanghai(2026, 12, 31, 23))
    assert (start, end) == (
        shanghai(2026, 12, 1, 4).astimezone(UTC),
        shanghai(2027, 1, 1, 4).astimezone(UTC),
    )


# ---------- 夏令时和换时区 ----------


def test_daylight_saving_days_are_not_exactly_24_hours() -> None:
    calendar = BudgetCalendar(NEW_YORK)
    # 2026-03-08 凌晨 2 点美国夏令时开始，时钟跳过 1 小时：3 月 7 日 4 点到 8 日 4 点只有 23 小时。
    start, end = calendar.day_bounds(datetime(2026, 3, 7, 12, tzinfo=NEW_YORK))
    assert end - start == timedelta(hours=23)
    # 2026-11-01 凌晨 2 点夏令时结束，倒回 1 小时：10 月 31 日 4 点到 11 月 1 日 4 点有 25 小时。
    start, end = calendar.day_bounds(datetime(2026, 10, 31, 12, tzinfo=NEW_YORK))
    assert end - start == timedelta(hours=25)


def test_four_in_the_morning_is_local_wall_clock_across_the_switch() -> None:
    calendar = BudgetCalendar(NEW_YORK)
    for moment in (
        datetime(2026, 3, 9, 4, tzinfo=NEW_YORK),
        datetime(2026, 11, 2, 4, tzinfo=NEW_YORK),
    ):
        start, _ = calendar.day_bounds(moment)
        assert start == moment.astimezone(UTC)
        assert calendar.day_of(start - timedelta(microseconds=1)) == moment.date() - timedelta(
            days=1
        )


def test_a_timezone_change_moves_the_boundary() -> None:
    moment = datetime(2026, 11, 30, 19, 30, tzinfo=UTC)
    # 北京时间 12 月 1 日 3:30，还是 11 月；换成纽约是 11 月 30 日 14:30，也是 11 月。
    assert BudgetCalendar(SHANGHAI).month_start_day(moment) == date(2026, 11, 1)
    # 东京（比北京早一小时）是 12 月 1 日 4:30，已经是 12 月。
    assert BudgetCalendar(ZoneInfo("Asia/Tokyo")).month_start_day(moment) == date(2026, 12, 1)


def test_fixed_offset_timezone_is_accepted() -> None:
    calendar = BudgetCalendar(BEIJING_FIXED)
    assert calendar.day_of(datetime(2026, 11, 17, 19, 59, tzinfo=UTC)) == date(2026, 11, 17)


def test_system_timezone_when_none_is_given() -> None:
    calendar = BudgetCalendar()
    moment = datetime(2026, 11, 18, 12, tzinfo=UTC)
    start, end = calendar.day_bounds(moment)
    assert start <= moment < end
    assert calendar.timezone is None


# ---------- 时区名 ----------


def test_load_timezone_knows_iana_names() -> None:
    assert load_timezone("Asia/Shanghai") == SHANGHAI


@pytest.mark.parametrize("name", ["Mars/Base", "not a zone", "../etc"])
def test_load_timezone_rejects_bad_names_without_echoing_them(name: str) -> None:
    with pytest.raises(ValueError, match="IANA") as caught:
        load_timezone(name)
    assert name not in str(caught.value)
