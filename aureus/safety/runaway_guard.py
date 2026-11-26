"""失控拦截 runaway_guard（D39，06 号文档 12.5 节）。

每个器官分开计算：
- 最近 1 分钟放行的调用达到上限，就拒绝；
- 连续失败达到次数，就暂停一段时间；暂停结束后只要再失败一次就再暂停（熔断器的「半开」），
  成功一次，连续失败数清零。
- 模型测试（`model_test`）不因连续失败暂停，每分钟频率限制照常（D132）。

状态只放在内存里，重启后清零。来源：器官调用的次数和成败；影响：拒绝调用；
开关：`SafetyConfig.runaway_guard_enabled`（D5）。

没用 pybreaker、aiolimiter 这类现成的库：它们直接读系统时间，换不了时钟，
用加速时钟做实验时就不准（D26）。
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta

from aureus.common import Clock, SafetyConfig

logger = logging.getLogger(__name__)

# 频率限制看的时间窗口：「每分钟最多 N 次」里的那一分钟。
RATE_WINDOW = timedelta(minutes=1)

# D132：模型测试只受频率限制，不因连续失败暂停；其它器官照常。
FAILURE_PAUSE_EXEMPT_ORGANS = frozenset({"model_test"})


class RunawayGuard:
    """失控拦截。先用 `blocked_reason` 检查，放行后用 `admit` 记一次，调用结束后用 `report`。"""

    def __init__(self, config: SafetyConfig, clock: Clock) -> None:
        self._config = config
        self._clock = clock
        self._recent_calls: dict[str, deque[datetime]] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._paused_until: dict[str, datetime] = {}

    @property
    def enabled(self) -> bool:
        """失控拦截是否开着。"""
        return self._config.runaway_guard_enabled

    def blocked_reason(self, organ_id: str) -> str | None:
        """检查这个器官现在能不能调用。只检查，不记任何东西。

        Returns:
            不能调用时返回原因（中文），可以调用时返回 None。
        """
        if not self.enabled:
            return None
        now = self._clock.now()

        paused_until = self._paused_until.get(organ_id)
        if paused_until is not None and now < paused_until:
            remaining = max(1, round((paused_until - now).total_seconds()))
            return (
                f"连续失败 {self._consecutive_failures.get(organ_id, 0)} 次，"
                f"已暂停调用，还要等 {remaining} 秒"
            )

        calls_in_window = self._calls_in_window(organ_id, now)
        if calls_in_window >= self._config.max_calls_per_minute:
            return (
                f"最近 1 分钟已经调用 {calls_in_window} 次，"
                f"达到上限 {self._config.max_calls_per_minute} 次"
            )
        return None

    def admit(self, organ_id: str) -> None:
        """记下放行了一次调用，算进频率窗口。必须在检查之后、真正调用之前马上做。"""
        if not self.enabled:
            return
        self._recent_calls.setdefault(organ_id, deque()).append(self._clock.now())

    def report(self, organ_id: str, *, succeeded: bool) -> None:
        """告诉失控拦截一次调用的结果。

        Args:
            organ_id: 器官。
            succeeded: 这次调用整体是否成功（全部模型都失败才算失败）。
        """
        if not self.enabled:
            return
        if succeeded:
            self._consecutive_failures[organ_id] = 0
            return
        if organ_id in FAILURE_PAUSE_EXEMPT_ORGANS:
            # D132：模型测试的失败不累计，只受频率限制。
            return
        failures = self._consecutive_failures.get(organ_id, 0) + 1
        self._consecutive_failures[organ_id] = failures
        # 不把失败数清零：暂停结束后再失败一次，数字还在上限以上，就会再次暂停。
        if failures >= self._config.max_consecutive_failures:
            pause = timedelta(seconds=self._config.failure_pause_seconds)
            self._paused_until[organ_id] = self._clock.now() + pause
            logger.warning(
                "失控拦截：器官 %s 连续失败 %d 次，暂停调用 %d 秒",
                organ_id,
                failures,
                self._config.failure_pause_seconds,
            )

    def _calls_in_window(self, organ_id: str, now: datetime) -> int:
        calls = self._recent_calls.get(organ_id)
        if calls is None:
            return 0
        # 恰好 1 分钟前的调用已经不算：窗口是左开右闭。
        while calls and now - calls[0] >= RATE_WINDOW:
            calls.popleft()
        return len(calls)
