"""激素计算：浓度 = 触发按半衰期衰减的和（D69 第 1 条）。

这里只算数字，不读写任何状态、不调用器官、不碰时间线。
"""

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aureus.common import HormonesConfig
from aureus.timeline import VisibilityScope


class HormoneKind(StrEnum):
    """激素的种类（09 号文档术语表）。"""

    ANGER = "anger"  # 怒气
    JOY = "joy"  # 愉悦
    HURT = "hurt"  # 低落
    SURPRISE = "surprise"  # 惊讶


@dataclass(frozen=True)
class HormoneTrigger:
    """一次触发：浓度上涨量、时刻、原因。写进时间线 EVENT_HORMONE_TRIGGERED 的 metadata。"""

    kind: HormoneKind
    # 浓度上涨了多少；记录里写这个数，不写触发前后的浓度：
    # 某一刻的浓度等于所有触发衰减后的和，启动时从历史触发重算能得到和运行中一样的结果。
    rise: float
    # 触发的时刻（UTC），由调用方（心跳或启动恢复）从时钟对象取。
    triggered_at: datetime
    # 对象：哪个人触发的，记人物 ID（None 表示不针对某个人，比如反馈触发的惊讶）。
    person_id: int | None
    # 原因：简短的文字（D69：事实摘要里会写，可见范围过滤后可能只写「另一段对话里的事」）。
    reason: str
    # 来源：触发事件在时间线里的记录序号（判断的记录，或者记结果的记录）。
    source_record_id: int
    # 来源记录的可见范围，用于回复别人时决定能不能写原因的细节（D69 第 3 条）。
    source_visibility: VisibilityScope

    def to_metadata(self) -> dict[str, object]:
        """写进时间线 metadata 的样子：可以序列化成 JSON，还原时能重建。"""
        return {
            "kind": self.kind.value,
            "rise": self.rise,
            "triggered_at": self.triggered_at.isoformat(),
            "person_id": self.person_id,
            "reason": self.reason,
            "source_record_id": self.source_record_id,
            "source_visibility": self.source_visibility.value,
        }


def level_at(
    triggers: list[HormoneTrigger],
    kind: HormoneKind,
    now: datetime,
    config: HormonesConfig,
    person_id: int | None = None,
) -> float:
    """算某一刻某种激素的浓度。

    浓度 = 该种激素的所有触发按各自半衰期衰减后的和，封顶 1。
    用闭式解直接算，不按心跳的拍推进，所以结果和心跳快慢无关（D69 第 1 条）。

    Args:
        triggers: 所有触发（会过滤出指定种类、指定对象的）。
        kind: 哪种激素。
        now: 算哪一刻的浓度（UTC），不在触发之前的触发不算。
        config: 配置（用到半衰期）。
        person_id: 只算针对哪个人的触发（None 表示算所有触发，不按人过滤）。

    Returns:
        浓度 [0, 1]，这种激素被关掉时是 0。
    """
    if not config.enabled:
        return 0.0
    enabled_by_kind = {
        HormoneKind.ANGER: config.anger_enabled,
        HormoneKind.JOY: config.joy_enabled,
        HormoneKind.HURT: config.hurt_enabled,
        HormoneKind.SURPRISE: config.surprise_enabled,
    }
    if not enabled_by_kind[kind]:
        return 0.0
    half_life_by_kind = {
        HormoneKind.ANGER: config.anger_half_life_seconds,
        HormoneKind.JOY: config.joy_half_life_seconds,
        HormoneKind.HURT: config.hurt_half_life_seconds,
        HormoneKind.SURPRISE: config.surprise_half_life_seconds,
    }
    half_life_seconds = half_life_by_kind[kind]
    decay_rate = 0.693147 / half_life_seconds  # ln(2)
    total = 0.0
    for trigger in triggers:
        if trigger.kind != kind:
            continue
        if person_id is not None and trigger.person_id != person_id:
            continue
        if trigger.triggered_at > now:
            # 触发时刻在 now 之后，不算。不假设触发已经排好序：重启时从时间线读回来的顺序不保证。
            continue
        elapsed_seconds = (now - trigger.triggered_at).total_seconds()
        decayed = trigger.rise * math.exp(-decay_rate * elapsed_seconds)
        total += decayed
    return min(1.0, total)
