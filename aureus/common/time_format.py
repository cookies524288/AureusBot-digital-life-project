"""公共地基 common：把时间和时长写成给人看的中文。

程序内部的时间一律是带时区的 UTC（D26），只有给人看时才转成本地时间，
而且总是带上 UTC 偏移：时间线里的文字会一直留着，以后换了时区也不会看错。
"""

from datetime import datetime, timedelta, tzinfo

from aureus.common.clock import ensure_aware

_MINUTE = timedelta(minutes=1)
_HOUR = timedelta(hours=1)
_DAY = timedelta(days=1)


def format_local_time(moment: datetime, timezone: tzinfo | None = None) -> str:
    """把时间写成本地时间，带 UTC 偏移，比如 “2026-11-17 09:05（UTC+08:00）”。

    Args:
        moment: 带时区的时间。
        timezone: 显示用的时区；不给就用这台电脑设置的时区（只读时区设置，不读系统时间）。

    Returns:
        精确到分钟的本地时间。

    Raises:
        ValueError: moment 不带时区。
    """
    local = ensure_aware(moment).astimezone(timezone)
    offset = local.utcoffset() or timedelta(0)
    sign = "-" if offset < timedelta(0) else "+"
    hours, remainder = divmod(abs(offset), _HOUR)
    minutes = remainder // _MINUTE
    return f"{local:%Y-%m-%d %H:%M}（UTC{sign}{hours:02d}:{minutes:02d}）"


def format_duration(duration: timedelta) -> str:
    """把时长写成中文，最多两个单位，比如 “3 天 2 小时”“10 小时 5 分钟”。

    Args:
        duration: 时长，不能是负数。

    Returns:
        不足一分钟时是 “不到 1 分钟”；整天、整小时不写多余的 “0 分钟”。

    Raises:
        ValueError: duration 是负数。
    """
    if duration < timedelta(0):
        raise ValueError(f"时长不能是负数：{duration!r}")
    if duration < _MINUTE:
        return "不到 1 分钟"
    days, remainder = divmod(duration, _DAY)
    hours, remainder = divmod(remainder, _HOUR)
    minutes = remainder // _MINUTE
    # 超过一天时分钟数没什么意义，只写到小时。
    parts = [(days, "天"), (hours, "小时")] if days else [(hours, "小时"), (minutes, "分钟")]
    return " ".join(f"{amount} {unit}" for amount, unit in parts if amount)
