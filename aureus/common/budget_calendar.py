"""日历 calendar：「一天」和「一个月」从哪里算起（D53 第 4 条，路线图 1.2）。

一天从当地凌晨 4 点开始，到第二天凌晨 4 点结束；每月 1 日凌晨 4 点算新的一个月。
每日额度（04 号文档第 4 节）按「本月剩余 ÷ 本月剩余天数」算，月和日的边界必须是同一套，
所以账本的月度统计和身体的每日额度都从这里取边界。

时间在程序里一律是 UTC（D26），这里只负责判断「这个时刻在当地算哪一天」，
返回的起止仍然是 UTC。时区不给就用这台电脑设置的时区，夏令时由系统规则处理。
"""

from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aureus.common.clock import ensure_aware

# 凌晨 4 点分界：半夜还没睡的时间算前一天，用户熬夜聊天不会一过零点就换了新的一天额度。
DEFAULT_DAY_START_HOUR = 4

_HOURS_PER_DAY = 24
_DECEMBER = 12


def load_timezone(name: str) -> tzinfo:
    """按名字（比如 Asia/Shanghai）取时区。

    Raises:
        ValueError: 没有这个时区。
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        # 为什么不回显名字：名字来自配置文件，和其他配置错误一样只指出字段，不带填进去的值。
        raise ValueError("不认识这个时区名，要写 IANA 名字，比如 Asia/Shanghai") from None


class BudgetCalendar:
    """判断某个时刻属于哪一天、哪个月，并给出它们的起止（UTC，前含后不含）。"""

    def __init__(
        self, timezone: tzinfo | None = None, day_start_hour: int = DEFAULT_DAY_START_HOUR
    ) -> None:
        """创建日历。

        Args:
            timezone: 按哪个时区算；None 表示这台电脑设置的时区。
            day_start_hour: 一天从当地几点开始，0 到 23。

        Raises:
            ValueError: 开始时刻不在 0 到 23 之间。
        """
        if not 0 <= day_start_hour < _HOURS_PER_DAY:
            raise ValueError(f"一天的开始时刻要在 0 到 23 之间，收到 {day_start_hour}")
        self._timezone = timezone
        self._day_start_hour = day_start_hour

    @property
    def timezone(self) -> tzinfo | None:
        """用的时区；None 表示这台电脑设置的时区。"""
        return self._timezone

    def day_of(self, moment: datetime) -> date:
        """这个时刻在当地算哪一天（凌晨 4 点前算前一天）。

        Raises:
            ValueError: 时间不带时区。
        """
        wall_clock = ensure_aware(moment).astimezone(self._timezone).replace(tzinfo=None)
        # 在「墙上时间」上减，而不是在 UTC 上减：这样夏令时那天也是当地的凌晨 4 点分界。
        return (wall_clock - timedelta(hours=self._day_start_hour)).date()

    def day_bounds(self, moment: datetime) -> tuple[datetime, datetime]:
        """算出这个时刻所在的一天的起止。

        Returns:
            （这天第一刻, 下一天第一刻），UTC，前者包含、后者不包含。

        Raises:
            ValueError: 时间不带时区。
        """
        today = self.day_of(moment)
        return self._start_of(today), self._start_of(today + timedelta(days=1))

    def month_bounds(self, moment: datetime) -> tuple[datetime, datetime]:
        """算出这个时刻所在的一个月的起止（从 1 日的开始时刻到下月 1 日的开始时刻）。

        Returns:
            （本月第一刻, 下月第一刻），UTC，前者包含、后者不包含。

        Raises:
            ValueError: 时间不带时区。
        """
        first = self.month_start_day(moment)
        return self._start_of(first), self._start_of(_next_month(first))

    def month_start_day(self, moment: datetime) -> date:
        """这个时刻所在的月份的 1 日。月份看的是「当地的哪一天」，所以 1 日凌晨 3 点还算上个月。"""
        today = self.day_of(moment)
        return today.replace(day=1)

    def days_left_in_month(self, moment: datetime) -> int:
        """本月还剩几天，包括今天，所以最少是 1（04 号文档：每日额度 = 本月剩余 ÷ 剩余天数）。"""
        today = self.day_of(moment)
        return (_next_month(today.replace(day=1)) - today).days

    def _start_of(self, day: date) -> datetime:
        """某一天的第一刻（当地 day_start_hour 点），换算成 UTC。"""
        wall_clock = datetime(day.year, day.month, day.day, self._day_start_hour)
        if self._timezone is None:
            # 不带时区的时间交给系统按本机时区解释；遇到夏令时的缺口或重叠，系统有固定的取法。
            return wall_clock.astimezone(UTC)
        return wall_clock.replace(tzinfo=self._timezone).astimezone(UTC)


def _next_month(first_day: date) -> date:
    """下个月的 1 日。参数必须是某个月的 1 日。"""
    if first_day.month == _DECEMBER:
        return date(first_day.year + 1, 1, 1)
    return date(first_day.year, first_day.month + 1, 1)
