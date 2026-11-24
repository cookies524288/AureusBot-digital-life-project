"""从工作记忆的近期来源和时间线证据计算临时共情，不改变激素或心情基线（3.15）。"""

from collections.abc import Iterable

from aureus.attention import WorkingMemory
from aureus.common import HormonesConfig
from aureus.heartbeat.events import EVENT_EMPATHY
from aureus.hormones import HormoneTrigger, kind_enabled
from aureus.timeline import ActorType, TimelineDatabase, TimelineQuery, VisibilityScope


class EmpathyFocus:
    """只读取焦点内共情；时间线记录原因，不当作持久刺激。"""

    def __init__(
        self, timeline: TimelineDatabase, memory: WorkingMemory | None, config: HormonesConfig | None
    ) -> None:
        self._timeline = timeline
        self._memory = memory
        self._config = config

    def source_ids(self) -> frozenset[int]:
        """可用作共情来源的当前经历。"""
        if self._memory is None or self._config is None or not self._config.enabled:
            return frozenset()
        return self._memory.retained_focus_ids()

    def candidates(self, source_ids: frozenset[int]) -> tuple[HormoneTrigger, ...]:
        """从原始来源仍在焦点的留痕中取候选。"""
        config = self._config
        if not source_ids or config is None:
            return ()
        records = self._timeline.query(
            TimelineQuery(event_type=EVENT_EMPATHY, since_id=min(source_ids), limit=-1)
        )
        return tuple(
            trigger
            for record in records
            if (trigger := HormoneTrigger.from_metadata(record.metadata)) is not None
            and trigger.is_empathy
            and trigger.source_record_id in source_ids
            and kind_enabled(trigger.kind, config)
        )

    def active(self, extra: Iterable[HormoneTrigger] = ()) -> tuple[HormoneTrigger, ...]:
        """同一目标只采用最新来源；同一来源的重复判断不再加量。"""
        source_ids = self.source_ids()
        selected: dict[tuple[int | None, str], HormoneTrigger] = {}
        for trigger in (*self.candidates(source_ids), *extra):
            if trigger.empathy_for is None or trigger.source_record_id not in source_ids:
                continue
            key = (trigger.person_id, trigger.empathy_for.casefold())
            previous = selected.get(key)
            if previous is None or trigger.source_record_id > previous.source_record_id:
                selected[key] = trigger
        return tuple(selected.values())

    def record(self, triggers: Iterable[HormoneTrigger]) -> None:
        """保留来源的对象、好感和量；同一个来源和目标只记一次。"""
        pending = tuple(triggers)
        if not pending:
            return
        source_ids = self.source_ids()
        seen = {_source_key(trigger) for trigger in self.candidates(source_ids)}
        for trigger in pending:
            key = _source_key(trigger)
            if trigger.source_record_id not in source_ids or key in seen:
                continue
            self._timeline.append(
                EVENT_EMPATHY,
                f"{trigger.kind.value} 临时上涨 {trigger.rise:.2f}：{trigger.reason}",
                ActorType.ME,
                visibility=VisibilityScope.PRIVATE,
                metadata=trigger.to_metadata(),
            )
            seen.add(key)


def _source_key(trigger: HormoneTrigger) -> tuple[int, str]:
    return trigger.source_record_id, (trigger.empathy_for or "").casefold()
