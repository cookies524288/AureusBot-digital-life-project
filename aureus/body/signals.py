"""疲劳、压力、睡眠：把真实来源算成读数（04 号文档第 5 节，D57）。

和精力一样是纯计算：数字由 Body 从账本和时钟取好传进来，这里不读任何状态。
关掉的信号返回 enabled=False 的读数，数字归零、不算偏高，调用方不用另外判断开关；
但显示的地方要看 enabled，关掉的信号不能说成「正常」。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from math import exp

from aureus.common import BodyConfig

# 自检通知里最多列几条失败原因：够定位问题，又不会刷屏。
MAX_FAILURE_REASONS = 3


@dataclass(frozen=True)
class FatigueReading:
    """疲劳：最近一段时间调用得有多密。"""

    enabled: bool
    # [0, 1]；窗口里的调用次数 ÷ 满额次数，超过 1 按 1 算。
    level: float = 0.0
    calls_in_window: int = 0
    high: bool = False


@dataclass(frozen=True)
class StressReading:
    """压力：最近调用失败得多不多，加上排队的消息有多少。"""

    enabled: bool
    # [0, 1]；失败率和积压两项里较大的一项。取较大而不是相加，是为了说得清：
    # 压力高就是其中一项高，自检通知能直接指出是哪一项。
    level: float = 0.0
    failure_rate: float = 0.0
    # 统计了最近多少次调用、其中失败了几次。
    sampled_calls: int = 0
    failed_calls: int = 0
    backlog: int = 0
    high: bool = False
    # 最近失败的原因，新的在前，去重，最多 MAX_FAILURE_REASONS 条。
    recent_failure_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class SleepReading:
    """睡眠：现在是不是夜间时段。停机时长不在这里，由心跳醒来时算（downtime）。"""

    enabled: bool
    night: bool = False


@dataclass(frozen=True)
class SocialNeedReading:
    """社交需求：离上一次和用户有效对话过去了多久（D76）。"""

    enabled: bool
    level: float = 0.0
    hours_since: float | None = None
    high: bool = False


def compute_social_need(
    last_conversation_at: datetime | None, now: datetime, config: BodyConfig
) -> SocialNeedReading:
    """按有效对话的间隔计算社交需求，时钟倒退按刚聊过处理（D76）。"""
    if not config.social_need_enabled:
        return SocialNeedReading(enabled=False)
    if last_conversation_at is None:
        # 没有聊过就是最想聊的时候；主动联系默认关，打开后还得知道用户在哪个通道。
        return SocialNeedReading(enabled=True, level=1.0, high=True)
    hours_since = max(0.0, (now - last_conversation_at).total_seconds()) / 3600
    level = 1 - exp(-hours_since / config.social_need_time_constant_hours)
    return SocialNeedReading(
        enabled=True, level=level, hours_since=hours_since, high=level >= config.social_need_high_at
    )


def compute_fatigue(calls_in_window: int, config: BodyConfig) -> FatigueReading:
    """按窗口里的调用次数算疲劳。关了开关时返回 enabled=False 的读数。"""
    if not config.fatigue_enabled:
        return FatigueReading(enabled=False)
    calls = max(calls_in_window, 0)
    level = min(calls / config.fatigue_full_calls, 1.0)
    return FatigueReading(
        enabled=True,
        level=level,
        calls_in_window=calls,
        high=level >= config.fatigue_high_at,
    )


def compute_stress(
    recent_outcomes: Sequence[tuple[bool, str | None]], backlog: int, config: BodyConfig
) -> StressReading:
    """按最近的调用结果和排队消息数算压力。

    Args:
        recent_outcomes: 最近的调用，从新到旧，每项是（是否成功，失败原因）；
            只看前 stress_window_calls 条。
        backlog: 收到了还没回复的消息数。
        config: 身体配置。
    """
    if not config.stress_enabled:
        return StressReading(enabled=False)
    window = recent_outcomes[: config.stress_window_calls]
    failures = [reason for succeeded, reason in window if not succeeded]
    # 分母至少是窗口大小：刚启动时调用很少，一次失败不该直接算成压力 100%。
    failure_rate = len(failures) / max(len(window), config.stress_window_calls)
    backlog = max(backlog, 0)
    backlog_level = min(backlog / config.stress_backlog_full, 1.0)
    level = max(failure_rate, backlog_level)
    return StressReading(
        enabled=True,
        level=level,
        failure_rate=failure_rate,
        sampled_calls=len(window),
        failed_calls=len(failures),
        backlog=backlog,
        high=level >= config.stress_high_at,
        recent_failure_reasons=_distinct_reasons(failures),
    )


def _distinct_reasons(reasons: Sequence[str | None]) -> tuple[str, ...]:
    distinct: list[str] = []
    for reason in reasons:
        text = (reason or "原因没有记录").strip()
        if text not in distinct:
            distinct.append(text)
        if len(distinct) == MAX_FAILURE_REASONS:
            break
    return tuple(distinct)


def compute_sleep(local_hour: int, config: BodyConfig) -> SleepReading:
    """按当地钟点判断是不是夜间。开始大于结束表示跨过午夜（比如 23 点到 7 点）。

    夜间包含开始的那个钟点，不包含结束的那个钟点。
    """
    if not config.sleep_enabled:
        return SleepReading(enabled=False)
    start, end = config.night_start_hour, config.night_end_hour
    night = start <= local_hour < end if start < end else local_hour >= start or local_hour < end
    return SleepReading(enabled=True, night=night)
