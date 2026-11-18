"""醒来时算出停机多久（D41 第 5 条；04 号文档“睡眠”一节）。

依据是时间线上最后一条记录：
- 是关闭记录 shut_down：知道是什么时候停的，停机时长是准确的；
- 不是：上次没来得及写关闭记录（断电、崩溃、被强行结束），进程停在最后一条记录之后的
  某个时刻，算出来的是“最多停了多久”；
- 最后一条记录比现在还晚：时钟被调过，或者上次用的是加速时钟。这时停机时长是未知，
  不算成负数，也不当成 0。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Any

from aureus.common.time_format import format_duration, format_local_time
from aureus.heartbeat.events import EVENT_SHUT_DOWN
from aureus.timeline import TimelineRecord


@dataclass(frozen=True)
class Downtime:
    """这次醒来前停了多久。"""

    woke_at: datetime
    # 时间线上最后一条记录的时间；None 表示时间线是空的，这是第一次醒来。
    last_record_at: datetime | None
    # 最后一条是不是关闭记录：是的话 duration 是准确值，不是的话是上限。
    shutdown_recorded: bool
    # 停了多久；第一次醒来、或者最后一条记录比现在还晚时是 None。
    duration: timedelta | None

    @property
    def is_first_wake(self) -> bool:
        """时间线是空的：从来没有运行过。"""
        return self.last_record_at is None

    def describe(self, timezone: tzinfo | None = None) -> str:
        """写成一句中文，记进时间线，也放进给器官的上下文。

        Args:
            timezone: 显示用的时区；不给就用这台电脑设置的时区。

        Returns:
            比如 “上次在 2026-11-16 22:10（UTC+08:00）关闭，停了 10 小时 5 分钟。”
        """
        if self.last_record_at is None:
            return "这是第一次醒来，时间线上还没有任何记录。"
        last = format_local_time(self.last_record_at, timezone)
        if self.duration is None:
            return (
                f"时间线上最后一条记录的时间（{last}）比现在还晚，可能调过系统时钟，"
                "或者上次用的是加速时钟，所以不知道停了多久。"
            )
        duration = format_duration(self.duration)
        # 中文和数字之间留空格（“停了 3 小时”），“不到 1 分钟”前面不留（“停了不到 1 分钟”）。
        spacing = " " if duration[:1].isdigit() else ""
        if self.shutdown_recorded:
            return f"上次在 {last}关闭，停了{spacing}{duration}。"
        return (
            "上次没有留下关闭记录（可能是断电、崩溃或被强行结束），"
            f"最后一条记录在 {last}，所以最多停了{spacing}{duration}。"
        )

    def to_metadata(self) -> dict[str, Any]:
        """写进时间线 metadata 的字段，只用 JSON 能表示的值。"""
        return {
            "woke_at": self.woke_at.isoformat(),
            "last_record_at": self.last_record_at.isoformat() if self.last_record_at else None,
            "shutdown_recorded": self.shutdown_recorded,
            "downtime_seconds": self.duration.total_seconds() if self.duration else None,
        }


def assess_downtime(last_record: TimelineRecord | None, now: datetime) -> Downtime:
    """根据时间线上最后一条记录，算出停机多久。

    Args:
        last_record: 时间线上最后一条记录；时间线是空的就传 None。
        now: 现在（从时钟取）。

    Returns:
        停机情况。
    """
    if last_record is None:
        return Downtime(woke_at=now, last_record_at=None, shutdown_recorded=False, duration=None)
    gap = now - last_record.timestamp
    return Downtime(
        woke_at=now,
        last_record_at=last_record.timestamp,
        shutdown_recorded=last_record.event_type == EVENT_SHUT_DOWN,
        duration=gap if gap >= timedelta(0) else None,
    )
