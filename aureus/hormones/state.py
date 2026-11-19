"""激素的内存状态：记着还没消退的触发，重启时从时间线记录还原（D69 第 1、5 条）。

只管内存里的触发列表，不读写时间线：写记录和读记录由心跳或 runtime 做。
"""

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta

from aureus.common import HormonesConfig
from aureus.hormones.hormones import HormoneKind, HormoneTrigger, level_at

logger = logging.getLogger(__name__)

# 触发过了这么多个半衰期，剩下不到 1/256，当作消退，不再占内存、不再参与计算。
_HALF_LIVES_TO_FORGET = 8


class Hormones:
    """还没消退的触发的集合，按需算出某一刻的浓度。"""

    def __init__(self, config: HormonesConfig) -> None:
        self._config = config
        self._triggers: list[HormoneTrigger] = []

    @property
    def config(self) -> HormonesConfig:
        """这套激素用的配置。"""
        return self._config

    @property
    def triggers(self) -> tuple[HormoneTrigger, ...]:
        """现在记着的触发（只读）。"""
        return tuple(self._triggers)

    def lookback(self) -> timedelta:
        """重启时要读多久以内的 hormone_triggered 记录：最长半衰期的 8 倍（D69 第 5 条）。"""
        longest = max(
            self._config.anger_half_life_seconds,
            self._config.joy_half_life_seconds,
            self._config.hurt_half_life_seconds,
            self._config.surprise_half_life_seconds,
        )
        return timedelta(seconds=longest * _HALF_LIVES_TO_FORGET)

    def add(self, triggers: Iterable[HormoneTrigger], now: datetime) -> None:
        """记下新触发，顺手丢掉已经消退的。"""
        self._triggers.extend(triggers)
        self._forget_faded(now)

    def restore(self, records: Iterable[Mapping[str, object]], now: datetime) -> int:
        """启动时从时间线里 hormone_triggered 记录的 metadata 还原触发。

        看不懂的记录跳过并记一条警告，不影响其他记录；停机的时间照样算作消退。

        Returns:
            还原了几条（已经消退的不算）。
        """
        restored = []
        for metadata in records:
            trigger = HormoneTrigger.from_metadata(metadata)
            if trigger is None:
                logger.warning("看不懂的 hormone_triggered 记录，已跳过：%r", metadata)
                continue
            restored.append(trigger)
        self._triggers = []
        self.add(restored, now)
        return len(self._triggers)

    def level(self, kind: HormoneKind, now: datetime, person_id: int | None = None) -> float:
        """某一刻某种激素的浓度；person_id 给了就只算针对这个人的触发。"""
        return level_at(self._triggers, kind, now, self._config, person_id)

    def _forget_faded(self, now: datetime) -> None:
        horizon = now - self.lookback()
        self._triggers = [t for t in self._triggers if t.triggered_at >= horizon]
