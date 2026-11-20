"""心情计算：激素触发和反馈结果按长半衰期累积，再减去此刻的压力（D73）。

这里只算数字和写摘要，不读写任何状态、不碰时间线、不读系统时间。
"""

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from aureus.common import MoodConfig
from aureus.hormones import HormoneKind, HormoneTrigger
from aureus.timeline import VisibilityScope

# 激素对心情的方向：愉悦让心情变好，怒气和低落让心情变差，惊讶不分好坏。
_HORMONE_SIGNS = {
    HormoneKind.JOY: 1.0,
    HormoneKind.ANGER: -1.0,
    HormoneKind.HURT: -1.0,
    HormoneKind.SURPRISE: 0.0,
}
# 摘要里最多列几件事。
_MAX_REASONS = 3
# 贡献衰减到这么小就不算进原因：几乎没有影响了，写出来只会误导。
_NEGLIGIBLE = 0.01
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_OTHER_CONVERSATION = "另一段对话里的事"


@dataclass(frozen=True)
class MoodContribution:
    """一件影响心情的事：什么时候、加还是减多少、为什么。"""

    at: datetime
    delta: float
    reason: str
    # 这件事跟谁有关（人物 ID）；None 表示不针对某个人（比如任务失败）。
    person_id: int | None = None
    # 来源的可见范围，回复别人时决定能不能写原因的细节（同 D69）。
    visibility: VisibilityScope = VisibilityScope.PRIVATE


@dataclass(frozen=True)
class FeedbackOutcome:
    """一次有结果的行动（来自反馈表）。"""

    at: datetime
    succeeded: bool
    category: str


@dataclass(frozen=True)
class MoodReading:
    """某一刻的心情。"""

    valence: float
    cautious: bool
    summary: str


def from_triggers(triggers: Iterable[HormoneTrigger], config: MoodConfig) -> list[MoodContribution]:
    """激素触发换成心情的贡献；惊讶和贡献为 0 的不记。"""
    contributions: list[MoodContribution] = []
    for trigger in triggers:
        sign = _HORMONE_SIGNS[trigger.kind]
        delta = _clip(sign * config.hormone_weight * trigger.rise, config.max_single_change)
        if delta == 0.0:
            continue
        contributions.append(
            MoodContribution(
                at=trigger.triggered_at,
                delta=delta,
                reason=trigger.reason,
                person_id=trigger.person_id,
                visibility=trigger.source_visibility,
            )
        )
    return contributions


def from_outcomes(
    outcomes: Iterable[FeedbackOutcome], config: MoodConfig
) -> list[MoodContribution]:
    """反馈结果换成心情的贡献：成功加、失败减。"""
    contributions: list[MoodContribution] = []
    for outcome in outcomes:
        amount = config.success_amount if outcome.succeeded else -config.failure_amount
        delta = _clip(amount, config.max_single_change)
        if delta == 0.0:
            continue
        what = "成功了" if outcome.succeeded else "失败了"
        contributions.append(MoodContribution(outcome.at, delta, f"{outcome.category} {what}"))
    return contributions


def valence_at(
    contributions: Sequence[MoodContribution],
    now: datetime,
    config: MoodConfig,
    stress_level: float = 0.0,
) -> float:
    """某一刻的心情：贡献按半衰期衰减后求和，减去此刻压力的拉低，截到 −1 到 1。

    用闭式解直接算，结果和心跳快慢无关（D73 第 2 条）。时刻在 now 之后的贡献不算。
    """
    total = sum(_remaining(item, now, config) for item in contributions)
    total -= config.stress_weight * max(0.0, min(stress_level, 1.0))
    return max(-1.0, min(1.0, total))


def describe_mood(
    contributions: Sequence[MoodContribution],
    now: datetime,
    config: MoodConfig,
    *,
    stress_level: float,
    cautious: bool,
    person_id: int | None,
    viewer_is_user: bool,
) -> str:
    """放进系统上下文的心情事实摘要，谨慎时带上谨慎说明（D73 第 4、5 条）。"""
    valence = valence_at(contributions, now, config, stress_level)
    lines = [f"【心情】\n心情 {valence:+.2f}（−1 最差，1 最好，0 是平常）：{_word(valence)}。"]
    reasons = _main_reasons(contributions, now, config, person_id, viewer_is_user)
    stress_pull = config.stress_weight * max(0.0, min(stress_level, 1.0))
    if stress_pull >= _NEGLIGIBLE:
        reasons.append(f"此刻压力 {min(stress_level, 1.0):.0%}（−{stress_pull:.2f}）")
    if reasons:
        lines.append("主要因为：" + "；".join(reasons) + "。")
    if cautious:
        lines.append(
            "最近不太顺，你正处在谨慎状态：先确认再承诺，不主动揽事；"
            "这是因为上面这些真实的事，不需要额外演出来。"
        )
    return "\n".join(lines)


def _main_reasons(
    contributions: Sequence[MoodContribution],
    now: datetime,
    config: MoodConfig,
    person_id: int | None,
    viewer_is_user: bool,
) -> list[str]:
    """影响最大的几件事（按此刻剩下的贡献绝对值排）。"""
    weighted = [(item, _remaining(item, now, config)) for item in contributions]
    weighted = [pair for pair in weighted if abs(pair[1]) >= _NEGLIGIBLE]
    weighted.sort(key=lambda pair: abs(pair[1]), reverse=True)
    reasons: list[str] = []
    for item, remaining in weighted[:_MAX_REASONS]:
        in_this_conversation = item.person_id is not None and item.person_id == person_id
        private_to_someone = item.person_id is not None
        hide = not viewer_is_user and private_to_someone and not in_this_conversation
        detail = _OTHER_CONVERSATION if hide else item.reason
        reasons.append(f"{_ago(item.at, now)}，{detail}（{remaining:+.2f}）")
    return reasons


def _remaining(item: MoodContribution, now: datetime, config: MoodConfig) -> float:
    if item.at > now:
        return 0.0
    elapsed = (now - item.at).total_seconds()
    return item.delta * math.exp(-math.log(2) * elapsed / config.half_life_seconds)


def _clip(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _word(valence: float) -> str:
    if valence <= -0.5:
        return "很差"
    if valence <= -0.15:
        return "偏低"
    if valence < 0.15:
        return "平常"
    if valence < 0.5:
        return "不错"
    return "很好"


def _ago(at: datetime, now: datetime) -> str:
    minutes = int((now - at).total_seconds() // _SECONDS_PER_MINUTE)
    if minutes < 1:
        return "刚才"
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes} 分钟前"
    return f"{minutes // _MINUTES_PER_HOUR} 小时前"
