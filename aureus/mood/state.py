"""心情的内存状态：记着还没回落的激素贡献，重启时从时间线记录还原（D73 第 3 条）。

反馈结果不在这里存，每次由调用方从反馈表读出来传进来，反馈表本身就是记录。
"""

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta

from aureus.common import MoodConfig
from aureus.hormones import HormoneTrigger
from aureus.mood.mood import (
    FeedbackOutcome,
    MoodContribution,
    MoodReading,
    describe_mood,
    from_outcomes,
    from_triggers,
    valence_at,
)

logger = logging.getLogger(__name__)

# 过了这么多个半衰期，剩下不到 1/256，当作回落完了。
_HALF_LIVES_TO_FORGET = 8


class Mood:
    """心情：激素贡献存在内存里，反馈结果由调用方每次传进来。谨慎状态有滞后。"""

    def __init__(self, config: MoodConfig) -> None:
        self._config = config
        self._contributions: list[MoodContribution] = []
        self._cautious = False

    @property
    def config(self) -> MoodConfig:
        """心情配置。"""
        return self._config

    def lookback(self) -> timedelta:
        """重启时要读多久以内的记录：半衰期的 8 倍。"""
        return timedelta(seconds=self._config.half_life_seconds * _HALF_LIVES_TO_FORGET)

    def add_triggers(self, triggers: Iterable[HormoneTrigger], now: datetime) -> None:
        """记下新的激素触发，顺手丢掉已经回落完的。"""
        self._contributions.extend(from_triggers(triggers, self._config))
        horizon = now - self.lookback()
        self._contributions = [item for item in self._contributions if item.at >= horizon]

    def restore(self, records: Iterable[Mapping[str, object]], now: datetime) -> int:
        """启动时从 hormone_triggered 记录的 metadata 还原；看不懂的跳过并记警告。

        Returns:
            还原了几条贡献（已经回落完的不算）。
        """
        triggers: list[HormoneTrigger] = []
        for metadata in records:
            trigger = HormoneTrigger.from_metadata(metadata)
            if trigger is None:
                logger.warning("心情：看不懂的 hormone_triggered 记录，已跳过：%r", metadata)
                continue
            if not trigger.is_empathy:
                triggers.append(trigger)
        self._contributions = []
        self.add_triggers(triggers, now)
        return len(self._contributions)

    def read(
        self,
        now: datetime,
        outcomes: Sequence[FeedbackOutcome] = (),
        *,
        stress_level: float = 0.0,
        person_id: int | None = None,
        viewer_is_user: bool = True,
        extra: Sequence[HormoneTrigger] = (),
    ) -> MoodReading:
        """此刻的心情，顺便更新谨慎状态（低于谨慎线进入，回到恢复线以上才解除）。

        Args:
            now: 此刻（UTC）。
            outcomes: 最近（lookback 以内）有结果的反馈。
            stress_level: 此刻的压力，0 到 1；压力信号关着时传 0。
            person_id: 回复的对象，决定原因能不能写细节。
            viewer_is_user: 回复的对象是用户。
            extra: 只影响这次读数的临时共情。
        """
        render = self.prepare_reading(
            now,
            outcomes,
            stress_level=stress_level,
            person_id=person_id,
            viewer_is_user=viewer_is_user,
        )
        return render(now, extra)

    def prepare_reading(
        self,
        now: datetime,
        outcomes: Sequence[FeedbackOutcome],
        *,
        stress_level: float,
        person_id: int | None,
        viewer_is_user: bool,
    ) -> Callable[[datetime, Sequence[HormoneTrigger]], MoodReading]:
        """更新基线的谨慎状态，然后返回不写状态的读数快照。"""
        contributions = [*self._contributions, *from_outcomes(outcomes, self._config)]
        valence = valence_at(contributions, now, self._config, stress_level)
        self._cautious = self._is_cautious(valence, self._cautious)
        baseline_cautious = self._cautious

        def render(at: datetime, extra: Sequence[HormoneTrigger]) -> MoodReading:
            effective = [*contributions, *from_triggers(extra, self._config)]
            valence = valence_at(effective, at, self._config, stress_level)
            cautious = self._is_cautious(valence, baseline_cautious)
            summary = describe_mood(
                effective,
                at,
                self._config,
                stress_level=stress_level,
                cautious=cautious,
                person_id=person_id,
                viewer_is_user=viewer_is_user,
            )
            return MoodReading(valence=valence, cautious=cautious, summary=summary)

        return render

    def _is_cautious(self, valence: float, was_cautious: bool) -> bool:
        if valence < self._config.cautious_below:
            return True
        if valence > self._config.recover_above:
            return False
        return was_cautious

    @property
    def cautious(self) -> bool:
        """最近一次 read 时是不是在谨慎状态（给 2.8 主动行为、2.10 审批用，D73 第 5 条）。"""
        return self._cautious
