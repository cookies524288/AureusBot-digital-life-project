"""D148：一轮回复内的单调时钟测量，不影响任何调度或投递决策。"""

from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic
from typing import Literal

type Phase = Literal["context_ms", "planner_ms", "replyer_ms", "split_ms"]


def timing_now() -> float:
    """测量实际经过的时间，不随系统时钟跳变或模拟生理时钟加速。"""
    return monotonic()


class RoundTiming:
    """记录真实发生的阶段；缺失的阶段不冒充零耗时。"""

    def __init__(self, queued_at: float, debounce_until: float | None = None) -> None:
        self._queued_at = queued_at
        started = timing_now()
        self._elapsed = {
            "inbox_wait_ms": started - queued_at,
            "debounce_ms": max(0.0, min(started, debounce_until) - queued_at)
            if debounce_until is not None
            else 0.0,
        }
        self._confirmed_segments = 0

    @contextmanager
    def phase(self, stage: Phase) -> Iterator[None]:
        """累加此阶段的实际用时（例如规划器一次再审）。"""
        started = timing_now()
        try:
            yield
        finally:
            self._elapsed[stage] = self._elapsed.get(stage, 0.0) + timing_now() - started

    def observe_delivery(self, segment: int | None, total: int) -> dict[str, int]:
        """在确认回执抵达时计时；有文字时，只把正文分段计入主回复。"""
        if segment is not None or total == 0:
            elapsed = timing_now() - self._queued_at
            self._elapsed.setdefault("first_delivery_ms", elapsed)
            self._confirmed_segments += 1
            if self._confirmed_segments == max(1, total):
                self._elapsed["last_delivery_ms"] = elapsed
        return self.metadata()

    def metadata(self) -> dict[str, int]:
        """给追加写入的记录留一份毫秒快照。"""
        return {stage: round(seconds * 1000) for stage, seconds in self._elapsed.items()}
