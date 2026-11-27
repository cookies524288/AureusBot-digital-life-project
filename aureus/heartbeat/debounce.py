"""固定消息防抖：安静/活跃开批，同人/换人续发，图片与表情包特殊处理（D142）。

移植自 MaiBot 1.3.15 的 src/chat/debounce/controller.py（GPL-3.0，固定提交
062d45cf8bd4272ac57d7ec03fcf405e5a458211）。改动：去掉 asyncio 唤醒事件和全局配置依赖，
只保留按会话的等待状态；时间由调用方从 Aureus 时钟和 InboxMessage.received_at 换算后传入。
来源与许可证登记见 THIRD_PARTY.md。
"""

from dataclasses import dataclass
from typing import Literal

DebounceMode = Literal["fast", "natural", "steady"]


@dataclass(frozen=True)
class WaitTimes:
    """一个档位的四种等待秒数：安静开批、活跃开批、同人续发、换人。"""

    quiet: float
    active: float
    same_author: float
    different_author: float


WAIT_PROFILES: dict[DebounceMode, WaitTimes] = {
    "fast": WaitTimes(1.0, 3.0, 1.0, 0.5),
    "natural": WaitTimes(2.0, 5.0, 2.0, 0.5),
    "steady": WaitTimes(4.0, 7.0, 4.0, 2.0),
}

# 上一条有效消息超过这么久，新批次按「安静」算，否则按「活跃」算。
QUIET_THRESHOLD = 30.0
# 裸图开启新批次时至少等这么久。
BARE_IMAGE_MIN_WAIT = 4.0
# 整批从第一条实际收到起最多等这么久。
BATCH_LIMIT = 8.0
# @或提及后最多等这么久。
MENTION_LIMIT = 1.0


class MessageDebouncer:
    """一个会话一个实例；只管理防抖，不启动后台任务或调用外部接口（D142）。"""

    def __init__(self) -> None:
        self._first: float | None = None
        self._last_arrival: float | None = None
        self._last_author: str | None = None
        self._deadline: float | None = None
        self._urgent: float | None = None
        self.measured_deadline: float | None = None

    @property
    def deadline(self) -> float | None:
        """当前批次的截止时间；没有批次时是 None。"""
        return self._deadline

    def observe(
        self,
        *,
        author: str,
        is_bare_image: bool,
        is_pure_emoji: bool,
        at_bot: bool,
        received_at: float,
        wall_time: float,
        now: float,
        mode: DebounceMode = "natural",
        monotonic_now: float | None = None,
    ) -> str:
        """更新截止时间并返回本次规则说明；预处理耗时也占用 8 秒/1 秒上限。

        Args:
            author: 发消息的人。
            is_bare_image: 没有文字、有非表情图片附件。
            is_pure_emoji: 没有文字、有表情图片附件。
            at_bot: 这条消息 @ 或点名了它。
            received_at: 消息实际收到的时刻（墙钟）。
            wall_time: 现在这一刻的墙钟时间。
            now: 现在这一刻的时间轴，用来算截止时间。
            mode: 等待档位；关闭防抖时调用方不调用这里。
            monotonic_now: 仅测量用的单调时钟读数，不参与防抖决策。

        Returns:
            这次采用的规则说明，中文，用于观察和排查。
        """
        waits = WAIT_PROFILES[mode]
        arrival = now - max(0.0, wall_time - received_at)
        previous_arrival = self._last_arrival
        previous_author = self._last_author
        # 媒体处理乱序时，时间轴不回退：后处理完的旧消息不能把批次起点提前。
        if self._last_arrival is not None:
            arrival = max(arrival, self._last_arrival)
        self._last_arrival = arrival
        self._last_author = author
        new_batch = self._first is None
        if new_batch:
            self._first = arrival
        first = self._first
        assert first is not None
        if at_bot:
            urgent = now - max(0.0, wall_time - received_at) + MENTION_LIMIT
            self._urgent = min(self._urgent, urgent) if self._urgent is not None else urgent
        # 已到期但因模型/工具忙碌尚未消费的批次，不允许被新消息重新延长。
        # 这些消息仍进入原有上下文缓存，一起消费，不为补偿模型忙碌再排一遍队。
        if self._deadline is None or self._deadline > now:
            reasons = []
            limit = first + BATCH_LIMIT
            if self._urgent is not None:
                limit = min(limit, self._urgent)
            if new_batch:
                active = (
                    previous_arrival is not None and arrival - previous_arrival <= QUIET_THRESHOLD
                )
                wait = waits.active if active else waits.quiet
                reasons.append(f"{'活跃' if active else '安静'}开批 {wait:g} 秒")
                if is_bare_image:
                    reasons.append("裸图开批至少 4 秒")
                    wait = max(wait, BARE_IMAGE_MIN_WAIT)
            else:
                wait = waits.same_author if author == previous_author else waits.different_author
                reasons.append(f"{'同人续发' if author == previous_author else '换人'} {wait:g} 秒")
            candidate = arrival + wait
            # 纯表情包仍可把截止时间提前，但不能在已有批次上继续续长。
            if is_pure_emoji and not new_batch and self._deadline is not None:
                reasons.append("纯表情包不延长等待")
                candidate = min(candidate, self._deadline)
            if candidate > first + BATCH_LIMIT:
                reasons.append("受整批 8 秒上限限制")
            if self._urgent is not None and candidate > self._urgent:
                reasons.append("受@或提及 1 秒上限限制")
            self._deadline = min(candidate, limit)
            if monotonic_now is not None:
                self.measured_deadline = monotonic_now + self._deadline - now
            reason = "；".join(reasons)
        else:
            reason = "批次已到期，不再延长等待"
        return reason

    def consume(self) -> None:
        """本批消息已收走；只保留上一条的时间和发言人，用于下一批判断。"""
        self._first = self._deadline = self._urgent = None
        self.measured_deadline = None
