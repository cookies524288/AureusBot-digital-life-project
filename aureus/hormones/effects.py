"""激素对行为的影响：事实摘要、回击、冷处理、低落、惊讶（D69 第 3 条）。

全是纯函数：输入触发和冲突计数，输出一份「这次该怎么做」。不读写状态、不读系统时间。
回击次数和冷处理次数由代码数，不靠模型自觉。
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aureus.common import HormonesConfig
from aureus.hormones.hormones import (
    HormoneKind,
    HormoneTrigger,
    half_life_seconds_of,
    kind_enabled,
    level_at,
)

# 低于这个浓度的激素不进摘要：几乎消退了，写出来只会让人以为它还在动气。
_SUMMARY_FLOOR = 0.05
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60

_KIND_NAMES = {
    HormoneKind.ANGER: "怒气",
    HormoneKind.JOY: "愉悦",
    HormoneKind.HURT: "低落",
    HormoneKind.SURPRISE: "惊讶",
}
_OTHER_CONVERSATION = "另一段对话里的事"


class RetaliationMode(StrEnum):
    """这次回复里关于回击的说明。"""

    OFF = "off"  # 没有说明
    ALLOWED = "allowed"  # 允许回击
    EXHAUSTED = "exhausted"  # 回击次数用完，改成冷静回应


@dataclass(frozen=True)
class Conflict:
    """和某个人的这场冲突里，已经回击了几次、连续没回了几次。"""

    retaliations: int = 0
    declines: int = 0


@dataclass(frozen=True)
class HormoneEffects:
    """此刻的激素对这次回复的影响。"""

    summary: str
    retaliation: RetaliationMode
    # 规划器可以选择不回这个人（怒气到了冷处理线，且这场冲突里还没连续不回过）。
    may_cold_shoulder: bool
    # 冲突里已经不回过一次，这次必须回。
    must_reply: bool
    shorten_reply: bool
    surprised: bool
    # 针对这个人的怒气，写进 reply_declined 留痕。
    person_anger: float
    retaliations_used: int

    def to_metadata(self) -> dict[str, object]:
        """写进时间线 metadata 的样子，回头查记录时知道当时的怒气和各项限制。"""
        return {
            "person_anger": round(self.person_anger, 3),
            "retaliation": self.retaliation.value,
            "retaliations_used": self.retaliations_used,
            "cold_shoulder_allowed": self.may_cold_shoulder,
            "must_reply": self.must_reply,
            "shorten_reply": self.shorten_reply,
            "surprised": self.surprised,
        }

    def context_text(self, *, plan_first: bool) -> str:
        """放进系统上下文的文字；全都没有时是空字符串。冷处理只在先规划的档位说。"""
        parts = [self.summary] if self.summary else []
        if self.retaliation is RetaliationMode.ALLOWED:
            parts.append(
                "你对这个人正在气头上。这次回复允许质疑、反驳、表达不满，语气可以冲；"
                "不许辱骂、威胁、泄露信息、刷屏，也不许牵扯无关的人。"
            )
        elif self.retaliation is RetaliationMode.EXHAUSTED:
            parts.append(
                f"这场冲突里你已经回击过 {self.retaliations_used} 次，不再回击：冷静地回应，"
                "不再用重话。"
            )
        if plan_first and self.may_cold_shoulder:
            parts.append("你气得很厉害，可以选择这次不回复这个人；回不回由你决定。")
        return "\n".join(parts)


def compute_effects(
    triggers: Sequence[HormoneTrigger],
    conflict: Conflict,
    *,
    now: datetime,
    config: HormonesConfig,
    person_id: int | None,
    viewer_is_user: bool,
) -> HormoneEffects:
    """算此刻激素对回复这个人的影响。

    Args:
        triggers: 还没消退的全部触发（可以包含刚算出、还没正式记下的）。
        conflict: 和这个人的冲突计数；怒气已回落到平息线以下的冲突，调用方传空计数。
        now: 此刻（UTC）。
        config: 配置。
        person_id: 回复的对象；认不出是谁时为 None，不会有针对这个人的行为。
        viewer_is_user: 回复的对象是用户。回复别人时，来源不在这个人对话里的原因不写细节。
    """
    all_triggers = list(triggers)
    anger = (
        level_at(all_triggers, HormoneKind.ANGER, now, config, person_id, exclude_empathy=True)
        if person_id is not None
        else 0.0
    )
    active = anger >= config.calm_below
    if not active:
        conflict = Conflict()
    return HormoneEffects(
        summary=_summary(all_triggers, now, config, person_id, viewer_is_user),
        retaliation=_retaliation_mode(anger, conflict, config),
        may_cold_shoulder=active and anger >= config.cold_shoulder_at and conflict.declines < 1,
        must_reply=active and conflict.declines >= 1,
        shorten_reply=level_at(all_triggers, HormoneKind.HURT, now, config)
        >= config.hurt_shorten_at,
        surprised=level_at(all_triggers, HormoneKind.SURPRISE, now, config) >= config.surprise_at,
        person_anger=anger,
        retaliations_used=conflict.retaliations,
    )


def _retaliation_mode(anger: float, conflict: Conflict, config: HormonesConfig) -> RetaliationMode:
    out_of_retaliations = conflict.retaliations >= config.max_retaliations
    if anger >= config.retaliate_at:
        return RetaliationMode.EXHAUSTED if out_of_retaliations else RetaliationMode.ALLOWED
    if anger >= config.calm_below and out_of_retaliations:
        return RetaliationMode.EXHAUSTED
    return RetaliationMode.OFF


def _summary(
    triggers: list[HormoneTrigger],
    now: datetime,
    config: HormonesConfig,
    person_id: int | None,
    viewer_is_user: bool,
) -> str:
    """各激素的数值和最主要的原因，只写事实。"""
    lines: list[str] = []
    for kind in HormoneKind:
        if not kind_enabled(kind, config):
            continue
        level = level_at(triggers, kind, now, config)
        if level < _SUMMARY_FLOOR:
            continue
        main = _main_trigger(triggers, kind, now, config)
        reason = _describe_reason(main, now, person_id, viewer_is_user) if main else ""
        lines.append(f"- {_KIND_NAMES[kind]} {level:.2f}{reason}")
    if not lines:
        return ""
    return "你此刻的情绪状态（由代码按事实算出，不是演出来的）：\n" + "\n".join(lines)


def _main_trigger(
    triggers: list[HormoneTrigger], kind: HormoneKind, now: datetime, config: HormonesConfig
) -> HormoneTrigger | None:
    """对现在的浓度贡献最大的那条触发。"""
    decay_rate = math.log(2) / half_life_seconds_of(kind, config)

    def remaining(trigger: HormoneTrigger) -> float:
        elapsed = max((now - trigger.triggered_at).total_seconds(), 0.0)
        return trigger.rise * math.exp(-decay_rate * elapsed)

    candidates = [t for t in triggers if t.kind == kind and t.triggered_at <= now]
    return max(candidates, key=remaining, default=None)


def _describe_reason(
    trigger: HormoneTrigger, now: datetime, person_id: int | None, viewer_is_user: bool
) -> str:
    minutes = int((now - trigger.triggered_at).total_seconds() // _SECONDS_PER_MINUTE)
    if minutes < 1:
        ago = "刚才"
    elif minutes < _MINUTES_PER_HOUR:
        ago = f"{minutes} 分钟前"
    else:
        ago = f"{minutes // _MINUTES_PER_HOUR} 小时前"
    in_this_conversation = trigger.person_id is not None and trigger.person_id == person_id
    detail = trigger.reason if viewer_is_user or in_this_conversation else _OTHER_CONVERSATION
    who = "这个人" if in_this_conversation else ""
    return f"（主要原因：{ago}，{who}{detail}）"
