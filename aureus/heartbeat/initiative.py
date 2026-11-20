"""主动联系的把关、退避和真实原因：纯计算，不读写状态（D76）。"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from aureus.body import EnergyTier, SocialNeedReading
from aureus.common import InitiativeConfig


@dataclass(frozen=True)
class InitiativeState:
    """主动联系的历史，心跳在内存里维护、重启时从时间线还原。"""

    contacts_today: int = 0
    last_considered_at: datetime | None = None
    unanswered_streak: int = 0
    pending_since: datetime | None = None


@dataclass(frozen=True)
class InitiativeDecision:
    """允许考虑时给出真实原因，不允许时给出第一道没过的关。"""

    allowed: bool
    blocked_by: str
    reasons: tuple[str, ...]


def next_interval(streak: int, config: InitiativeConfig) -> timedelta:
    """每次没回应间隔翻倍，到上限就停，避免大计数产生巨大的指数。"""
    hours = config.min_interval_hours
    for _ in range(max(0, streak)):
        hours = min(hours * 2, config.max_interval_hours)
        if hours >= config.max_interval_hours:
            break
    return timedelta(hours=hours)


def daily_limit(config: InitiativeConfig, *, cautious: bool) -> int:
    """谨慎时每日次数减半，至少保留一次（D73 第 5 条）。"""
    return max(1, config.max_per_day // 2) if cautious else config.max_per_day


def decide(
    *,
    now: datetime,
    config: InitiativeConfig,
    state: InitiativeState,
    social_need: SocialNeedReading | None,
    due_items: Sequence[str],
    effective_tier: EnergyTier | None,
    night: bool,
    halted: bool,
    busy: bool,
    cautious: bool,
    has_channel: bool,
) -> InitiativeDecision:
    """按 D76 的顺序把关；原因只来自社交读数和到期约定，不让器官编造。"""
    checks = (
        (config.enabled, "主动联系已关闭"),
        (not halted, "急停中"),
        (not busy, "还有正在处理的消息"),
        (not night, "夜间不打扰"),
        (
            effective_tier in (None, EnergyTier.NORMAL, EnergyTier.UNLIMITED),
            "有效精力档位不足",
        ),
        (has_channel, "还不知道用户在哪个通道"),
        (state.unanswered_streak < config.give_up_after, "连续没有回应，等用户先开口"),
        (state.pending_since is None, "还在等上一次的回应"),
        (state.contacts_today < daily_limit(config, cautious=cautious), "今天主动的次数已满"),
        (
            state.last_considered_at is None
            or now - state.last_considered_at >= next_interval(state.unanswered_streak, config),
            "距离上次考虑还不够久",
        ),
    )
    for allowed, reason in checks:
        if not allowed:
            return InitiativeDecision(False, reason, ())
    reasons: list[str] = []
    if social_need is not None and social_need.enabled and social_need.high:
        elapsed = (
            "还没和用户好好聊过"
            if social_need.hours_since is None
            else f"距上次和用户好好聊过 {social_need.hours_since:.1f} 小时"
        )
        reasons.append(f"{elapsed}，社交需求 {social_need.level:.0%}")
    reasons.extend(f"答应的事到期了：{item}" for item in due_items)
    if not reasons:
        return InitiativeDecision(False, "没有要找的理由", ())
    return InitiativeDecision(True, "", tuple(reasons))
