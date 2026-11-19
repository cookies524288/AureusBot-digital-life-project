"""激素的内存状态：记着还没消退的触发，重启时从时间线记录还原（D69 第 1、5 条）。

只管内存里的触发列表，不读写时间线：写记录和读记录由心跳或 runtime 做。
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from aureus.common import HormonesConfig
from aureus.hormones.effects import Conflict, HormoneEffects, compute_effects
from aureus.hormones.hormones import HormoneKind, HormoneTrigger, level_at

logger = logging.getLogger(__name__)

# 触发过了这么多个半衰期，剩下不到 1/256，当作消退，不再占内存、不再参与计算。
_HALF_LIVES_TO_FORGET = 8


class Hormones:
    """还没消退的触发的集合，按需算出某一刻的浓度。"""

    def __init__(self, config: HormonesConfig) -> None:
        self._config = config
        self._triggers: list[HormoneTrigger] = []
        # 和每个人的冲突计数。只在内存里：重启后算新的一场，最多多回击几次，上限照样管着。
        self._conflicts: dict[int, Conflict] = {}

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
        new_triggers = list(triggers)
        # 新触发来之前先收尾已经平息的冲突，免得新的一场接着旧的计数算。
        for person_id in {t.person_id for t in new_triggers}:
            self._live_conflict(person_id, now)
        self._triggers.extend(new_triggers)
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

    def effects(
        self,
        now: datetime,
        *,
        person_id: int | None,
        viewer_is_user: bool,
        extra: Sequence[HormoneTrigger] = (),
    ) -> HormoneEffects:
        """此刻激素对回复这个人的影响（D69 第 3 条）。

        Args:
            now: 此刻（UTC）。
            person_id: 回复的对象；认不出是谁时为 None。
            viewer_is_user: 回复的对象是用户。
            extra: 刚算出、还没正式记下的触发。后台任务里只算不写（D41），
                所以回复这一次要把它们算进去，但不改内存。
        """
        return compute_effects(
            [*self._triggers, *extra],
            self._conflicts.get(person_id, Conflict()) if person_id is not None else Conflict(),
            now=now,
            config=self._config,
            person_id=person_id,
            viewer_is_user=viewer_is_user,
        )

    def note_reply(self, person_id: int | None, now: datetime, *, retaliated: bool) -> None:
        """记一次回复：回击了就算一次，连续不回的计数清零。"""
        conflict = self._live_conflict(person_id, now)
        if person_id is None or conflict is None:
            return
        self._conflicts[person_id] = Conflict(
            retaliations=conflict.retaliations + (1 if retaliated else 0), declines=0
        )

    def note_decline(self, person_id: int | None, now: datetime) -> None:
        """记一次不回：连续不回的计数加一。"""
        conflict = self._live_conflict(person_id, now)
        if person_id is None or conflict is None:
            return
        self._conflicts[person_id] = Conflict(conflict.retaliations, conflict.declines + 1)

    def _live_conflict(self, person_id: int | None, now: datetime) -> Conflict | None:
        """这个人的冲突计数；怒气已回落到平息线以下就当这场冲突结束，计数清掉。"""
        if person_id is None:
            return None
        anger = self.level(HormoneKind.ANGER, now, person_id)
        if anger < self._config.calm_below:
            self._conflicts.pop(person_id, None)
            return Conflict()
        return self._conflicts.get(person_id, Conflict())
