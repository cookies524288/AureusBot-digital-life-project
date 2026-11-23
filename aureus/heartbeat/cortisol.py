"""用本地时相和已结算的承诺失败推迟可选主动联系（3.8）。"""

from datetime import datetime, timedelta, tzinfo
from math import cos, pi

from aureus.common.clock import ensure_aware
from aureus.common.config import CortisolConfig
from aureus.feedback import FeedbackStore

# Debono 等表 1 的本地晨峰和午夜谷。换成午夜后分钟，供两段余弦插值。
_PEAK_MINUTE = 8 * 60 + 32
_TROUGH_MINUTE = 18
_DAY_MINUTES = 24 * 60
# 超过 12 个半衰期后，每个事件的余量不足峰值的 1/4096。
_LOOKBACK_HALF_LIVES = 12


class CortisolTiming:
    """从同一时钟和持久化反馈重建无量纲读数，无需另外保存状态。"""

    def __init__(
        self, config: CortisolConfig, timezone: tzinfo | None, feedback: FeedbackStore | None
    ) -> None:
        self._config = config
        self._timezone = timezone
        self._feedback = feedback

    def level(self, moment: datetime) -> float:
        """返回当前读数 [0, 1]。关闭时读数为零，也不查询反馈。"""
        config = self._config
        if not config.enabled:
            return 0.0
        now = ensure_aware(moment)
        local = now.astimezone(self._timezone)
        minute = local.hour * 60 + local.minute + local.second / 60 + local.microsecond / 60_000_000
        since_trough = (minute - _TROUGH_MINUTE) % _DAY_MINUTES
        rise_minutes = _PEAK_MINUTE - _TROUGH_MINUTE
        if since_trough <= rise_minutes:
            phase = since_trough / rise_minutes
        else:
            phase = 1 + (since_trough - rise_minutes) / (_DAY_MINUTES - rise_minutes)
        baseline = config.baseline_span * (1 - cos(pi * phase)) / 2
        if self._feedback is None:
            return baseline

        window = config.rise_seconds + _LOOKBACK_HALF_LIVES * config.half_life_seconds
        failures = self._feedback.commitment_failures(now - timedelta(seconds=window), now)
        increment = 0.0
        for failure in failures:
            assert failure.outcome_at is not None
            age = (now - failure.outcome_at).total_seconds()
            if age <= config.rise_seconds:
                fraction = age / config.rise_seconds
            else:
                fraction = 2 ** (-(age - config.rise_seconds) / config.half_life_seconds)
            increment += config.failure_peak * fraction
        return min(1.0, baseline + increment)

    def should_defer(self, moment: datetime) -> bool:
        """调节量达到门槛时，把可选联系留给后续心跳重新决定。"""
        return self.level(moment) >= self._config.defer_at
