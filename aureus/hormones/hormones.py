"""激素计算和触发规则（D69，路线图 2.3）。

这里只算数字和映射触发规则，不读写任何状态、不调用器官、不碰时间线。
"""

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from aureus.common import HormonesConfig
from aureus.timeline import VisibilityScope

if TYPE_CHECKING:
    from aureus.heartbeat.judging import TowardMe


class HormoneKind(StrEnum):
    """激素的种类（09 号文档术语表）。"""

    ANGER = "anger"  # 怒气
    JOY = "joy"  # 愉悦
    HURT = "hurt"  # 低落
    SURPRISE = "surprise"  # 惊讶


# 判断器官「冲着它来的事」的种类（值和 heartbeat.judging.TowardMeKind 一致）对应的激素和原因。
# 这里按值写字符串，不 import heartbeat：心跳要用本模块，反过来 import 会循环。
_TOWARD_ME_RULES: dict[str, tuple[HormoneKind, str]] = {
    "offense": (HormoneKind.ANGER, "被冒犯了"),
    "praise": (HormoneKind.JOY, "被夸了"),
    "rejection": (HormoneKind.HURT, "被拒绝或冷落了"),
}


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


def make_triggers_from_judgment(
    toward_me: Sequence["TowardMe"],
    *,
    person_id: int,
    source_record_id: int,
    source_visibility: VisibilityScope,
    now: datetime,
    config: HormonesConfig,
    recent_triggers: Iterable[HormoneTrigger],
) -> list[HormoneTrigger]:
    """判断器官报的「冲着它来的事」变成触发（D69 第 2 条）。

    冒犯 → 怒气，夸奖 → 愉悦，拒绝 → 低落。上涨量 = 基础量 × 强度 × 把握，单次不超过上限；
    把握低于 min_confidence 的不算；同一人在窗口里反复触发同一种激素，第 n 次乘以
    repeat_decay 的 n−1 次方（刷屏刷不出情绪）。

    Args:
        toward_me: 判断结果里冲着它来的事。
        person_id: 发消息的人。
        source_record_id: 判断记录在时间线里的序号。
        source_visibility: 判断记录的可见范围。
        now: 触发时刻（UTC，由心跳从时钟对象取）。
        config: 配置。
        recent_triggers: 已有的触发（只数窗口内、这个人、同一种激素的）。

    Returns:
        触发列表，可能是空。
    """
    if not config.enabled:
        return []
    window_start = now - timedelta(seconds=config.repeat_window_seconds)
    earlier_counts: Counter[HormoneKind] = Counter(
        trigger.kind
        for trigger in recent_triggers
        if trigger.person_id == person_id and trigger.triggered_at >= window_start
    )
    triggers: list[HormoneTrigger] = []
    for item in toward_me:
        rule = _TOWARD_ME_RULES.get(item.kind.value)
        if rule is None or item.confidence < config.min_confidence:
            continue
        kind, reason = rule
        # 第 n 次（n 从 1 数）乘以 repeat_decay 的 n−1 次方：已有 k 次，这次乘 decay 的 k 次方。
        decay = config.repeat_decay ** earlier_counts[kind]
        rise = min(
            config.base_amount * item.intensity * item.confidence * decay, config.max_single_rise
        )
        if rise <= 0.0:
            continue
        earlier_counts[kind] += 1  # 同一批里的第二条也算刷屏
        triggers.append(
            HormoneTrigger(
                kind=kind,
                rise=rise,
                triggered_at=now,
                person_id=person_id,
                reason=reason,
                source_record_id=source_record_id,
                source_visibility=source_visibility,
            )
        )
    return triggers


def make_triggers_from_feedback(
    *,
    expected: float | None,
    succeeded: bool,
    source_record_id: int,
    source_visibility: VisibilityScope,
    now: datetime,
    config: HormonesConfig,
) -> list[HormoneTrigger]:
    """反馈结果变成触发（D69 第 2 条）。

    比预期顺利 → 愉悦，比预期糟糕 → 低落，差得很多（差距不小于 surprise_gap）→ 再加惊讶。
    冲动（没写预期）不触发：没有预期就谈不上意外。触发不针对某个人（person_id 是 None）。

    Args:
        expected: 写下的预期把握 [0, 1]，冲动时是 None。
        succeeded: 结果是成功还是失败。
        source_record_id: 记结果的记录序号。
        source_visibility: 来源记录的可见范围。
        now: 触发时刻（UTC）。
        config: 配置。
    """
    if not config.enabled or expected is None:
        return []
    gap = (1.0 if succeeded else 0.0) - expected
    if gap == 0.0:
        return []
    better = gap > 0.0
    rise = min(config.base_amount * abs(gap), config.max_single_rise)
    specs = [
        (
            HormoneKind.JOY if better else HormoneKind.HURT,
            rise,
            "比预期顺利" if better else "比预期糟糕",
        )
    ]
    if abs(gap) >= config.surprise_gap:
        specs.append(
            (
                HormoneKind.SURPRISE,
                min(config.base_amount, config.max_single_rise),
                "结果和预期差得很多",
            )
        )
    return [
        HormoneTrigger(
            kind=kind,
            rise=amount,
            triggered_at=now,
            person_id=None,
            reason=reason,
            source_record_id=source_record_id,
            source_visibility=source_visibility,
        )
        for kind, amount, reason in specs
    ]


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
